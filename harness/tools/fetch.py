"""Fetch many URLs concurrently through crawl4ai, with no single URL able to fail the batch.

Each URL is classified into a small outcome vocabulary (`FetchOutcome`) rather than
raising, so a blocked or timed-out page shows up as data for the model to reason about
instead of an exception that would sink the whole tool call. The model sees compact,
`[Sn]`-headed, boilerplate-stripped markdown capped per page; the artifact carries the
full, untruncated per-URL outcomes for anything downstream that needs them (e.g. citation
resolution via `harness.sources.SourceRegistry`).
"""

import asyncio
import re
from typing import Literal

from crawl4ai import (  # type: ignore[import-untyped]
    AsyncWebCrawler,
    CacheMode,
    CrawlerRunConfig,
    DefaultMarkdownGenerator,
    PruningContentFilter,
)
from crawl4ai.async_crawler_strategy import (  # type: ignore[import-untyped]
    AsyncHTTPCrawlerStrategy,
    HTTPCrawlerConfig,
)
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.sources import SourceRegistry, normalize_url

FetchOutcome = Literal["fetched", "blocked", "timeout", "non_html", "error"]

_BLOCKED_STATUSES = frozenset({403, 429, 503})
_EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "script", "style", "form", "noscript"]

# A markdown heading line. crawl4ai hands us flat strings with no heading tree, so a cut
# boundary has to be found in the text itself.
_HEADING_LINE = re.compile(r"^#{1,6} ", re.MULTILINE)

# Matches the "HTTP <code>: ..." shape crawl4ai 0.9.2's HTTP strategy raises internally on
# a non-2xx response (see _status_from_error).
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")


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


def _status_from_error(error: str | None) -> int | None:
    """Recover the numeric HTTP status crawl4ai's HTTP strategy encodes only in its error
    message on a non-2xx (`CrawlResult.status_code` itself is `None` there).

    Risk !#1: this is a string-format dependency on crawl4ai 0.9.2 — re-verify the parse
    against the installed package on any upgrade.
    """
    if not error:
        return None
    match = _HTTP_STATUS.search(error)
    return int(match.group(1)) if match else None


async def _fetch_one(
    crawler: object, url: str, run_config: object, deadline_ms: int
) -> FetchedPage:
    """One deadlined `arun` attempt for `url`; never raises.

    `source_id` is left blank — the caller assigns it in input order once every URL's
    attempts are done, since registering from inside a concurrent task would number
    `[Sn]` citations by completion order instead.
    """
    try:
        result = await asyncio.wait_for(crawler.arun(url, config=run_config), deadline_ms / 1000)  # type: ignore[attr-defined]
    except TimeoutError:
        return FetchedPage(
            source_id="",
            url=url,
            outcome="timeout",
            status_code=None,
            title=None,
            markdown="",
            error=f"exceeded the {deadline_ms}ms fetch deadline",
        )
    except Exception as exc:
        return FetchedPage(
            source_id="",
            url=url,
            outcome="error",
            status_code=None,
            title=None,
            markdown="",
            error=str(exc),
        )

    if result is None:
        return FetchedPage(
            source_id="",
            url=url,
            outcome="error",
            status_code=None,
            title=None,
            markdown="",
            error="no result returned for this URL",
        )

    markdown = _markdown_of(result)
    title = _title_of(result)
    error_message = getattr(result, "error_message", None)
    status_code = getattr(result, "status_code", None)
    if status_code is None:
        status_code = _status_from_error(error_message)
    outcome = classify(status_code, error_message, _content_type(result), markdown)
    return FetchedPage(
        source_id="",
        url=url,
        outcome=outcome,
        status_code=status_code,
        title=title,
        markdown=markdown,
        error=error_message,
    )


def _is_retryable(page: FetchedPage) -> bool:
    """Retryable: timeouts, 5xx (503 is also classified `blocked` — that overlap is
    intended), and network errors (an `error` outcome with no status). Not retryable: any
    other 4xx, including 429, and every successful outcome.
    """
    if page.outcome == "timeout":
        return True
    if page.status_code is not None and page.status_code >= 500:
        return True
    return page.outcome == "error" and page.status_code is None


async def _fetch_with_retries(
    crawler: object,
    url: str,
    run_config: object,
    deadline_ms: int,
    max_retries: int,
    semaphore: asyncio.Semaphore,
) -> FetchedPage:
    """Attempt `url` up to `max_retries + 1` times, returning the first non-retryable page
    or the last attempt once the budget is exhausted. No backoff between attempts — none
    is specified, and the per-attempt deadline already bounds each one.
    """
    page: FetchedPage | None = None
    for _ in range(max_retries + 1):
        async with semaphore:
            page = await _fetch_one(crawler, url, run_config, deadline_ms)
        if not _is_retryable(page):
            return page
    assert page is not None  # max_retries is gt=0, so the loop always runs
    return page


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
        # Cut on the latest paragraph break or heading start; the heading goes with the cut,
        # since a heading with no body under it is noise. No boundary found — or one at 0,
        # which would empty the block — takes the whole allowance instead.
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
    # Crawl each canonical URL once: the registry dedups by normalized URL, so two spellings
    # of one page would otherwise render duplicate [Sn] headings over different bodies.
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
    semaphore = asyncio.Semaphore(config.fetch.http_concurrency)

    # verbose=False above is deliberate: crawl4ai defaults it True and prints into our
    # process. The HTTP strategy/config below exposes no separate verbose flag to silence
    # (unlike the browser backend this replaces), so there is nothing else to configure here.
    async with AsyncWebCrawler(
        crawler_strategy=AsyncHTTPCrawlerStrategy(browser_config=HTTPCrawlerConfig())
    ) as crawler:
        fetched_pages = await asyncio.gather(
            *(
                _fetch_with_retries(
                    crawler,
                    url,
                    run_config,
                    config.fetch.http_deadline_ms,
                    config.fetch.max_retries,
                    semaphore,
                )
                for url in urls
            )
        )

    pages: list[FetchedPage] = []
    for url, page in zip(urls, fetched_pages, strict=True):
        source_id = registry.add(url, title=page.title)
        pages.append(page.model_copy(update={"source_id": source_id}))

    content = "\n\n".join(_render(page, config.fetch.per_page_char_cap) for page in pages)
    return content, pages


def build_fetch_tool(config: HarnessConfig, registry: SourceRegistry) -> BaseTool:
    """Build the `fetch_pages` tool, closing over `config` and the shared `registry`."""

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
