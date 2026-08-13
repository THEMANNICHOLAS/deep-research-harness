"""Fetch many URLs concurrently through crawl4ai; no single URL can fail the batch.

Each URL is classified into `FetchOutcome` rather than raising. The model sees compact,
`[Sn]`-headed, boilerplate-stripped markdown capped per page; the artifact carries the
full untruncated outcomes for downstream use (e.g. `harness.sources.SourceRegistry`).
"""

import re
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
    RateLimiter,
)
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.sources import SourceRegistry, normalize_url

FetchOutcome = Literal["fetched", "blocked", "timeout", "non_html", "error"]

_BLOCKED_STATUSES = frozenset({403, 429, 503})
_EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "script", "style", "form", "noscript"]

# 75%, not crawl4ai's default 90%: each concurrent crawl is a real browser page, and this
# box also hosts SearXNG and Chromium.
_MEMORY_THRESHOLD_PERCENT = 75.0

# The single home for this policy string: `_write_source_file` writes it as the first
# line of any non-`fetched` source's captured file, and `harness/report.py` imports it
# to judge, from that same captured file, whether a registered source is usable evidence
# (CLAUDE.md: a constant or policy statement lives in exactly one place).
FETCH_FAILED_PREFIX = "FETCH FAILED: "


def is_failed_capture(source_text: str) -> bool:
    """Whether a captured source file's text is a failure stub rather than real content.

    The single home for READING the policy `FETCH_FAILED_PREFIX` writes, as opposed to
    the prefix itself. `harness/report.py` (is this source usable evidence?) and
    `harness/verify.py` (can this source settle a claim?) both ask the same question and
    had each implemented "split the first line, test the prefix" separately (PR #4
    review, Minor) — a change to the stub shape had to land in two places or leave the
    two disagreeing about which sources count as evidence.
    """
    return source_text.split("\n", 1)[0].startswith(FETCH_FAILED_PREFIX)


def _sources_dir(config: HarnessConfig, registry: SourceRegistry) -> Path:
    """The one place the `<workspace_dir>/sources/<run_id>` layout is built."""
    return config.agent.workspace_dir / "sources" / registry.run_id


# Despite the name, crawl4ai 0.9.2 re-fetches nothing — this caps how many times a domain's
# backoff delay may double. See @docs/plans/PLAN-crawler-refinement.md Reconciliation #1.
_RATE_LIMIT_MAX_RETRIES = 1

# crawl4ai hands us flat strings with no heading tree, so a cut boundary has to be
# found in the text itself.
_HEADING_LINE = re.compile(r"^#{1,6} ", re.MULTILINE)


def classify(
    status_code: int | None,
    error_message: str | None,
    content_type: str | None,
    markdown: str,
) -> FetchOutcome:
    """Classify one crawl result into the frozen outcome vocabulary.

    Success is inferred from `error_message` being absent — there is no `success` flag,
    so no error plus empty markdown means an empty (non-HTML) page, not an error.
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
    metadata = getattr(result, "metadata", None) or {}
    return metadata.get("title")


def _pair(urls: list[str], results: list[object]) -> list[tuple[str, object | None]]:
    """Pair each URL with the result crawl4ai keyed to it exactly.

    Never positional: an unclaimed result could attribute one page's body to another's
    `[Sn]`. No match pairs with `None` and reports `error`. One URL can yield two results
    under memory pressure; the first is taken. See
    @docs/plans/PLAN-crawler-refinement.md Phase 1.
    """
    by_url: dict[str | None, list[object]] = {}
    for result in results:
        by_url.setdefault(getattr(result, "url", None), []).append(result)

    pairs: list[tuple[str, object | None]] = []
    for url in urls:
        bucket = by_url.get(url)
        pairs.append((url, bucket.pop(0) if bucket else None))
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
        window = text[:cap]
        # Cut at the latest paragraph break or heading start (a heading with no body is
        # noise). No boundary, or one at 0 that would empty the block, takes the full cap.
        boundary = max([window.rfind("\n\n"), *(m.start() for m in _HEADING_LINE.finditer(window))])
        cut = boundary if boundary > 0 else cap
        text = (
            window[:cut].rstrip()
            + f"\n\n_[truncated at the {cap}-character cap — the rest of this page was omitted]_"
        )
    lines.append(text)

    return "\n\n".join(lines)


async def _fetch(
    urls: list[str], config: HarnessConfig, registry: SourceRegistry
) -> tuple[str, list[FetchedPage]]:
    """Fetch every URL, returning model-facing markdown and the full per-URL artifact."""
    # Crawl each canonical URL once: the registry dedups by normalized URL, so two
    # spellings would otherwise render duplicate [Sn] headings over different bodies.
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

    sources_dir = _sources_dir(config, registry)
    for page in pages:
        _write_source_file(sources_dir, page)

    content = "\n\n".join(_render(page, config.fetch.per_page_char_cap) for page in pages)
    return content, pages


def build_fetch_tool(config: HarnessConfig, registry: SourceRegistry) -> BaseTool:
    """Build the `fetch_pages` tool, closing over `config` and the shared `registry`.

    Creates `<workspace_dir>/sources/<run_id>` up front, so an unwritable workspace
    fails at startup — before any research is spent — rather than silently losing
    captures mid-run.
    """
    _sources_dir(config, registry).mkdir(parents=True, exist_ok=True)

    max_urls = config.fetch.max_urls_per_call

    class FetchPagesInput(BaseModel):
        """Model-facing input schema for the `fetch_pages` tool."""

        model_config = ConfigDict(extra="forbid")

        urls: list[str] = Field(
            max_length=max_urls,
            description=(
                "The URLs to fetch, in the order they should be reported. "
                f"At most {max_urls} per call."
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

    # Appended rather than written into the docstring above: the limit is config, and a
    # literal would go stale the moment an operator changed it (D2).
    fetch_pages.description = (
        f"{fetch_pages.description}\n\nAt most {max_urls} URLs may be requested per call; "
        "a call carrying more is rejected without fetching anything."
    )

    # `exc` is `object`: langchain may hand over a pydantic v1 or v2 `ValidationError`.
    def _explain_validation_error(exc: object) -> str:
        """Turn a rejected call into a message the model can act on and retry."""
        return (
            f"fetch_pages rejected this call without fetching anything: {exc}. "
            f"At most {max_urls} URLs may be requested per call."
        )

    # A callable, not a fixed string: this swallows EVERY validation failure for the tool, so
    # a wrong type must not be misreported as an over-limit call (D2, risk #3).
    fetch_pages.handle_validation_error = _explain_validation_error

    return fetch_pages
