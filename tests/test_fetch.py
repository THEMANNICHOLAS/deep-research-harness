"""Behavioral tests for harness.tools.fetch."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from crawl4ai import (  # type: ignore[import-untyped]
    BrowserConfig,
    DefaultMarkdownGenerator,
    PruningContentFilter,
)
from crawl4ai.async_crawler_strategy import (  # type: ignore[import-untyped]
    AsyncHTTPCrawlerStrategy,
)
from langchain_core.tools import BaseTool

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


def _make_fake_crawler_class(
    results: list[_FakeResult], behaviors: dict[str, list[object]] | None = None
) -> type:
    """Build a fake AsyncWebCrawler class serving per-URL `arun` calls.

    `results` are bucketed by URL once (the grouping `_pair` used to do) and the first
    match is popped per call, so every test that only passes a plain `results` list keeps
    working unchanged. `behaviors` lets a test script a multi-attempt sequence (retries,
    timeouts, hangs) for one URL without disturbing the rest: each entry is a `_FakeResult`
    (returned), an `Exception` instance (raised), or the string `"hang"` (sleeps far longer
    than any test deadline). A URL absent from `behaviors` falls back to the `results`
    bucket; exhausting a URL's `behaviors` list is a test bug and raises `AssertionError`.
    """
    by_url: dict[str, list[_FakeResult]] = {}
    for result in results:
        by_url.setdefault(result.url, []).append(result)
    scripted: dict[str, list[object]] = behaviors or {}

    class _FakeCrawler:
        constructed_with: list[dict[str, object]] = []
        calls: list[SimpleNamespace] = []
        # Phase 2 constructs a second (browser) AsyncWebCrawler from this same patched
        # class, so per-instance in_flight/max_in_flight keep the HTTP and browser
        # concurrency caps from conflating; instances is order-preserving construction order.
        instances: list["_FakeCrawler"] = []

        def __init__(self, **kwargs: object) -> None:
            _FakeCrawler.constructed_with.append(kwargs)
            self.in_flight = 0
            self.max_in_flight = 0
            _FakeCrawler.instances.append(self)

        async def __aenter__(self) -> "_FakeCrawler":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def arun(self, url: str, config: object = None) -> _FakeResult | None:
            _FakeCrawler.calls.append(SimpleNamespace(url=url, config=config))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                # Yields control at least once so overlapping arun calls actually interleave
                # under the semaphore, rather than each running to completion synchronously.
                await asyncio.sleep(0)
                if url in scripted:
                    if not scripted[url]:
                        raise AssertionError(f"{url}'s scripted behaviors were exhausted")
                    behavior = scripted[url].pop(0)
                    if isinstance(behavior, Exception):
                        raise behavior
                    if behavior == "hang":
                        await asyncio.sleep(3600)
                    return behavior  # type: ignore[return-value]
                bucket = by_url.get(url)
                return bucket.pop(0) if bucket else None
            finally:
                self.in_flight -= 1

    return _FakeCrawler


def _rendered(markdown: str, cap: int) -> str:
    """Render one fetched page's block through `_render`, for truncation assertions."""
    page = fetch.FetchedPage(
        source_id="S1",
        url="https://a.test",
        outcome="fetched",
        status_code=200,
        title=None,
        markdown=markdown,
        error=None,
    )
    return fetch._render(page, cap)


@pytest.fixture
def install_crawler(monkeypatch):
    """Patch fetch's AsyncWebCrawler with a fake serving canned results; returns the class."""

    def _install(
        results: list[_FakeResult], behaviors: dict[str, list[object]] | None = None
    ) -> type:
        fake_cls = _make_fake_crawler_class(results, behaviors)
        monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)
        return fake_cls

    return _install


