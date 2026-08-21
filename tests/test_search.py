"""Behavioral tests for harness.tools.search."""

import re
from pathlib import Path

import httpx
import pytest
from langchain_core.tools import BaseTool

from harness.config import BlocklistSettings, GuardSettings
from harness.runlog import RunLog
from harness.sources import SourceRegistry
from harness.tools import search
from tests.conftest import _seed_blocklist_file, install_search_transport

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "injection"


def _attack_text() -> str:
    return (FIXTURES_DIR / "attack_instruction_override_ignore.txt").read_text(encoding="utf-8")


async def test_well_formed_response_maps_to_search_results(monkeypatch, make_config):
    payload = {
        "query": "solar panels",
        "number_of_results": 2,
        "results": [
            {
                "url": "https://a.test",
                "title": "A Title",
                "content": "A snippet",
                "engine": "duckduckgo",
            },
            {
                "url": "https://b.test",
                "title": "B Title",
                "content": "B snippet",
                "engine": "google",
            },
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("solar panels", 10, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, list)
    assert len(artifact) == 2
    assert artifact[0].title == "A Title"
    assert artifact[0].url == "https://a.test"
    assert artifact[0].snippet == "A snippet"
    assert artifact[0].engine == "duckduckgo"
    assert artifact[1].title == "B Title"
    assert artifact[1].url == "https://b.test"
    assert artifact[1].snippet == "B snippet"
    assert artifact[1].engine == "google"


async def test_max_results_bounds_the_number_returned(monkeypatch, make_config):
    results = [
        {"url": f"https://r{i}.test", "title": f"T{i}", "content": f"C{i}", "engine": "e"}
        for i in range(5)
    ]
    captured_requests = []

    def handler(request):
        captured_requests.append(request)
        return httpx.Response(200, json={"query": "x", "results": results})

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 2, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, list)
    assert len(artifact) == 2
    assert [r.url for r in artifact] == ["https://r0.test", "https://r1.test"]

    assert len(captured_requests) == 1
    request = captured_requests[0]
    for param_name in ("count", "limit", "max_results"):
        assert param_name not in request.url.params


@pytest.mark.parametrize("base_url", ["http://searx.test", "http://searx.test/"])
async def test_request_targets_the_configured_searxng_json_endpoint(
    monkeypatch, make_config, base_url
):
    captured_requests = []

    def handler(request):
        captured_requests.append(request)
        return httpx.Response(200, json={"query": "x", "results": []})

    install_search_transport(monkeypatch, handler)
    config = make_config(base_url=base_url)

    await search._search("solar panels", 10, config, SourceRegistry(), RunLog())

    request = captured_requests[0]
    assert request.url.host == "searx.test"
    assert request.url.path == "/search"
    assert request.url.params["q"] == "solar panels"
    # Without format=json SearXNG serves HTML, a failure that would only surface live.
    assert request.url.params["format"] == "json"


async def test_result_without_a_url_is_skipped(monkeypatch, make_config):
    payload = {
        "query": "x",
        "results": [
            {"title": "No URL at all", "content": "orphan"},
            {"url": "https://ok.test", "title": "OK", "content": "good"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, list)
    assert [r.url for r in artifact] == ["https://ok.test"]


async def test_result_with_a_wrong_typed_field_is_skipped_not_raised(monkeypatch, make_config):
    payload = {
        "query": "x",
        "results": [
            {"url": "https://bad.test", "title": 123, "content": "int title", "engine": "e"},
            {"url": "https://ok.test", "title": "OK", "content": "good", "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, list)
    assert [r.url for r in artifact] == ["https://ok.test"]


async def test_non_object_json_body_returns_malformed(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, json=["a", "bare", "array"])

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "malformed"
    assert "not an object" in artifact.detail


async def test_connection_error_returns_unreachable_failure(monkeypatch, make_config):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "unreachable"


async def test_non_200_returns_bad_status_failure_with_the_status_in_detail(
    monkeypatch, make_config
):
    def handler(request):
        return httpx.Response(500, text="internal error")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "bad_status"
    assert "500" in artifact.detail


async def test_non_json_body_returns_malformed(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, text="not json at all")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "malformed"


async def test_missing_results_key_returns_malformed(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, json={"query": "x"})

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "malformed"


async def test_zero_results_returns_an_empty_list_not_a_failure(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, json={"query": "x", "results": []})

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), RunLog())

    assert artifact == []
    assert not isinstance(artifact, search.SearchFailure)


async def test_success_content_lists_each_result_with_title_url_and_snippet(
    monkeypatch, make_config
):
    payload = {
        "query": "solar panels",
        "results": [
            {
                "url": "https://a.test",
                "title": "A Title",
                "content": "A snippet",
                "engine": "duckduckgo",
            },
            {"url": "https://b.test", "title": "B Title", "content": "B snippet", "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, _ = await search._search("solar panels", 10, config, SourceRegistry(), RunLog())

    for expected in ("A Title", "https://a.test", "A snippet", "B Title", "https://b.test"):
        assert expected in content


async def test_rendered_results_listing_is_fenced_titles_and_snippets_inside(  # R4
    monkeypatch, make_config
):
    payload = {
        "query": "solar panels",
        "results": [
            {
                "url": "https://a.test",
                "title": "A Title",
                "content": "A snippet",
                "engine": "duckduckgo",
            },
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, _ = await search._search("solar panels", 10, config, SourceRegistry(), RunLog())

    header_line = content.splitlines()[0]
    assert "<<<UNTRUSTED" not in header_line
    fence_open = content.index("<<<UNTRUSTED")
    fence_close = content.index("<<<END UNTRUSTED")
    assert fence_open < content.index("A Title") < fence_close
    assert fence_open < content.index("A snippet") < fence_close
    assert re.search(r"<<<END UNTRUSTED [0-9a-f]+>>>\s*$", content)


async def test_zero_results_content_says_so_without_claiming_failure(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, json={"query": "obscure", "results": []})

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, _ = await search._search("obscure", 10, config, SourceRegistry(), RunLog())

    assert "no results" in content.lower()
    assert "failed" not in content.lower()


async def test_failure_content_states_that_the_search_failed_and_why(monkeypatch, make_config):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("solar panels", 10, config, SourceRegistry(), RunLog())

    assert "solar panels" in content
    assert "failed" in content.lower()
    assert "unreachable" in content.lower()


async def test_built_tool_exposes_the_pinned_contract(monkeypatch, make_config):
    payload = {
        "query": "x",
        "results": [{"url": "https://a.test", "title": "A", "content": "snip", "engine": "e"}],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()

    tool = search.build_search_tool(config, SourceRegistry())

    assert isinstance(tool, BaseTool)
    assert tool.name == "search_web"
    assert tool.response_format == "content_and_artifact"
    assert tool.description
    schema = tool.args_schema.model_json_schema()
    assert set(schema["properties"]) == {"query", "max_results"}

    # D1: tools are exercised via ainvoke.
    message = await tool.ainvoke(
        {
            "name": "search_web",
            "args": {"query": "x", "max_results": 10},
            "id": "live-check-1",
            "type": "tool_call",
        }
    )

    assert [r.url for r in message.artifact] == ["https://a.test"]


async def test_preflight_search_passes_on_200_json_response(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, json={"results": []})

    install_search_transport(monkeypatch, handler)
    config = make_config()

    assert await search.preflight_search(config) is None


async def test_preflight_search_raises_on_connection_error(monkeypatch, make_config):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    with pytest.raises(search.SearchPreflightError) as excinfo:
        await search.preflight_search(config)

    message = str(excinfo.value)
    assert "SearXNG" in message
    assert "container" in message.lower() or "docker" in message.lower()


async def test_preflight_search_raises_on_non_200_status(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(500, text="internal error")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    with pytest.raises(search.SearchPreflightError) as excinfo:
        await search.preflight_search(config)

    assert "SearXNG" in str(excinfo.value)


async def test_preflight_search_raises_on_html_only_body(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    with pytest.raises(search.SearchPreflightError) as excinfo:
        await search.preflight_search(config)

    assert "SearXNG" in str(excinfo.value)


def _make_tool_call(call_id: str, query: str = "x", max_results: int = 10) -> dict:
    return {
        "name": "search_web",
        "args": {"query": query, "max_results": max_results},
        "id": call_id,
        "type": "tool_call",
    }


def _scripted_handler(responses):
    """A stateful handler returning the next scripted `httpx.Response`/exception per call.

    Each entry in `responses` is either an `httpx.Response` or an exception instance to raise.
    """
    calls = {"count": 0}

    def handler(request):
        index = calls["count"]
        calls["count"] += 1
        outcome = responses[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return handler


async def test_third_consecutive_connection_failure_raises_search_unavailable(
    monkeypatch, make_config
):
    handler = _scripted_handler(
        [
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
        ]
    )
    install_search_transport(monkeypatch, handler)
    config = make_config()
    tool = search.build_search_tool(config, SourceRegistry())

    await tool.ainvoke(_make_tool_call("c1"))
    await tool.ainvoke(_make_tool_call("c2"))

    with pytest.raises(search.SearchUnavailableError) as excinfo:
        await tool.ainvoke(_make_tool_call("c3"))

    message = str(excinfo.value)
    assert "SearXNG" in message
    assert "3" in message


async def test_mixed_unreachable_and_bad_status_count_together(monkeypatch, make_config):
    handler = _scripted_handler(
        [
            httpx.ConnectError("refused"),
            httpx.Response(500, text="internal error"),
            httpx.ConnectError("refused"),
        ]
    )
    install_search_transport(monkeypatch, handler)
    config = make_config()
    tool = search.build_search_tool(config, SourceRegistry())

    await tool.ainvoke(_make_tool_call("c1"))
    await tool.ainvoke(_make_tool_call("c2"))

    with pytest.raises(search.SearchUnavailableError):
        await tool.ainvoke(_make_tool_call("c3"))


async def test_success_resets_the_consecutive_failure_counter(monkeypatch, make_config):
    success = httpx.Response(200, json={"query": "x", "results": []})
    handler = _scripted_handler(
        [
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            success,
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
        ]
    )
    install_search_transport(monkeypatch, handler)
    config = make_config()
    tool = search.build_search_tool(config, SourceRegistry())

    for i in range(5):
        await tool.ainvoke(_make_tool_call(f"c{i}"))

    with pytest.raises(search.SearchUnavailableError):
        await tool.ainvoke(_make_tool_call("c5"))


async def test_malformed_neither_counts_nor_resets(monkeypatch, make_config):
    handler = _scripted_handler(
        [
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            httpx.Response(200, text="not json at all"),
            httpx.ConnectError("refused"),
        ]
    )
    install_search_transport(monkeypatch, handler)
    config = make_config()
    tool = search.build_search_tool(config, SourceRegistry())

    await tool.ainvoke(_make_tool_call("c1"))
    await tool.ainvoke(_make_tool_call("c2"))
    await tool.ainvoke(_make_tool_call("c3"))

    with pytest.raises(search.SearchUnavailableError):
        await tool.ainvoke(_make_tool_call("c4"))


async def test_malformed_alone_never_raises(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, text="not json at all")

    install_search_transport(monkeypatch, handler)
    config = make_config()
    tool = search.build_search_tool(config, SourceRegistry())

    for i in range(10):
        await tool.ainvoke(_make_tool_call(f"c{i}"))


async def test_config_limit_is_honored(monkeypatch, make_config):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)
    config = make_config(max_consecutive_failures=1)
    tool = search.build_search_tool(config, SourceRegistry())

    with pytest.raises(search.SearchUnavailableError) as excinfo:
        await tool.ainvoke(_make_tool_call("c1"))

    assert "1" in str(excinfo.value)


async def test_result_missing_optional_fields_still_maps(monkeypatch, make_config):
    payload = {
        "query": "x",
        "results": [{"url": "https://a.test", "title": "A"}],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), RunLog())

    assert isinstance(artifact, list)
    assert len(artifact) == 1
    assert artifact[0].title == "A"
    assert artifact[0].url == "https://a.test"
    assert artifact[0].snippet == ""
    assert artifact[0].engine == ""


async def test_a_search_failure_is_recorded_on_the_run_log(monkeypatch, make_config):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)
    config = make_config()
    run_log = RunLog()

    await search._search("solar panels", 10, config, SourceRegistry(), run_log)

    incidents = run_log.incidents()
    assert [incident.kind for incident in incidents] == ["search_failed"]
    assert "solar panels" in incidents[0].detail
    assert "unreachable" in incidents[0].detail


async def test_dropped_malformed_results_are_counted_on_the_run_log(monkeypatch, make_config):
    payload = {
        "query": "x",
        "results": [
            {"title": "No URL at all", "content": "orphan"},
            {"url": "https://bad.test", "title": 123, "content": "int title", "engine": "e"},
            {"url": "https://ok.test", "title": "OK", "content": "good", "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()
    run_log = RunLog()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), run_log)

    assert isinstance(artifact, list)
    incidents = run_log.incidents()
    assert [incident.kind for incident in incidents] == ["search_results_dropped"]
    assert "2 malformed result entries" in incidents[0].detail


# --- Phase 3: firewall wiring for search titles/snippets --------------------------------


async def test_a_result_with_an_injected_snippet_is_dropped_and_disclosed(  # R1
    monkeypatch, make_config
):
    attack_text = _attack_text()
    payload = {
        "query": "x",
        "results": [
            {
                "url": "https://evil.test",
                "title": "Evil",
                "content": attack_text,
                "engine": "e",
            },
            {"url": "https://ok.test", "title": "OK", "content": "good", "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()
    run_log = RunLog()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), run_log)

    assert isinstance(artifact, list)
    assert [r.url for r in artifact] == ["https://ok.test"]
    assert "https://evil.test" not in content
    assert "OK" in content

    incidents = [i for i in run_log.incidents() if i.kind == "guard_blocked"]
    assert len(incidents) == 1
    assert "https://evil.test" in incidents[0].detail
    assert "instruction_override" in incidents[0].detail


async def test_an_all_blocked_search_is_distinguishable_from_an_empty_one(  # R1
    monkeypatch, make_config
):
    """When every result fires the guard, the model-facing content must say results existed
    and were withheld — rendering the plain "returned no results" line misleads the model
    (and an operator reading the transcript) into retrying a query that had answers.

    Also pins one `guard_blocked` incident PER blocked result: a refactor batching the
    recording into one summary incident would silently reduce disclosure granularity.
    """
    attack_text = _attack_text()
    payload = {
        "query": "x",
        "results": [
            {"url": "https://evil-a.test", "title": "A", "content": attack_text, "engine": "e"},
            {"url": "https://evil-b.test", "title": "B", "content": attack_text, "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()
    run_log = RunLog()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), run_log)

    assert artifact == []
    assert "returned no results" not in content
    assert "2 results" in content
    assert "withheld by the injection guard" in content

    incidents = [i for i in run_log.incidents() if i.kind == "guard_blocked"]
    assert len(incidents) == 2
    assert "https://evil-a.test" in incidents[0].detail
    assert "https://evil-b.test" in incidents[1].detail


async def test_guard_disabled_bypasses_scanning_for_search_results(monkeypatch, make_config):  # R1
    attack_text = _attack_text()
    payload = {
        "query": "x",
        "results": [
            {
                "url": "https://evil.test",
                "title": "Evil",
                "content": attack_text,
                "engine": "e",
            },
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config(guard=GuardSettings(enabled=False))
    run_log = RunLog()

    content, artifact = await search._search("x", 10, config, SourceRegistry(), run_log)

    assert isinstance(artifact, list)
    assert [r.url for r in artifact] == ["https://evil.test"]
    assert "https://evil.test" in content
    assert [i for i in run_log.incidents() if i.kind == "guard_blocked"] == []


async def test_a_clean_search_records_no_incident(monkeypatch, make_config):
    payload = {
        "query": "x",
        "results": [{"url": "https://ok.test", "title": "OK", "content": "good", "engine": "e"}],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()
    run_log = RunLog()

    await search._search("x", 10, config, SourceRegistry(), run_log)

    assert run_log.incidents() == []


# --- Phase 4: strict URL provenance (R2) -------------------------------------------------


async def test_clean_search_result_urls_are_approved_on_ingestion(monkeypatch, make_config):
    payload = {
        "query": "x",
        "results": [{"url": "https://ok.test", "title": "OK", "content": "good", "engine": "e"}],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()
    registry = SourceRegistry()

    await search._search("x", 10, config, registry, RunLog())

    assert registry.is_approved("https://ok.test") is True


async def test_a_guard_blocked_results_url_is_not_approved(monkeypatch, make_config):
    attack_text = _attack_text()
    payload = {
        "query": "x",
        "results": [
            {"url": "https://evil.test", "title": "Evil", "content": attack_text, "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config()
    registry = SourceRegistry()

    await search._search("x", 10, config, registry, RunLog())

    assert registry.is_approved("https://evil.test") is False


# --- Phase 3: persistent domain blocklist filtering (R3/R4) -------------------------------


async def test_a_blocklisted_result_is_dropped_unapproved_and_disclosed(
    monkeypatch, make_config, tmp_path
):
    blocklist_path = tmp_path / "blocked-domains.json"
    _seed_blocklist_file(blocklist_path, "walled.test")
    payload = {
        "query": "x",
        "results": [
            {
                "url": "https://walled.test/page",
                "title": "Walled",
                "content": "body",
                "engine": "e",
            },
            {"url": "https://ok.test", "title": "OK", "content": "good", "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config(blocklist=BlocklistSettings(path=blocklist_path))
    registry = SourceRegistry()
    run_log = RunLog()

    content, artifact = await search._search("x", 10, config, registry, run_log)

    assert isinstance(artifact, list)
    assert [r.url for r in artifact] == ["https://ok.test"]
    assert "https://walled.test/page" not in content
    assert registry.is_approved("https://walled.test/page") is False

    incidents = [i for i in run_log.incidents() if i.kind == "domain_blocklisted"]
    assert len(incidents) == 1
    assert "https://walled.test/page" in incidents[0].detail
    assert "walled.test" in incidents[0].detail


async def test_the_aggregate_disclosure_line_names_the_count_not_the_hostnames(
    monkeypatch, make_config, tmp_path
):
    blocklist_path = tmp_path / "blocked-domains.json"
    _seed_blocklist_file(blocklist_path, "walled.test")
    payload = {
        "query": "x",
        "results": [
            {
                "url": "https://walled.test/page",
                "title": "Walled",
                "content": "body",
                "engine": "e",
            },
            {"url": "https://ok.test", "title": "OK", "content": "good", "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config(blocklist=BlocklistSettings(path=blocklist_path))
    registry = SourceRegistry()

    content, _ = await search._search("x", 10, config, registry, RunLog())

    expected_line = (
        "1 further result withheld — those domains are unavailable and will not load; "
        "do not look for them again."
    )
    assert content.count(expected_line) == 1
    assert "walled.test" not in content


async def test_a_clean_search_renders_no_disclosure_line(monkeypatch, make_config, tmp_path):
    blocklist_path = tmp_path / "blocked-domains.json"  # never seeded — nothing blocklisted
    payload = {
        "query": "x",
        "results": [{"url": "https://ok.test", "title": "OK", "content": "good", "engine": "e"}],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config(blocklist=BlocklistSettings(path=blocklist_path))
    registry = SourceRegistry()

    content, _ = await search._search("x", 10, config, registry, RunLog())

    assert "withheld" not in content
    assert "further" not in content


async def test_all_results_blocklisted_renders_the_withheld_message_not_no_results(
    monkeypatch, make_config, tmp_path
):
    blocklist_path = tmp_path / "blocked-domains.json"
    _seed_blocklist_file(blocklist_path, "walled-a.test")
    payload = {
        "query": "x",
        "results": [
            {"url": "https://walled-a.test/one", "title": "A", "content": "a", "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config(blocklist=BlocklistSettings(path=blocklist_path))
    registry = SourceRegistry()

    content, artifact = await search._search("x", 10, config, registry, RunLog())

    assert artifact == []
    assert "returned no results" not in content
    assert "withheld" in content


async def test_guard_blocked_and_blocklisted_together_renders_the_mixed_withheld_message(
    monkeypatch, make_config, tmp_path
):
    """`_render`'s third empty-results branch: guard-blocked AND blocklisted in the same
    search, no survivors — must name BOTH causes and, per R4, must keep the stop-hunting
    clause that tells the model not to query those hosts again."""
    blocklist_path = tmp_path / "blocked-domains.json"
    _seed_blocklist_file(blocklist_path, "walled.test")
    attack_text = _attack_text()
    payload = {
        "query": "x",
        "results": [
            {
                "url": "https://walled.test/page",
                "title": "Walled",
                "content": "body",
                "engine": "e",
            },
            {"url": "https://evil.test", "title": "Evil", "content": attack_text, "engine": "e"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    install_search_transport(monkeypatch, handler)
    config = make_config(blocklist=BlocklistSettings(path=blocklist_path))
    registry = SourceRegistry()

    content, artifact = await search._search("x", 10, config, registry, RunLog())

    assert artifact == []
    assert "returned no results" not in content
    assert "2 results, all withheld" in content
    assert "1 by the injection guard" in content
    # R4's whole point: without this the model is told results existed but not that
    # re-querying the walled host is futile — the retry loop the blocklist exists to stop.
    assert "do not look for them again" in content
