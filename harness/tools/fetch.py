"""Fetch many URLs concurrently through crawl4ai, with no single URL able to fail the batch.

Each URL is classified into a small outcome vocabulary (`FetchOutcome`) rather than
raising, so a blocked or timed-out page shows up as data for the model to reason about
instead of an exception that would sink the whole tool call. The model sees compact,
`[Sn]`-headed, boilerplate-stripped markdown capped per page; the artifact carries the
full, untruncated per-URL outcomes for anything downstream that needs them (e.g. citation
resolution via `harness.sources.SourceRegistry`).
"""

from typing import Literal

from crawl4ai import (  # type: ignore[import-untyped]
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    DefaultMarkdownGenerator,
    MemoryAdaptiveDispatcher,
    PruningContentFilter,
    RateLimiter,
)
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.sources import SourceRegistry, normalize_url

FetchOutcome = Literal["fetched", "blocked", "timeout", "non_html", "error"]

_BLOCKED_STATUSES = frozenset({403, 429, 503})
_EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "script", "style", "form", "noscript"]

# crawl4ai's own default is 90%, which on a box also hosting SearXNG and Chromium starts
# shedding work far too late. Each concurrent crawl is a real browser page, so 75% leaves
# headroom to finish the pages already in flight instead of pausing under real pressure.
_MEMORY_THRESHOLD_PERCENT = 75.0

# Despite the name, crawl4ai 0.9.2 re-fetches nothing on a 429/503: `update_delay` runs
# after the crawl returns and this value caps how many times a domain's backoff delay may
# double (`async_dispatcher.py:65-85`). That sleep is served while holding one of
# `max_concurrency`'s permits, so 1 — the tightest cap — gives the slot back to the rest of
# the batch soonest. It only bites when a batch holds 2+ URLs from one domain; either way a
# rate-limited page still surfaces as `blocked`.
_RATE_LIMIT_MAX_RETRIES = 1


def classify(
    status_code: int | None,
    error_message: str | None,
    content_type: str | None,
    markdown: str,
) -> FetchOutcome:
    """Classify one crawl result into the frozen outcome vocabulary.

    "Successful" is inferred from `error_message` being absent — this signature takes
    no `success` flag, so a `None` error_message with empty markdown is treated as a
    successful crawl of an empty (non-HTML) page, not an error.
    """
    if status_code in _BLOCKED_STATUSES:
        return "blocked"
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if content_type and "html" not in content_type.lower():
        return "non_html"
    if not error_message and not markdown.strip():
        return "non_html"
    if error_message:
        return "error"
    return "fetched"


class FetchedPage(BaseModel):
    """One URL's outcome: classification plus whatever content or error it produced."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    url: str
    outcome: FetchOutcome
    status_code: int | None
    title: str | None
    markdown: str
    error: str | None


def _content_type(result: object) -> str | None:
    """Case-insensitive lookup of `content-type` in `result.response_headers`."""
    headers = getattr(result, "response_headers", None) or {}
    for key, value in headers.items():
        if key.lower() == "content-type":
            return str(value)
    return None


def _markdown_of(result: object) -> str:
    """Prefer the boilerplate-stripped `fit_markdown`, falling back to `raw_markdown`."""
    markdown = getattr(result, "markdown", None)
    if markdown is None:
        return ""
    fit = getattr(markdown, "fit_markdown", None)
    if fit:
        return str(fit)
    raw = getattr(markdown, "raw_markdown", None)
    return str(raw) if raw else ""


def _title_of(result: object) -> str | None:
    """Read the page title out of `result.metadata`, which crawl4ai keys as `"title"`."""
    metadata = getattr(result, "metadata", None) or {}
    return metadata.get("title")


def _pair(urls: list[str], results: list[object]) -> list[tuple[str, object | None]]:
    """Pair each input URL with the result crawl4ai keyed to that exact URL.

    crawl4ai keeps the requested URL on `result.url` and puts a redirect destination on
    `redirected_url`, so an exact match is the only sound pairing. An input URL with no
    matching result pairs with `None` and is reported as an `error` rather than consuming
    an unclaimed result positionally — that fallback could attribute one page's body to
    another page's `[Sn]` citation marker, which is silent and unfixable downstream.

    One URL can legitimately yield two results: under critical memory pressure the
    dispatcher returns a "Requeued" placeholder AND re-queues the crawl
    (`async_dispatcher.py:289-293`), so the first of a URL's results is taken and that
    page may report `error` despite the retry later succeeding. Pre-existing behaviour,
    left alone deliberately — reordering to prefer a successful result would be new
    machinery for a case only reachable above 95% memory.
    """
    by_url: dict[str | None, list[object]] = {}
    for result in results:
        by_url.setdefault(getattr(result, "url", None), []).append(result)

    pairs: list[tuple[str, object | None]] = []
    for url in urls:
        bucket = by_url.get(url)
        pairs.append((url, bucket.pop(0) if bucket else None))
    return pairs


def _render(page: FetchedPage, cap: int) -> str:
    """Render one page's model-facing block: heading, outcome line, capped markdown."""
    lines = [f"## [{page.source_id}] {page.url}"]

    status_bits: list[str] = [page.outcome]
    if page.status_code is not None:
        status_bits.append(f"status {page.status_code}")
    if page.error:
        status_bits.append(page.error)
    lines.append(" — ".join(status_bits))

    text = page.markdown
    if len(text) > cap:
        text = text[:cap] + f"\n\n_[truncated at {cap} characters]_"
    lines.append(text)

    return "\n\n".join(lines)