@pytest.fixture
def install_head(monkeypatch):
    """Route fetch's PDF-precheck AsyncClient through a MockTransport running `handler`,
    mirroring tests/test_search.py's `_install` seam for the SearXNG client.
    """
    real = httpx.AsyncClient

    def _install(handler):
        def factory(**kwargs):
            return real(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr("harness.tools.fetch.httpx.AsyncClient", factory)

    return _install


@pytest.fixture(autouse=True)
def _default_head_response(install_head):
    """Every `_fetch()` call now issues a PDF-precheck HEAD, so a test that doesn't care
    about it (nearly all of them, pre-dating Phase 3) still needs to stay offline rather
    than hit the real network for `*.test` URLs. Defaults to a `text/html` reply so those
    tests keep proceeding to the fake crawler unchanged; a test exercising the precheck
    itself calls `install_head` again to override this default.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"})

    install_head(handler)


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
    assert [call.url for call in fake_cls.calls] == ["https://dup.test/a"]
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


def test_text_at_or_under_the_cap_is_returned_unchanged():
    cap = 100

    at_cap = _rendered("A" * 100, cap)
    under_cap = _rendered("A" * 40, cap)

    assert "A" * 100 in at_cap
    assert "truncated" not in at_cap
    assert "A" * 40 in under_cap
    assert "truncated" not in under_cap


def test_over_cap_text_ends_at_the_latest_paragraph_break():
    cap = 100
    text = "A" * 40 + "\n\n" + "B" * 40 + "\n\n" + "C" * 100

    rendered = _rendered(text, cap)

    assert "C" not in rendered
    assert ("A" * 40 + "\n\n" + "B" * 40) in rendered
    assert str(cap) in rendered


def test_a_heading_start_is_a_valid_truncation_boundary():
    cap = 100
    text = "A" * 70 + "\n# Later heading\n" + "B" * 100

    rendered = _rendered(text, cap)

    assert "# Later heading" not in rendered
    assert "A" * 70 in rendered


def test_text_with_no_boundary_before_the_cap_falls_back_to_a_hard_cut():
    cap = 100
    text = "A" * 500

    rendered = _rendered(text, cap)

    assert "A" * 100 in rendered
    assert "A" * 101 not in rendered
    assert "truncated" in rendered
    assert str(cap) in rendered


def test_an_early_boundary_is_taken_even_though_it_discards_most_of_the_allowance():
    # Supersedes test_a_boundary_too_early_to_be_worth_taking_falls_back_to_the_hard_cut:
    # the `_MIN_BOUNDARY_FRACTION` floor was removed, so the latest boundary always wins.
    cap = 100
    text = "A" * 10 + "\n\n" + "B" * 200

    rendered = _rendered(text, cap)

    assert "A" * 10 in rendered
    assert "B" not in rendered
    assert "truncated" in rendered


def test_a_heading_at_the_very_start_does_not_empty_the_block():
    cap = 100
    text = "# Title\n" + "A" * 200

    rendered = _rendered(text, cap)

    assert "# Title" in rendered
    assert "A" * 92 in rendered
    assert "truncated" in rendered


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
    config = make_config(page_timeout_ms=1234)
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
    assert fake_cls.calls[0].config.page_timeout == 1234


async def test_crawl4ai_logging_is_silenced_on_the_run_config(install_crawler, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry)

    # crawl4ai defaults `verbose` to True on CrawlerRunConfig and prints into our process.
    # The HTTP strategy/config exposes no separate verbose flag to silence.
    assert fake_cls.calls[0].config.verbose is False


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


async def test_crawler_is_constructed_with_the_http_strategy_not_a_browser(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a"))
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://a.test"], config, registry)

    assert len(fake_cls.constructed_with) == 1
    strategy = fake_cls.constructed_with[0]["crawler_strategy"]
    assert isinstance(strategy, AsyncHTTPCrawlerStrategy)


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
    fake_cls = install_crawler([])

    _, pages = await fetch._fetch(["https://a.test"], config, registry)

    # The `None`-return branch of `_fetch_one`, exercised when a URL has no matching result.
    assert len(pages) == 1
    assert pages[0].url == "https://a.test"
    assert pages[0].outcome == "error"
    assert pages[0].markdown == ""
    assert pages[0].error == "no result returned for this URL"
    # A statusless `error` counts as a network error, so this path now exhausts the retry
    # budget rather than reporting after one attempt as it did under `arun_many`.
    assert len(fake_cls.calls) == config.fetch.max_retries + 1


async def test_a_hung_url_times_out_without_blocking_a_sibling_url(install_crawler, make_config):
    config = make_config(http_deadline_ms=20, max_retries=1)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://fast.test",
            markdown=_FakeMarkdown(raw_markdown="fine", fit_markdown="fine"),
        ),
    ]
    fake_cls = install_crawler(results, behaviors={"https://hang.test": ["hang", "hang"]})

    _, pages = await fetch._fetch(["https://hang.test", "https://fast.test"], config, registry)

    by_url = {page.url: page for page in pages}
    assert by_url["https://hang.test"].outcome == "timeout"
    assert by_url["https://fast.test"].outcome == "fetched"
    # Pins the contract's "Retryable: timeout" line: max_retries=1 means the hung URL is
    # attempted twice, not once. Without this the test passes whatever the retry rule does.
    assert len([c for c in fake_cls.calls if c.url == "https://hang.test"]) == 2


async def test_5xx_is_retried_to_the_budget_404_is_not_and_recovery_stops_early(
    install_crawler, make_config
):
    config = make_config(max_retries=2)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://flaky.test": [
            _FakeResult(
                "https://flaky.test", error_message="HTTP 500: Server Error", status_code=None
            ),
            _FakeResult(
                "https://flaky.test", error_message="HTTP 500: Server Error", status_code=None
            ),
            _FakeResult(
                "https://flaky.test", error_message="HTTP 500: Server Error", status_code=None
            ),
        ],
        "https://notfound.test": [
            _FakeResult(
                "https://notfound.test", error_message="HTTP 404: Not Found", status_code=None
            ),
        ],
        "https://recovers.test": [
            _FakeResult(
                "https://recovers.test", error_message="HTTP 500: Server Error", status_code=None
            ),
            _FakeResult(
                "https://recovers.test",
                markdown=_FakeMarkdown(raw_markdown="recovered", fit_markdown="recovered"),
            ),
        ],
    }
    fake_cls = install_crawler([], behaviors=behaviors)

    _, pages = await fetch._fetch(
        ["https://flaky.test", "https://notfound.test", "https://recovers.test"],
        config,
        registry,
    )

    assert len([c for c in fake_cls.calls if c.url == "https://flaky.test"]) == 3
    assert len([c for c in fake_cls.calls if c.url == "https://notfound.test"]) == 1
    assert len([c for c in fake_cls.calls if c.url == "https://recovers.test"]) == 2

    by_url = {page.url: page for page in pages}
    assert by_url["https://recovers.test"].outcome == "fetched"


async def test_a_403_recovered_from_the_error_message_classifies_as_blocked(
    install_crawler, make_config
):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult("https://blocked.test", error_message="HTTP 403: Forbidden", status_code=None)
    ]
    install_crawler(results)

    _, pages = await fetch._fetch(["https://blocked.test"], config, registry)

    assert pages[0].status_code == 403
    assert pages[0].outcome == "blocked"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("HTTP 500: Server Error", 500),
        (None, None),
        ("some other failure with no status", None),
    ],
)
def test_status_from_error_parses_the_http_status_or_returns_none(error, expected):
    assert fetch._status_from_error(error) == expected


def test_status_from_error_keeps_the_exact_http_code_colon_message_shape():
    # Risk !#1: pins the exact "HTTP <code>: ..." format crawl4ai 0.9.2 raises internally;
    # a library upgrade that changes it must fail here, not silently stop recovering statuses.
    assert fetch._status_from_error("HTTP 403: Forbidden") == 403


def test_status_from_error_parses_the_wrapped_message_production_actually_sees():
    # The bare string above never reaches us: `AsyncWebCrawler.arun` catches the internal
    # `HTTPStatusError` and stores it wrapped in a traceback blob with trailing code context.
    # Phase 4's blocklist gate keys on this number, so the wrapper — not just the inner
    # format — is what must keep parsing. Note the parse relies on "Error:" preceding
    # "Code context:", since the regex takes the first match in the blob.
    wrapped = (
        "Unexpected error in _crawl_web at line 2461 in wrapper "
        "(crawl4ai/async_crawler_strategy.py):\n"
        "Error: HTTP 403: Forbidden\n\n"
        "Code context:\n"
        "  2459     if response.status >= 400:\n"
        "  2460         message = await response.text()\n"
        "  2461 ->      raise HTTPStatusError(response.status, message)\n"
    )
    assert fetch._status_from_error(wrapped) == 403


async def test_concurrency_never_exceeds_the_configured_cap(install_crawler, make_config):
    config = make_config(http_concurrency=2)
    registry = SourceRegistry()
    urls = [f"https://c{n}.test" for n in range(5)]
    results = [
        _FakeResult(url, markdown=_FakeMarkdown(raw_markdown="ok", fit_markdown="ok"))
        for url in urls
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(urls, config, registry)

    assert fake_cls.instances[0].max_in_flight == 2


async def test_output_order_follows_input_order_not_completion_order(install_crawler, make_config):
    # Replaces the deleted `_pair`-pinning tests (Reconciliation #1): `asyncio.gather`
    # preserves input order structurally, so this pins that guarantee instead.
    config = make_config(max_retries=2)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://slow.test": [
            _FakeResult(
                "https://slow.test", error_message="HTTP 500: Server Error", status_code=None
            ),
            _FakeResult(
                "https://slow.test",
                markdown=_FakeMarkdown(raw_markdown="slow body", fit_markdown="slow body"),
            ),
        ],
    }
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="a", fit_markdown="a")),
        _FakeResult("https://c.test", markdown=_FakeMarkdown(raw_markdown="c", fit_markdown="c")),
    ]
    install_crawler(results, behaviors=behaviors)

    _, pages = await fetch._fetch(
        ["https://a.test", "https://slow.test", "https://c.test"], config, registry
    )

    assert [page.url for page in pages] == ["https://a.test", "https://slow.test", "https://c.test"]
    assert [page.source_id for page in pages] == ["S1", "S2", "S3"]


async def test_a_call_over_the_url_limit_is_rejected_before_any_fetch(install_crawler, make_config):
    # R7 / D2 — the schema rejects the call before any fetch happens, and the rejection comes
    # back as a recoverable tool message rather than an exception escaping the call (risk #3),
    # so the caller can resend fewer URLs.
    config = make_config()
    limit = config.fetch.max_urls_per_call
    registry = SourceRegistry()
    fake_cls = install_crawler([])
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        {
            "name": "fetch_pages",
            "args": {"urls": [f"https://over{n}.test" for n in range(1, limit + 2)]},
            "id": "over-limit-1",
            "type": "tool_call",
        }
    )

    assert message.status == "error"
    # The cap is what rejected the call, not some unrelated validation failure.
    assert f"at most {limit} items" in message.content
    assert f"At most {limit} URLs" in message.content
    assert fake_cls.calls == []


async def test_duplicate_urls_still_count_toward_the_limit(install_crawler, make_config):
    # R7 sub-bullet: the limit counts URLs as submitted, not after deduplication. Six URLs of
    # which two are equivalent spellings is rejected, not silently collapsed to four and
    # accepted — the cap lives in the schema, which runs before `_fetch` dedups.
    config = make_config(max_urls_per_call=5)
    registry = SourceRegistry()
    fake_cls = install_crawler([])
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        {
            "name": "fetch_pages",
            "args": {
                "urls": [
                    "https://dup.test/a",
                    "https://dup.test/b",
                    "https://dup.test/c",
                    "https://dup.test/d",
                    "https://dup.test/a/",  # same page as /a once normalized
                    "https://dup.test/b#frag",  # same page as /b once normalized
                ]
            },
            "id": "over-limit-dupes",
            "type": "tool_call",
        }
    )

    assert message.status == "error"
    assert fake_cls.calls == []


async def test_a_malformed_call_is_reported_as_itself_not_as_an_over_limit_call(
    install_crawler, make_config
):
    # The `handle_validation_error` hook swallows EVERY validation failure for this tool, so it
    # must report the real cause — a fixed over-limit string would leave a wrong type looking
    # like a too-long list and give the caller nothing to correct.
    config = make_config()
    registry = SourceRegistry()
    fake_cls = install_crawler([])
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        {
            "name": "fetch_pages",
            "args": {"urls": "https://not-a-list.test"},
            "id": "malformed-1",
            "type": "tool_call",
        }
    )

    assert message.status == "error"
    assert "valid list" in message.content
    assert fake_cls.calls == []


def test_both_prose_surfaces_state_the_url_limit(make_config):
    config = make_config()
    registry = SourceRegistry()
    fetch_pages = fetch.build_fetch_tool(config, registry)

    expected = str(config.fetch.max_urls_per_call)

    assert expected in fetch_pages.description
    schema = fetch_pages.args_schema.model_json_schema()
    assert expected in schema["properties"]["urls"]["description"]


def test_the_schema_url_limit_follows_the_configured_value(make_config):
    registry = SourceRegistry()
    low_config = make_config(max_urls_per_call=2)
    high_config = make_config(max_urls_per_call=7)

    low_tool = fetch.build_fetch_tool(low_config, registry)
    high_tool = fetch.build_fetch_tool(high_config, registry)

    low_schema = low_tool.args_schema.model_json_schema()
    high_schema = high_tool.args_schema.model_json_schema()

    assert low_schema["properties"]["urls"]["maxItems"] == 2
    assert high_schema["properties"]["urls"]["maxItems"] == 7


async def test_a_call_at_exactly_the_limit_fetches_every_url(install_crawler, make_config):
    # Survival guard: proves the cap does not break the at-limit case.
    config = make_config(max_urls_per_call=2)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://limit1.test",
            markdown=_FakeMarkdown(raw_markdown="one", fit_markdown="one"),
        ),
        _FakeResult(
            "https://limit2.test",
            markdown=_FakeMarkdown(raw_markdown="two", fit_markdown="two"),
        ),
    ]
    install_crawler(results)
    fetch_pages = fetch.build_fetch_tool(config, registry)

    message = await fetch_pages.ainvoke(
        {
            "name": "fetch_pages",
            "args": {"urls": ["https://limit1.test", "https://limit2.test"]},
            "id": "at-limit-1",
            "type": "tool_call",
        }
    )

    assert [page.url for page in message.artifact] == [
        "https://limit1.test",
        "https://limit2.test",
    ]
    assert "## [S1] https://limit1.test" in message.content
    assert "## [S2] https://limit2.test" in message.content


async def test_a_thin_result_escalates_once_and_the_browser_result_wins(
    install_crawler, make_config
):
    config = make_config(min_markdown_words=5)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://shell.test": [
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(raw_markdown="thin", fit_markdown="thin"),
            ),
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(
                    raw_markdown="a rich rendered body with plenty of real words",
                    fit_markdown="a rich rendered body with plenty of real words",
                ),
            ),
        ],
    }
    fake_cls = install_crawler([], behaviors=behaviors)

    _, pages = await fetch._fetch(["https://shell.test"], config, registry)

    assert pages[0].markdown == "a rich rendered body with plenty of real words"
    assert len([c for c in fake_cls.calls if c.url == "https://shell.test"]) == 2
    assert len(fake_cls.constructed_with) == 2
    assert isinstance(fake_cls.constructed_with[1]["config"], BrowserConfig)


async def test_a_rich_result_never_escalates_and_constructs_no_browser(
    install_crawler, make_config
):
    config = make_config(min_markdown_words=5)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://rich.test",
            markdown=_FakeMarkdown(
                raw_markdown="plenty of real words here already",
                fit_markdown="plenty of real words here already",
            ),
        ),
    ]
    fake_cls = install_crawler(results)

    await fetch._fetch(["https://rich.test"], config, registry)

    assert len([c for c in fake_cls.calls if c.url == "https://rich.test"]) == 1
    assert len(fake_cls.constructed_with) == 1


async def test_failure_outcomes_do_not_escalate(install_crawler, make_config):
    config = make_config(min_markdown_words=5, http_deadline_ms=20, max_retries=1)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://hang.test": ["hang", "hang"],
        "https://notfound.test": [
            _FakeResult(
                "https://notfound.test", error_message="HTTP 404: Not Found", status_code=None
            ),
        ],
        "https://blocked.test": [
            _FakeResult(
                "https://blocked.test", error_message="HTTP 403: Forbidden", status_code=None
            ),
        ],
    }
    fake_cls = install_crawler([], behaviors=behaviors)

    _, pages = await fetch._fetch(
        ["https://hang.test", "https://notfound.test", "https://blocked.test"], config, registry
    )

    by_url = {page.url: page for page in pages}
    assert by_url["https://hang.test"].outcome == "timeout"
    assert by_url["https://notfound.test"].outcome == "error"
    assert by_url["https://blocked.test"].outcome == "blocked"
    # Neither a timeout, a 404, nor a blocked outcome is escalatable, so thinness alone never
    # triggers escalation — only one (HTTP) crawler is ever constructed.
    assert len(fake_cls.constructed_with) == 1


async def test_empty_but_html_non_html_result_escalates(install_crawler, make_config):
    # Reconciliation #2: a 200 text/html page with empty generated markdown classifies as
    # `non_html` (classify()'s existing behavior), not `fetched` — but it is the canonical
    # JS-shell case (`<div id="root"></div>`) and must still escalate.
    config = make_config(min_markdown_words=5)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://shell.test": [
            _FakeResult(
                "https://shell.test",
                response_headers={"Content-Type": "text/html"},
                markdown=_FakeMarkdown(raw_markdown="", fit_markdown=""),
            ),
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(
                    raw_markdown="a rich rendered body with plenty of real words",
                    fit_markdown="a rich rendered body with plenty of real words",
                ),
            ),
        ],
    }
    fake_cls = install_crawler([], behaviors=behaviors)

    _, pages = await fetch._fetch(["https://shell.test"], config, registry)

    assert pages[0].markdown == "a rich rendered body with plenty of real words"
    assert len([c for c in fake_cls.calls if c.url == "https://shell.test"]) == 2
    assert len(fake_cls.constructed_with) == 2


async def test_empty_pdf_non_html_result_does_not_escalate(install_crawler, make_config):
    # A genuine non-HTML resource (PDF) with empty markdown must stay `non_html` and never
    # trigger a browser launch, even though it is also "thin".
    config = make_config(min_markdown_words=5)
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://pdf.test/doc",
            response_headers={"Content-Type": "application/pdf"},
            markdown=_FakeMarkdown(raw_markdown="", fit_markdown=""),
        )
    ]
    fake_cls = install_crawler(results)

    _, pages = await fetch._fetch(["https://pdf.test/doc"], config, registry)

    assert pages[0].outcome == "non_html"
    assert len([c for c in fake_cls.calls if c.url == "https://pdf.test/doc"]) == 1
    assert len(fake_cls.constructed_with) == 1


async def test_empty_result_with_no_content_type_header_escalates(install_crawler, make_config):
    # No `content-type` header at all still reads as "looks like HTML" — `content_type` is
    # `None`, not a non-HTML type — so it escalates too.
    config = make_config(min_markdown_words=5)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://noheader.test": [
            _FakeResult(
                "https://noheader.test",
                response_headers=None,
                markdown=_FakeMarkdown(raw_markdown="", fit_markdown=""),
            ),
            _FakeResult(
                "https://noheader.test",
                markdown=_FakeMarkdown(
                    raw_markdown="a rich rendered body with plenty of real words",
                    fit_markdown="a rich rendered body with plenty of real words",
                ),
            ),
        ],
    }
    fake_cls = install_crawler([], behaviors=behaviors)

    _, pages = await fetch._fetch(["https://noheader.test"], config, registry)

    assert pages[0].markdown == "a rich rendered body with plenty of real words"
    assert len([c for c in fake_cls.calls if c.url == "https://noheader.test"]) == 2
    assert len(fake_cls.constructed_with) == 2


async def test_escalation_run_config_uses_browser_deadline_and_render_aware_wait(
    install_crawler, make_config
):
    # Reconciliation #3: the browser attempt must get its own CrawlerRunConfig — its
    # page_timeout aligned to browser_deadline_ms (not the HTTP page_timeout_ms) and a
    # render-aware wait, while the HTTP attempt's config is untouched.
    config = make_config(min_markdown_words=5, page_timeout_ms=1234, browser_deadline_ms=20000)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://shell.test": [
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(raw_markdown="thin", fit_markdown="thin"),
            ),
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(
                    raw_markdown="a rich rendered body with plenty of real words",
                    fit_markdown="a rich rendered body with plenty of real words",
                ),
            ),
        ],
    }
    fake_cls = install_crawler([], behaviors=behaviors)

    await fetch._fetch(["https://shell.test"], config, registry)

    calls = [c for c in fake_cls.calls if c.url == "https://shell.test"]
    assert len(calls) == 2
    http_config, browser_config = calls[0].config, calls[1].config
    assert http_config.page_timeout == 1234
    assert browser_config.page_timeout == 20000
    assert browser_config.wait_until == "networkidle"
    assert http_config.wait_until != "networkidle"


async def test_an_escalation_exceeding_its_deadline_yields_timeout_not_a_hang(
    install_crawler, make_config
):
    config = make_config(min_markdown_words=5, browser_deadline_ms=20)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://shell.test": [
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(raw_markdown="thin", fit_markdown="thin"),
            ),
            "hang",
        ],
    }
    install_crawler([], behaviors=behaviors)

    _, pages = await asyncio.wait_for(
        fetch._fetch(["https://shell.test"], config, registry), timeout=5
    )

    assert pages[0].outcome == "timeout"
    assert pages[0].markdown == ""


async def test_escalation_does_not_consume_the_retry_budget(install_crawler, make_config):
    config = make_config(min_markdown_words=5, max_retries=2)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://flaky.test": [
            _FakeResult(
                "https://flaky.test", error_message="HTTP 500: Server Error", status_code=None
            ),
            _FakeResult(
                "https://flaky.test", error_message="HTTP 500: Server Error", status_code=None
            ),
            _FakeResult(
                "https://flaky.test", error_message="HTTP 500: Server Error", status_code=None
            ),
        ],
        "https://shell.test": [
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(raw_markdown="thin", fit_markdown="thin"),
            ),
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(
                    raw_markdown="a rich rendered body with plenty of words",
                    fit_markdown="a rich rendered body with plenty of words",
                ),
            ),
        ],
    }
    fake_cls = install_crawler([], behaviors=behaviors)

    _, pages = await fetch._fetch(["https://flaky.test", "https://shell.test"], config, registry)

    assert (
        len([c for c in fake_cls.calls if c.url == "https://flaky.test"])
        == config.fetch.max_retries + 1
    )
    assert len([c for c in fake_cls.calls if c.url == "https://shell.test"]) == 2

    by_url = {page.url: page for page in pages}
    assert by_url["https://flaky.test"].outcome == "error"
    assert by_url["https://shell.test"].markdown == "a rich rendered body with plenty of words"


async def test_browser_concurrency_never_exceeds_its_configured_cap(install_crawler, make_config):
    config = make_config(min_markdown_words=5, browser_concurrency=2, http_concurrency=10)
    registry = SourceRegistry()
    urls = [f"https://shell{n}.test" for n in range(5)]
    behaviors: dict[str, list[object]] = {
        url: [
            _FakeResult(url, markdown=_FakeMarkdown(raw_markdown="thin", fit_markdown="thin")),
            _FakeResult(
                url,
                markdown=_FakeMarkdown(
                    raw_markdown="a rich rendered body with plenty of words",
                    fit_markdown="a rich rendered body with plenty of words",
                ),
            ),
        ]
        for url in urls
    }
    fake_cls = install_crawler([], behaviors=behaviors)

    await fetch._fetch(urls, config, registry)

    assert fake_cls.instances[1].max_in_flight == 2
    assert fake_cls.instances[0].max_in_flight == 5


async def test_pdf_head_short_circuits_without_fetching_the_body(
    install_crawler, install_head, make_config
):
    # R1's PDF case: the body is never fetched, and the non_html result can't escalate.
    config = make_config()
    registry = SourceRegistry()
    fake_cls = install_crawler([])

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"})

    install_head(handler)

    _, pages = await fetch._fetch(["https://pdf.test/doc.pdf"], config, registry)

    assert pages[0].outcome == "non_html"
    assert pages[0].markdown == ""
    assert fake_cls.calls == []
    # Only the HTTP crawler is ever constructed — no browser escalation for a PDF.
    assert len(fake_cls.constructed_with) == 1


async def test_html_head_proceeds_to_a_normal_fetch(install_crawler, install_head, make_config):
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test", markdown=_FakeMarkdown(raw_markdown="body", fit_markdown="body")
        )
    ]
    fake_cls = install_crawler(results)

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"})

    install_head(handler)

    _, pages = await fetch._fetch(["https://a.test"], config, registry)

    assert pages[0].outcome == "fetched"
    assert len(fake_cls.calls) == 1


async def test_malformed_url_does_not_sink_the_whole_batch(install_crawler, make_config):
    # httpx raises InvalidURL (NOT a subclass of HTTPError) while parsing the URL, before
    # any transport runs — so the precheck's except clause has to be wider than httpx's own
    # error tree. The module contract is that no single URL can fail the batch, and the
    # model supplies these URLs, so a malformed one is expected traffic.
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://ok.test", markdown=_FakeMarkdown(raw_markdown="body", fit_markdown="body")
        )
    ]
    install_crawler(results)

    _, pages = await fetch._fetch(["http://[::1", "https://ok.test"], config, registry)

    by_url = {page.url: page for page in pages}
    assert by_url["https://ok.test"].outcome == "fetched"
    assert by_url["http://[::1"].outcome == "error"


@pytest.mark.parametrize("kind", ["transport_error", "timeout", "rejected"])
async def test_head_failure_falls_through_to_a_normal_fetch(
    install_crawler, install_head, make_config, kind
):
    # Risk !#3's "no worse than today" guarantee: a HEAD that errors, times out, or is
    # rejected must never turn into a hard failure — it proceeds to a normal fetch.
    config = make_config()
    registry = SourceRegistry()
    results = [
        _FakeResult(
            "https://a.test", markdown=_FakeMarkdown(raw_markdown="body", fit_markdown="body")
        )
    ]
    fake_cls = install_crawler(results)

    def handler(request):
        if kind == "transport_error":
            raise httpx.ConnectError("refused")
        if kind == "timeout":
            raise httpx.ConnectTimeout("timed out")
        # A 405 rejection, with a content-type that would otherwise look like a PDF —
        # the non-2xx status alone must be enough to fall through.
        return httpx.Response(405, headers={"content-type": "application/pdf"})

    install_head(handler)

    _, pages = await fetch._fetch(["https://a.test"], config, registry)

    assert pages[0].outcome == "fetched"
    assert len(fake_cls.calls) == 1


async def test_downloads_path_is_pinned_on_both_crawlers(
    install_crawler, install_head, make_config, tmp_path
):
    downloads_dir = str(tmp_path / "downloads")
    config = make_config(downloads_dir=downloads_dir, min_markdown_words=5)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://shell.test": [
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(raw_markdown="thin", fit_markdown="thin"),
            ),
            _FakeResult(
                "https://shell.test",
                markdown=_FakeMarkdown(
                    raw_markdown="a rich rendered body with plenty of words",
                    fit_markdown="a rich rendered body with plenty of words",
                ),
            ),
        ],
    }
    fake_cls = install_crawler([], behaviors=behaviors)

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"})

    install_head(handler)

    await fetch._fetch(["https://shell.test"], config, registry)

    http_strategy = fake_cls.constructed_with[0]["crawler_strategy"]
    assert http_strategy.browser_config.downloads_path == downloads_dir

    browser_config = fake_cls.constructed_with[1]["config"]
    assert browser_config.downloads_path == downloads_dir


async def test_precheck_runs_once_per_url_not_per_attempt(
    install_crawler, install_head, make_config
):
    # Pins where the precheck sits relative to the retry loop: a URL whose fetch attempts
    # are 5xx to the retry budget must still issue exactly one HEAD.
    config = make_config(max_retries=2)
    registry = SourceRegistry()
    behaviors: dict[str, list[object]] = {
        "https://flaky.test": [
            _FakeResult(
                "https://flaky.test", error_message="HTTP 500: Server Error", status_code=None
            ),
            _FakeResult(
                "https://flaky.test", error_message="HTTP 500: Server Error", status_code=None
            ),
            _FakeResult(
                "https://flaky.test", error_message="HTTP 500: Server Error", status_code=None
            ),
        ],
    }
    fake_cls = install_crawler([], behaviors=behaviors)
    captured_requests = []

    def handler(request):
        captured_requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/html"})

    install_head(handler)

    await fetch._fetch(["https://flaky.test"], config, registry)

    assert len(fake_cls.calls) == config.fetch.max_retries + 1
    assert len(captured_requests) == 1
