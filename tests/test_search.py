"""Behavioral tests for harness.tools.search."""

import httpx
import pytest
from langchain_core.tools import BaseTool

from harness.runlog import RunLog
from harness.tools import search
from tests.conftest import install_search_transport


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

    content, artifact = await search._search("solar panels", 10, config, RunLog())

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

    content, artifact = await search._search("x", 2, config, RunLog())

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

    await search._search("solar panels", 10, config, RunLog())

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

    content, artifact = await search._search("x", 10, config, RunLog())

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

    content, artifact = await search._search("x", 10, config, RunLog())

    assert isinstance(artifact, list)
    assert [r.url for r in artifact] == ["https://ok.test"]


async def test_non_object_json_body_returns_malformed(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, json=["a", "bare", "array"])

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "malformed"
    assert "not an object" in artifact.detail


async def test_connection_error_returns_unreachable_failure(monkeypatch, make_config):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "unreachable"


async def test_non_200_returns_bad_status_failure_with_the_status_in_detail(
    monkeypatch, make_config
):
    def handler(request):
        return httpx.Response(500, text="internal error")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "bad_status"
    assert "500" in artifact.detail


async def test_non_json_body_returns_malformed(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, text="not json at all")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "malformed"


async def test_missing_results_key_returns_malformed(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, json={"query": "x"})

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, RunLog())

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "malformed"


async def test_zero_results_returns_an_empty_list_not_a_failure(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, json={"query": "x", "results": []})

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("x", 10, config, RunLog())

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

    content, _ = await search._search("solar panels", 10, config, RunLog())

    for expected in ("A Title", "https://a.test", "A snippet", "B Title", "https://b.test"):
        assert expected in content


async def test_zero_results_content_says_so_without_claiming_failure(monkeypatch, make_config):
    def handler(request):
        return httpx.Response(200, json={"query": "obscure", "results": []})

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, _ = await search._search("obscure", 10, config, RunLog())

    assert "no results" in content.lower()
    assert "failed" not in content.lower()


async def test_failure_content_states_that_the_search_failed_and_why(monkeypatch, make_config):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)
    config = make_config()

    content, artifact = await search._search("solar panels", 10, config, RunLog())

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

    tool = search.build_search_tool(config)

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
    tool = search.build_search_tool(config)

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
    tool = search.build_search_tool(config)

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
    tool = search.build_search_tool(config)

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
    tool = search.build_search_tool(config)

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
    tool = search.build_search_tool(config)

    for i in range(10):
        await tool.ainvoke(_make_tool_call(f"c{i}"))


async def test_config_limit_is_honored(monkeypatch, make_config):
    def handler(request):
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)
    config = make_config(max_consecutive_failures=1)
    tool = search.build_search_tool(config)

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

    content, artifact = await search._search("x", 10, config, RunLog())

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

    await search._search("solar panels", 10, config, run_log)

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

    content, artifact = await search._search("x", 10, config, run_log)

    assert isinstance(artifact, list)
    incidents = run_log.incidents()
    assert [incident.kind for incident in incidents] == ["search_results_dropped"]
    assert "2 malformed result entries" in incidents[0].detail


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

    await search._search("x", 10, config, run_log)

    assert run_log.incidents() == []