async def _fetch(
    urls: list[str], config: HarnessConfig, registry: SourceRegistry
) -> tuple[str, list[FetchedPage]]:
    """Fetch every URL, returning model-facing markdown and the full per-URL artifact."""
    # The registry dedups by normalized URL, so crawl each canonical URL exactly once —
    # otherwise two spellings of one page (trailing slash, fragment, case) would render
    # duplicate [Sn] headings over independently-fetched, possibly different bodies.
    seen: set[str] = set()
    unique_urls: list[str] = []
    for url in urls:
        normalized = normalize_url(url)
        if normalized not in seen:
            seen.add(normalized)
            unique_urls.append(url)
    urls = unique_urls
    if not urls:
        return "", []

    run_config = CrawlerRunConfig(
        page_timeout=config.fetch.page_timeout_ms,
        excluded_tags=_EXCLUDED_TAGS,
        markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
        cache_mode=CacheMode.BYPASS,
        stream=False,
        verbose=False,
    )
    dispatcher = MemoryAdaptiveDispatcher(
        max_session_permit=config.fetch.max_concurrency,
        memory_threshold_percent=_MEMORY_THRESHOLD_PERCENT,
        rate_limiter=RateLimiter(max_retries=_RATE_LIMIT_MAX_RETRIES),
    )

    # verbose=False is deliberate: crawl4ai defaults it True and prints into our process.
    async with AsyncWebCrawler(config=BrowserConfig(verbose=False)) as crawler:
        raw_results = await crawler.arun_many(urls, config=run_config, dispatcher=dispatcher)
        results = list(raw_results)

    pages: list[FetchedPage] = []
    for url, result in _pair(urls, results):
        if result is None:
            source_id = registry.add(url)
            pages.append(
                FetchedPage(
                    source_id=source_id,
                    url=url,
                    outcome="error",
                    status_code=None,
                    title=None,
                    markdown="",
                    error="no result returned for this URL",
                )
            )
            continue

        markdown = _markdown_of(result)
        title = _title_of(result)
        status_code = getattr(result, "status_code", None)
        error_message = getattr(result, "error_message", None)
        outcome = classify(status_code, error_message, _content_type(result), markdown)
        source_id = registry.add(url, title=title)
        pages.append(
            FetchedPage(
                source_id=source_id,
                url=url,
                outcome=outcome,
                status_code=status_code,
                title=title,
                markdown=markdown,
                error=error_message,
            )
        )

    content = "\n\n".join(_render(page, config.fetch.per_page_char_cap) for page in pages)
    return content, pages


class FetchPagesInput(BaseModel):
    """Model-facing input schema for the `fetch_pages` tool."""

    model_config = ConfigDict(extra="forbid")

    urls: list[str] = Field(description="The URLs to fetch, in the order they should be reported.")


def build_fetch_tool(config: HarnessConfig, registry: SourceRegistry) -> BaseTool:
    """Build the `fetch_pages` tool, closing over `config` and the shared `registry`."""

    @tool("fetch_pages", args_schema=FetchPagesInput, response_format="content_and_artifact")
    async def fetch_pages(urls: list[str]) -> tuple[str, list[FetchedPage]]:
        """Fetch the given URLs and return boilerplate-stripped markdown for each.

        Failures (blocked, timed out, non-HTML, or otherwise unfetchable pages) are
        reported per URL with their outcome rather than raising, so one bad URL never
        fails the whole batch. URLs that are equivalent spellings of the same page
        (trailing slash, fragment, case) are fetched once and reported once.
        """
        return await _fetch(urls, config, registry)

    return fetch_pages
