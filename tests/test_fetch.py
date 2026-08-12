"""Behavioral tests for harness.tools.fetch."""

from types import SimpleNamespace

import pytest
from crawl4ai import DefaultMarkdownGenerator, PruningContentFilter  # type: ignore[import-untyped]
from langchain_core.tools import BaseTool

from harness.config import BrowserSettings
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
        error_message: str | None = None,
        status_code: int | None = 200,
        response_headers: dict | None = None,
        metadata: dict | None = None,
        markdown: _FakeMarkdown | None = None,
    ) -> None:
        self.url = url
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


@pytest.fixture
def install_crawler(monkeypatch):
    """Patch fetch's AsyncWebCrawler with a fake serving canned results; returns the class."""

    def _install(results: list[_FakeResult]) -> type:
        fake_cls = _make_fake_crawler_class(results)
        monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)
        return fake_cls

    return _install


async def test_empty_url_list_returns_empty_content_and_artifact(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    fake_cls = install_crawler([])

    content, pages = await fetch._fetch([], config, registry)

    assert (content, pages) == ("", [])
    assert fake_cls.constructed_with == []


async def test_mixed_batch_returns_one_entry_per_url_with_successes_intact(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test",
            markdown=_FakeMarkdown(raw_markdown="A content", fit_markdown="A content"),
        ),
        _FakeResult(
            "https://b.test",
            error_message="boom",
            status_code=500,
            markdown=None,
        ),
        _FakeResult(
            "https://c.test",
            markdown=_FakeMarkdown(raw_markdown="C content", fit_markdown="C content"),
        ),
    ]
    install_crawler(results)

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


async def test_equivalent_url_spellings_are_fetched_once_with_one_source_id(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://dup.test/a",
            markdown=_FakeMarkdown(raw_markdown="one", fit_markdown="one"),
        ),
    ]
    fake_cls = install_crawler(results)

    content, pages = await fetch._fetch(
        ["https://dup.test/a", "https://dup.test/a/", "https://dup.test/a#frag"],
        config,
        registry,
    )

    # One crawl, one page, one heading — never two [Sn] blocks over one identity.
    assert fake_cls.calls[0].urls == ["https://dup.test/a"]
    assert len(pages) == 1
    assert registry.get(pages[0].source_id) is not None
    assert len(registry.all()) == 1
    assert content.count(f"## [{pages[0].source_id}]") == 1


async def test_content_is_truncated_at_the_cap_but_artifact_keeps_full_text(
    install_crawler, make_config
):
    cap = 50
    config = make_config(per_page_char_cap=cap)
    registry = SourceRegistry()
    long_markdown = "x" * 500
    results = [
        _FakeResult(
            "https://long.test",
            markdown=_FakeMarkdown(raw_markdown=long_markdown, fit_markdown=long_markdown),
        )
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(["https://long.test"], config, registry)

    assert len(content) < len(long_markdown)
    assert str(cap) in content
    assert pages[0].markdown == long_markdown


async def test_content_has_a_heading_for_every_url_including_failures(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://ok.test",
            markdown=_FakeMarkdown(raw_markdown="fine", fit_markdown="fine"),
        ),
        _FakeResult(
            "https://fail.test",
            error_message="server exploded",
            status_code=500,
            markdown=None,
        ),
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(["https://ok.test", "https://fail.test"], config, registry)

    for page in pages:
        assert f"## [{page.source_id}] {page.url}" in content
        assert page.outcome in content


async def test_config_limits_reach_the_crawl4ai_call(install_crawler, make_config):
    config = make_config(page_timeout_ms=1234, max_concurrency=3)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test",
            markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"),
        )
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry)

    assert len(fake_cls.calls) == 1
    recorded = fake_cls.calls[0]
    assert recorded.config.page_timeout == 1234
    assert recorded.dispatcher.max_session_permit == 3


async def test_dispatcher_is_memory_bounded_and_rate_limited(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry)

    dispatcher = fake_cls.calls[0].dispatcher
    # 75%, not crawl4ai's 90% default: each permit is a real browser page.
    assert dispatcher.memory_threshold_percent == 75.0
    # Not a retry count — 0.9.2 re-fetches nothing on a 429/503; this caps how many times a
    # domain's backoff delay doubles, and that sleep holds a concurrency permit.
    assert dispatcher.rate_limiter is not None
    assert dispatcher.rate_limiter.max_retries == 1


