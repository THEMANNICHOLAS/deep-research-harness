"""Behavioral tests for harness.tools.fetch."""

from types import SimpleNamespace
from typing import Literal

import pytest
from crawl4ai import DefaultMarkdownGenerator, PruningContentFilter  # type: ignore[import-untyped]
from langchain_core.tools import BaseTool

from harness.config import (
    BrowserSettings,
    FetchSettings,
    HarnessConfig,
    ProviderConfig,
    RoleConfig,
    SearchSettings,
)
from harness.sources import SourceRegistry
from harness.tools import fetch


class _FakeMarkdown:
    """Stand-in for crawl4ai's StringCompatibleMarkdown, exposing raw and fit variants."""

    def __init__(self, raw_markdown: str = "", fit_markdown: str = "") -> None:
        self.raw_markdown = raw_markdown
        self.fit_markdown = fit_markdown


class _FakeResult:
    """Stand-in for crawl4ai's CrawlResult, exposing only the attributes fetch.py reads."""

    def __init__(
        self,
        url: str,
        *,
        success: bool = True,
        error_message: str | None = None,
        status_code: int | None = 200,
        response_headers: dict | None = None,
        metadata: dict | None = None,
        markdown: _FakeMarkdown | None = None,
    ) -> None:
        self.url = url
        self.success = success
        self.error_message = error_message
        self.status_code = status_code
        self.response_headers = response_headers
        self.metadata = metadata
        self.markdown = markdown


def _make_fake_crawler_class(results: list[_FakeResult]) -> type:
    """Build a fake AsyncWebCrawler class recording construction and `arun_many` calls."""

    class _FakeCrawler:
        constructed_with: list[object] = []
        calls: list[SimpleNamespace] = []

        def __init__(self, config: object = None) -> None:
            _FakeCrawler.constructed_with.append(config)

        async def __aenter__(self) -> "_FakeCrawler":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def arun_many(
            self, urls: list[str], config: object = None, dispatcher: object = None
        ) -> list[_FakeResult]:
            _FakeCrawler.calls.append(
                SimpleNamespace(urls=urls, config=config, dispatcher=dispatcher)
            )
            return results

    return _FakeCrawler


def _make_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend: Literal["lightpanda", "playwright"] = "playwright",
    cdp_url: str | None = None,
    page_timeout_ms: int = 15000,
    max_concurrency: int = 5,
    per_page_char_cap: int = 12000,
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
        browser=BrowserSettings(backend=backend, cdp_url=cdp_url),
        fetch=FetchSettings(
            page_timeout_ms=page_timeout_ms,
            max_concurrency=max_concurrency,
            per_page_char_cap=per_page_char_cap,
        ),
        search=SearchSettings(base_url="http://localhost:8080"),
    )


