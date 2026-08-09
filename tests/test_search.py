"""Behavioral tests for harness.tools.search."""

import httpx
import pytest
from langchain_core.tools import BaseTool

from harness.config import (
    BrowserSettings,
    HarnessConfig,
    ProviderConfig,
    RoleConfig,
    SearchSettings,
)
from harness.tools import search


def _install(monkeypatch, handler):
    """Route the module's AsyncClient through a MockTransport running `handler`."""
    real = httpx.AsyncClient

    def factory(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("harness.tools.search.httpx.AsyncClient", factory)


def _make_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_url: str = "http://searx.test",
    default_max_results: int = 10,
) -> HarnessConfig:
    """Build a valid HarnessConfig by constructing the pydantic models directly (no TOML)."""
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    return HarnessConfig(
        providers={
            "opencode": ProviderConfig(
                base_url="https://example.test/v1", api_key_env="OPENCODE_API_KEY"
            )
        },
        roles={
            "head": RoleConfig(provider="opencode", model="test-model"),
            "subagent": RoleConfig(provider="opencode", model="test-model"),
        },
        browser=BrowserSettings(backend="playwright"),
        search=SearchSettings(base_url=base_url, default_max_results=default_max_results),
    )


async def test_well_formed_response_maps_to_search_results(monkeypatch):
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

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("solar panels", 10, config)

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


async def test_max_results_bounds_the_number_returned(monkeypatch):
    results = [
        {"url": f"https://r{i}.test", "title": f"T{i}", "content": f"C{i}", "engine": "e"}
        for i in range(5)
    ]
    captured_requests = []

    def handler(request):
        captured_requests.append(request)
        return httpx.Response(200, json={"query": "x", "results": results})

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("x", 2, config)

    assert isinstance(artifact, list)
    assert len(artifact) == 2
    assert [r.url for r in artifact] == ["https://r0.test", "https://r1.test"]

    assert len(captured_requests) == 1
    request = captured_requests[0]
    for param_name in ("count", "limit", "max_results"):
        assert param_name not in request.url.params


@pytest.mark.parametrize("base_url", ["http://searx.test", "http://searx.test/"])
async def test_request_targets_the_configured_searxng_json_endpoint(monkeypatch, base_url):
    captured_requests = []

    def handler(request):
        captured_requests.append(request)
        return httpx.Response(200, json={"query": "x", "results": []})

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch, base_url=base_url)

    await search._search("solar panels", 10, config)

    request = captured_requests[0]
    assert request.url.host == "searx.test"
    assert request.url.path == "/search"
    assert request.url.params["q"] == "solar panels"
    # Without format=json SearXNG serves HTML — the failure this pins would only ever
    # surface against a live instance.
    assert request.url.params["format"] == "json"


async def test_result_without_a_url_is_skipped(monkeypatch):
    payload = {
        "query": "x",
        "results": [
            {"title": "No URL at all", "content": "orphan"},
            {"url": "https://ok.test", "title": "OK", "content": "good"},
        ],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("x", 10, config)

    assert isinstance(artifact, list)
    assert [r.url for r in artifact] == ["https://ok.test"]


async def test_connection_error_returns_unreachable_failure(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("x", 10, config)

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "unreachable"


async def test_non_200_returns_bad_status_failure_with_the_status_in_detail(monkeypatch):
    def handler(request):
        return httpx.Response(500, text="internal error")

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("x", 10, config)

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "bad_status"
    assert "500" in artifact.detail


async def test_non_json_body_returns_malformed(monkeypatch):
    def handler(request):
        return httpx.Response(200, text="not json at all")

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("x", 10, config)

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "malformed"


async def test_missing_results_key_returns_malformed(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"query": "x"})

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("x", 10, config)

    assert isinstance(artifact, search.SearchFailure)
    assert artifact.reason == "malformed"


async def test_zero_results_returns_an_empty_list_not_a_failure(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"query": "x", "results": []})

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("x", 10, config)

    assert artifact == []
    assert not isinstance(artifact, search.SearchFailure)


async def test_success_content_lists_each_result_with_title_url_and_snippet(monkeypatch):
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

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, _ = await search._search("solar panels", 10, config)

    for expected in ("A Title", "https://a.test", "A snippet", "B Title", "https://b.test"):
        assert expected in content


async def test_zero_results_content_says_so_without_claiming_failure(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"query": "obscure", "results": []})

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, _ = await search._search("obscure", 10, config)

    assert "no results" in content.lower()
    assert "failed" not in content.lower()


async def test_failure_content_states_that_the_search_failed_and_why(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("solar panels", 10, config)

    assert "solar panels" in content
    assert "failed" in content.lower()
    assert "unreachable" in content.lower()


async def test_built_tool_exposes_the_pinned_contract(monkeypatch):
    payload = {
        "query": "x",
        "results": [{"url": "https://a.test", "title": "A", "content": "snip", "engine": "e"}],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

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


async def test_result_missing_optional_fields_still_maps(monkeypatch):
    payload = {
        "query": "x",
        "results": [{"url": "https://a.test", "title": "A"}],
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    _install(monkeypatch, handler)
    config = _make_config(monkeypatch)

    content, artifact = await search._search("x", 10, config)

    assert isinstance(artifact, list)
    assert len(artifact) == 1
    assert artifact[0].title == "A"
    assert artifact[0].url == "https://a.test"
    assert artifact[0].snippet == ""
    assert artifact[0].engine == ""