async def test_crawl4ai_logging_is_silenced_on_both_configs(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry)

    # crawl4ai defaults `verbose` to True on both configs and prints into our process.
    assert fake_cls.calls[0].config.verbose is False
    assert fake_cls.constructed_with[0].verbose is False


async def test_boilerplate_stripping_config_reaches_the_crawl4ai_call(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry)

    recorded = fake_cls.calls[0].config
    assert set(recorded.excluded_tags) >= {"nav", "header", "footer", "aside", "script"}
    assert isinstance(recorded.markdown_generator, DefaultMarkdownGenerator)
    assert isinstance(recorded.markdown_generator.content_filter, PruningContentFilter)


async def test_built_tool_exposes_the_pinned_contract_and_returns_content_and_artifact(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://tool.test",
            metadata={"title": "Tool Page"},
            markdown=_FakeMarkdown(raw_markdown="raw body", fit_markdown="clean body"),
        )
    ]
    install_crawler(results)

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


async def test_fit_markdown_is_preferred_over_raw_markdown(install_crawler, make_config):
    config = make_config()
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
    install_crawler(results)

    content, pages = await fetch._fetch(["https://pruned.test"], config, registry)

    assert pages[0].markdown == "The real article body."
    assert "Footer links" not in content


async def test_raw_markdown_is_used_when_fit_markdown_is_empty(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://unpruned.test",
            markdown=_FakeMarkdown(raw_markdown="Only raw survived.", fit_markdown=""),
        )
    ]
    install_crawler(results)

    _, pages = await fetch._fetch(["https://unpruned.test"], config, registry)

    assert pages[0].markdown == "Only raw survived."
    assert pages[0].outcome == "fetched"


async def test_title_from_result_metadata_reaches_the_page_and_the_registry(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://titled.test",
            metadata={"title": "An Article Title"},
            markdown=_FakeMarkdown(raw_markdown="body", fit_markdown="body"),
        )
    ]
    install_crawler(results)

    _, pages = await fetch._fetch(["https://titled.test"], config, registry)

    assert pages[0].title == "An Article Title"
    source = registry.get(pages[0].source_id)
    assert source is not None
    assert source.title == "An Article Title"


async def test_content_type_header_on_the_result_drives_non_html_classification(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://pdf.test/doc",
            response_headers={"Content-Type": "application/pdf"},
            markdown=_FakeMarkdown(raw_markdown="%PDF junk", fit_markdown="%PDF junk"),
        )
    ]
    install_crawler(results)

    _, pages = await fetch._fetch(["https://pdf.test/doc"], config, registry)

    # Exercises _content_type's case-insensitive header lookup through the real
    # pipeline — the classify() unit tests bypass it with a plain string.
    assert pages[0].outcome == "non_html"


async def test_input_url_with_no_result_reports_a_single_error_outcome(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    install_crawler([])

    _, pages = await fetch._fetch(["https://a.test"], config, registry)

    # This is the `None`-pairing branch (fetch.py:212-224), which was previously
    # untested; it must survive the removal of the positional fallback.
    assert len(pages) == 1
    assert pages[0].url == "https://a.test"
    assert pages[0].outcome == "error"
    assert pages[0].markdown == ""
    assert pages[0].error == "no result returned for this URL"


async def test_result_matching_no_input_url_never_supplies_another_urls_body(
    install_crawler, make_config
):
    # Supersedes the deleted test_result_whose_url_diff_from_input_paired, which asserted
    # the opposite: that an unrelated result could be handed to an input URL positionally.
    # A visible `error` is strictly safer than a plausible wrong citation.
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://redirected.test/final",
            markdown=_FakeMarkdown(
                raw_markdown="redirected content", fit_markdown="redirected content"
            ),
        )
    ]
    install_crawler(results)

    content, pages = await fetch._fetch(["https://original.test/start"], config, registry)

    assert len(pages) == 1  # R6 — exactly one outcome per input URL
    assert pages[0].url == "https://original.test/start"
    assert pages[0].outcome == "error"
    assert pages[0].markdown == ""
    assert "redirected content" not in content