async def test_empty_url_list_returns_empty_content_and_artifact(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    fake_cls = _make_fake_crawler_class([])
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    content, pages = await fetch._fetch([], config, registry)

    assert (content, pages) == ("", [])
    assert fake_cls.constructed_with == []


async def test_mixed_batch_returns_one_entry_per_url_with_successes_intact(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test",
            markdown=_FakeMarkdown(raw_markdown="A content", fit_markdown="A content"),
        ),
        _FakeResult(
            "https://b.test",
            success=False,
            error_message="boom",
            status_code=500,
            markdown=None,
        ),
        _FakeResult(
            "https://c.test",
            markdown=_FakeMarkdown(raw_markdown="C content", fit_markdown="C content"),
        ),
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    content, pages = await fetch._fetch(
        ["https://a.test", "https://b.test", "https://c.test"], config, registry
    )

    assert len(pages) == 3
    assert [p.url for p in pages] == ["https://a.test", "https://b.test", "https://c.test"]
    assert pages[0].outcome == "fetched"
    assert pages[0].markdown == "A content"
    assert pages[1].outcome == "error"
    assert pages[2].outcome == "fetched"
    assert pages[2].markdown == "C content"


@pytest.mark.parametrize(
    ("status_code", "error_message", "content_type", "markdown", "expected"),
    [
        (403, None, "text/html", "content", "blocked"),
        (429, None, "text/html", "content", "blocked"),
        (503, None, "text/html", "content", "blocked"),
        (200, "Timeout after 15000ms", "text/html", "", "timeout"),
        (200, None, "application/pdf", "", "non_html"),
        (200, None, "text/html", "", "non_html"),
        (500, "internal server error", "text/html", "", "error"),
        (200, None, "text/html", "# Title\ncontent", "fetched"),
    ],
)
def test_classification_rules(status_code, error_message, content_type, markdown, expected):
    assert fetch.classify(status_code, error_message, content_type, markdown) == expected


async def test_every_page_has_a_registered_source_id_and_duplicates_share_one(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://dup.test",
            markdown=_FakeMarkdown(raw_markdown="one", fit_markdown="one"),
        ),
        _FakeResult(
            "https://dup.test",
            markdown=_FakeMarkdown(raw_markdown="two", fit_markdown="two"),
        ),
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    _, pages = await fetch._fetch(["https://dup.test", "https://dup.test"], config, registry)

    assert len(pages) == 2
    for page in pages:
        assert registry.get(page.source_id) is not None
    assert pages[0].source_id == pages[1].source_id
    assert len(registry.all()) == 1


async def test_content_is_truncated_at_the_cap_but_artifact_keeps_full_text(monkeypatch):
    cap = 50
    config = _make_config(monkeypatch, per_page_char_cap=cap)
    registry = SourceRegistry()
    long_markdown = "x" * 500
    results = [
        _FakeResult(
            "https://long.test",
            markdown=_FakeMarkdown(raw_markdown=long_markdown, fit_markdown=long_markdown),
        )
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    content, pages = await fetch._fetch(["https://long.test"], config, registry)

    assert len(content) < len(long_markdown)
    assert str(cap) in content
    assert pages[0].markdown == long_markdown


async def test_content_has_a_heading_for_every_url_including_failures(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://ok.test",
            markdown=_FakeMarkdown(raw_markdown="fine", fit_markdown="fine"),
        ),
        _FakeResult(
            "https://fail.test",
            success=False,
            error_message="server exploded",
            status_code=500,
            markdown=None,
        ),
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    content, pages = await fetch._fetch(["https://ok.test", "https://fail.test"], config, registry)

    for page in pages:
        assert f"## [{page.source_id}] {page.url}" in content
        assert page.outcome in content


async def test_config_limits_reach_the_crawl4ai_call(monkeypatch):
    config = _make_config(monkeypatch, page_timeout_ms=1234, max_concurrency=3)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test",
            markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"),
        )
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    await fetch._fetch(["https://a.test"], config, registry)

    assert len(fake_cls.calls) == 1
    recorded = fake_cls.calls[0]
    assert recorded.config.page_timeout == 1234
    assert recorded.dispatcher.max_session_permit == 3


async def test_boilerplate_stripping_config_reaches_the_crawl4ai_call(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    await fetch._fetch(["https://a.test"], config, registry)

    recorded = fake_cls.calls[0].config
    assert set(recorded.excluded_tags) >= {"nav", "header", "footer", "aside", "script"}
    assert isinstance(recorded.markdown_generator, DefaultMarkdownGenerator)
    assert isinstance(recorded.markdown_generator.content_filter, PruningContentFilter)


async def test_built_tool_exposes_the_pinned_contract_and_returns_content_and_artifact(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://tool.test",
            metadata={"title": "Tool Page"},
            markdown=_FakeMarkdown(raw_markdown="raw body", fit_markdown="clean body"),
        )
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    fetch_pages = fetch.build_fetch_tool(config, registry)

    assert isinstance(fetch_pages, BaseTool)
    assert fetch_pages.name == "fetch_pages"
    assert fetch_pages.response_format == "content_and_artifact"
    assert fetch_pages.description
    schema = fetch_pages.args_schema.model_json_schema()
    assert set(schema["properties"]) == {"urls"}
    assert schema["properties"]["urls"]["type"] == "array"

    # D1: tools are driven with `ainvoke`; the ToolCall form is what surfaces the artifact.
    message = await fetch_pages.ainvoke(
        {
            "name": "fetch_pages",
            "args": {"urls": ["https://tool.test"]},
            "id": "live-check-1",
            "type": "tool_call",
        }
    )

    assert "## [S1] https://tool.test" in message.content
    assert [page.url for page in message.artifact] == ["https://tool.test"]
    assert message.artifact[0].markdown == "clean body"
    assert message.artifact[0].title == "Tool Page"


def test_build_browser_config_maps_backend_to_browser_mode():
    lightpanda = fetch.build_browser_config(
        BrowserSettings(backend="lightpanda", cdp_url="ws://lightpanda.test:9222")
    )
    assert lightpanda.browser_mode == "cdp"
    assert lightpanda.cdp_url == "ws://lightpanda.test:9222"

    playwright = fetch.build_browser_config(BrowserSettings(backend="playwright"))
    assert playwright.cdp_url is None
    assert playwright.browser_mode == "dedicated"


async def test_fit_markdown_is_preferred_over_raw_markdown(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://pruned.test",
            markdown=_FakeMarkdown(
                raw_markdown="Home About Contact The real article body. Footer links",
                fit_markdown="The real article body.",
            ),
        )
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    content, pages = await fetch._fetch(["https://pruned.test"], config, registry)

    assert pages[0].markdown == "The real article body."
    assert "Footer links" not in content


async def test_raw_markdown_is_used_when_fit_markdown_is_empty(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://unpruned.test",
            markdown=_FakeMarkdown(raw_markdown="Only raw survived.", fit_markdown=""),
        )
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    _, pages = await fetch._fetch(["https://unpruned.test"], config, registry)

    assert pages[0].markdown == "Only raw survived."
    assert pages[0].outcome == "fetched"


async def test_title_from_result_metadata_reaches_the_page_and_the_registry(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://titled.test",
            metadata={"title": "An Article Title"},
            markdown=_FakeMarkdown(raw_markdown="body", fit_markdown="body"),
        )
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    _, pages = await fetch._fetch(["https://titled.test"], config, registry)

    assert pages[0].title == "An Article Title"
    source = registry.get(pages[0].source_id)
    assert source is not None
    assert source.title == "An Article Title"


async def test_result_whose_url_differs_from_the_input_is_still_paired(monkeypatch):
    config = _make_config(monkeypatch)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://redirected.test/final",
            markdown=_FakeMarkdown(
                raw_markdown="redirected content", fit_markdown="redirected content"
            ),
        )
    ]
    fake_cls = _make_fake_crawler_class(results)
    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)

    _, pages = await fetch._fetch(["https://original.test/start"], config, registry)

    assert len(pages) == 1
    assert pages[0].url == "https://original.test/start"
    assert pages[0].markdown == "redirected content"
