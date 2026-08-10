"""Fetch many URLs concurrently through crawl4ai, with no single URL able to fail the batch.

Each URL is classified into a small outcome vocabulary (`FetchOutcome`) rather than
raising, so a blocked or timed-out page shows up as data for the model to reason about
instead of an exception that would sink the whole tool call. The model sees compact,
`[Sn]`-headed, boilerplate-stripped markdown capped per page; the artifact carries the
full, untruncated per-URL outcomes for anything downstream that needs them (e.g. citation
resolution via `harness.sources.SourceRegistry`).
"""

import sys
from pathlib import Path
from typing import Literal

from crawl4ai import (  # type: ignore[import-untyped]
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    DefaultMarkdownGenerator,
    MemoryAdaptiveDispatcher,
    PruningContentFilter,
)
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from harness.config import BrowserSettings, HarnessConfig
from harness.sources import SourceRegistry, normalize_url

FetchOutcome = Literal["fetched", "blocked", "timeout", "non_html", "error"]

_BLOCKED_STATUSES = frozenset({403, 429, 503})
_EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "script", "style", "form", "noscript"]

# The single home for this policy string: `_write_source_file` writes it as the first
# line of any non-`fetched` source's captured file, and `harness/report.py` imports it
# to judge, from that same captured file, whether a registered source is usable evidence
# (CLAUDE.md: a constant or policy statement lives in exactly one place).
FETCH_FAILED_PREFIX = "FETCH FAILED: "


def _sources_dir(config: HarnessConfig) -> Path:
    """The one place the frozen `<workspace_dir>/sources` layout is built."""
    return config.agent.workspace_dir / "sources"


def build_browser_config(settings: BrowserSettings) -> BrowserConfig:
    """Map the harness's browser backend vocabulary onto crawl4ai's `browser_mode`.

    `settings.backend` is the harness's vocabulary (`"lightpanda"` / `"playwright"`);
    `browser_mode` is crawl4ai's. This function is the only place the two are mapped.
    """
    if settings.backend == "lightpanda":
        return BrowserConfig(browser_mode="cdp", cdp_url=settings.cdp_url)
    return BrowserConfig()


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
    """Pair each input URL with its crawl result, tolerating a redirect-renamed URL.

    First pass: match each input URL against a dict keyed by `result.url` — the common
    case. Second pass: any input URL left unmatched consumes the next still-unmatched
    result in input order — this is what survives a redirect that leaves `result.url`
    different from what was requested. An input URL with nothing left to match against
    pairs with `None`.
    """
    by_url: dict[str | None, list[object]] = {}
    for result in results:
        by_url.setdefault(getattr(result, "url", None), []).append(result)

    exact_matches: dict[int, object] = {}
    claimed_ids: set[int] = set()
    for index, url in enumerate(urls):
        bucket = by_url.get(url)
        if bucket:
            match = bucket.pop(0)
            exact_matches[index] = match
            claimed_ids.add(id(match))

    leftovers = iter(r for r in results if id(r) not in claimed_ids)

    pairs: list[tuple[str, object | None]] = []
    for index, url in enumerate(urls):
        if index in exact_matches:
            pairs.append((url, exact_matches[index]))
        else:
            pairs.append((url, next(leftovers, None)))
    return pairs


def _write_source_file(sources_dir: Path, page: FetchedPage) -> None:
    """Write `page`'s full-text capture to `<sources_dir>/<source_id>.md`.

    A `fetched` page gets its full untruncated markdown; any other outcome gets a stub
    whose first line names the outcome (`FETCH FAILED: <outcome>`) so Phase 6 can treat
    it as unusable without parsing further. Overwrites freely — a refetched URL reuses
    its registry ID and rewrites its file rather than duplicating it (D10).

    A write failure here degrades to a skipped file, never an exception into the model —
    Phase 6 treats a missing source file exactly as it treats a stub.
    """
    path = sources_dir / f"{page.source_id}.md"
    if page.outcome == "fetched":
        heading = page.title or page.url
        text = (
            f"# {page.source_id}: {heading}\n\n"
            f"- URL: {page.url}\n"
            f"- Outcome: fetched\n\n"
            f"{page.markdown}"
        )
    else:
        lines = [
            f"{FETCH_FAILED_PREFIX}{page.outcome}",
            "",
            f"- URL: {page.url}",
            f"- Source: {page.source_id}",
        ]
        if page.status_code is not None:
            lines.append(f"- Status: {page.status_code}")
        if page.error:
            lines.append(f"- Error: {page.error}")
        text = "\n".join(lines)

    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(
            f"warning: failed to write source file for {page.source_id} ({page.url}) "
            f"to {path}: {exc}",
            file=sys.stderr,
        )


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
    )
    dispatcher = MemoryAdaptiveDispatcher(max_session_permit=config.fetch.max_concurrency)

    async with AsyncWebCrawler(config=build_browser_config(config.browser)) as crawler:
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

    sources_dir = _sources_dir(config)
    for page in pages:
        _write_source_file(sources_dir, page)

    content = "\n\n".join(_render(page, config.fetch.per_page_char_cap) for page in pages)
    return content, pages


def build_fetch_tool(config: HarnessConfig, registry: SourceRegistry) -> BaseTool:
    """Build the `fetch_pages` tool, closing over `config` and the shared `registry`.

    Creates `<workspace_dir>/sources` up front, so an unwritable workspace fails at
    startup — before any research is spent — rather than silently losing captures
    mid-run.
    """
    _sources_dir(config).mkdir(parents=True, exist_ok=True)

    class FetchPagesInput(BaseModel):
        """Model-facing input schema for the `fetch_pages` tool."""

        model_config = ConfigDict(extra="forbid")

        urls: list[str] = Field(
            max_length=config.fetch.max_urls_per_call,
            description=(
                "The URLs to fetch, in the order they should be reported. "
                f"At most {config.fetch.max_urls_per_call} URLs per call."
            ),
        )

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
