"""Fetch many URLs concurrently through crawl4ai; no single URL can fail the batch.

Each URL is classified into `FetchOutcome` rather than raising. The model sees compact,
`[Sn]`-headed, boilerplate-stripped markdown capped per page; the artifact carries the
full untruncated outcomes for downstream use (e.g. `harness.sources.SourceRegistry`).

crawl4ai is imported lazily, inside `_fetch`/`_crawler_class`: its import alone costs ~1.2s,
which every CLI invocation paid before doing anything. The first fetch pays it instead,
overlapped with the model's first research turn.
"""

import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.runlog import RunLog
from harness.sources import (
    FETCH_FAILED_PREFIX,
    SourceRegistry,
    is_failed_capture,
    normalize_url,
    note_digest_candidate,
    sources_dir,
)

FetchOutcome = Literal["fetched", "blocked", "timeout", "non_html", "error", "pdf"]

_BLOCKED_STATUSES = frozenset({403, 429, 503})
_EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "script", "style", "form", "noscript"]

# 75%, not crawl4ai's default 90%: each concurrent crawl is a real browser page, and this
# box also hosts SearXNG and Chromium.
_MEMORY_THRESHOLD_PERCENT = 75.0


def _crawler_class() -> Any:
    """The crawler class `_fetch` constructs — the one seam tests patch to avoid a browser.

    A function rather than a module-level name so crawl4ai stays unimported until a fetch
    actually happens.
    """
    from crawl4ai import AsyncWebCrawler  # type: ignore[import-untyped]

    return AsyncWebCrawler


def _pdf_crawler_parts() -> tuple[Any, Any]:
    """The `(PDFCrawlerStrategy, PDFContentScrapingStrategy)` pair the PDF batch constructs.

    A function, mirroring `_crawler_class`'s shape, so both stay unimported (and pypdf's
    presence unchecked) until a PDF fetch actually happens — and so tests can patch this one
    seam instead of the classes themselves.
    """
    from crawl4ai import PDFContentScrapingStrategy
    from crawl4ai.processors.pdf import PDFCrawlerStrategy  # type: ignore[import-untyped]

    return PDFCrawlerStrategy, PDFContentScrapingStrategy


def _looks_like_pdf_url(url: str) -> bool:
    """Whether `url`'s path (lowercased, query/fragment ignored) ends with `.pdf`.

    Cheap, pre-fetch routing for the common case; an extensionless PDF URL still gets caught
    post-fetch by `classify`'s content-type check and rerouted once.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return path.lower().endswith(".pdf")


# Despite the name, crawl4ai 0.9.2 re-fetches nothing — this caps how many times a domain's
# backoff delay may double.
_RATE_LIMIT_MAX_RETRIES = 1

# crawl4ai hands us flat strings with no heading tree, so a cut boundary has to be found in the
# text itself.
_HEADING_LINE = re.compile(r"^#{1,6} ", re.MULTILINE)


def classify(
    status_code: int | None,
    error_message: str | None,
    content_type: str | None,
    markdown: str,
) -> FetchOutcome:
    """Classify one crawl result into the frozen outcome vocabulary.

    Success is inferred from `error_message` being absent: there is no `success` flag, so no
    error plus empty markdown means an empty (non-HTML) page rather than a failure.
    """
    if status_code in _BLOCKED_STATUSES:
        return "blocked"
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if content_type and "application/pdf" in content_type.lower():
        # The internal reroute signal: an extensionless PDF URL, discovered only after the
        # Playwright fetch. `_fetch` never writes this to a capture — it reroutes the URL
        # through the PDF batch once, whose own classification is the final outcome.
        return "pdf"
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

    Never positional: an unclaimed result could attribute one page's body to another's `[Sn]`.
    No match pairs with `None` and reports `error`. One URL can yield two results under memory
    pressure; the first is taken.
    """
    by_url: dict[str | None, list[object]] = {}
    for result in results:
        by_url.setdefault(getattr(result, "url", None), []).append(result)

    pairs: list[tuple[str, object | None]] = []
    for url in urls:
        bucket = by_url.get(url)
        pairs.append((url, bucket.pop(0) if bucket else None))
    return pairs


def _holds_successful_capture(path: Path) -> bool:
    """Whether `path` already holds real captured content rather than a failure stub.

    A missing or unreadable file answers False: the caller asks only to decide whether
    overwriting would LOSE evidence, and neither case has any to lose.
    """
    try:
        return not is_failed_capture(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False


def _write_source_file(captures_dir: Path, page: FetchedPage, run_log: RunLog) -> None:
    """Write `page`'s full-text capture to `<captures_dir>/<source_id>.md`.

    A `fetched` page gets its full untruncated markdown; any other outcome gets a stub whose
    first line names the outcome, so a reader can treat it as unusable without parsing further.
    A refetched URL reuses its registry ID and rewrites its file rather than duplicating it
    (D10) — but only ever upward: a later failure never overwrites captured content, because
    both attempts share one `[Sn]` and downgrading the file would make a claim cited from the
    good capture report as unverifiable.

    A write failure degrades to a skipped file, never an exception into the model — but it is
    RECORDED on `run_log` (best-effort + disclose): the source will show as unusable evidence,
    and without the incident the report mis-attributes a local disk problem as a fetch failure.
    """
    path = captures_dir / f"{page.source_id}.md"
    if page.outcome != "fetched" and _holds_successful_capture(path):
        return

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
        run_log.record(
            "capture_write_failed",
            f"[{page.source_id}] {page.url}: the fetched page could not be saved to {path} "
            f"({exc}); the source will be reported as unusable evidence",
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
        # Cut at the latest paragraph break or heading start (a heading with no body is noise).
        # No boundary, or one at 0 that would empty the block, takes the full cap.
        boundary = max([window.rfind("\n\n"), *(m.start() for m in _HEADING_LINE.finditer(window))])
        cut = boundary if boundary > 0 else cap
        text = (
            window[:cut].rstrip()
            + f"\n\n_[truncated at the {cap}-character cap — the rest of this page was omitted]_"
        )
    lines.append(text)

    return "\n\n".join(lines)


def _failure_detail(page: FetchedPage) -> str:
    """One incident line for a page that did not come back `fetched`."""
    bits: list[str] = [page.outcome]
    if page.status_code is not None:
        bits.append(f"status {page.status_code}")
    if page.error:
        bits.append(page.error)
    return f"[{page.source_id}] {page.url}: {' — '.join(bits)}"


async def _fetch(
    urls: list[str], config: HarnessConfig, registry: SourceRegistry, run_log: RunLog
) -> tuple[str, list[FetchedPage]]:
    """Fetch every URL, returning model-facing markdown and the full per-URL artifact."""
    from crawl4ai import (
        BrowserConfig,
        CacheMode,
        CrawlerRunConfig,
        DefaultMarkdownGenerator,
        MemoryAdaptiveDispatcher,
        PruningContentFilter,
        RateLimiter,
    )

    # Crawl each canonical URL once: the registry dedups by normalized URL, so two spellings
    # would otherwise render duplicate [Sn] headings over different bodies.
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

    # One crawler cannot mix strategies in one `arun_many`: partition the batch by extension
    # first (cheap, pre-fetch routing); an extensionless PDF is only discoverable after the
    # Playwright fetch, via its content-type, and is rerouted below.
    pdf_urls = [url for url in urls if _looks_like_pdf_url(url)]
    pdf_url_set = set(pdf_urls)
    playwright_urls = [url for url in urls if url not in pdf_url_set]

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

    pages_by_url: dict[str, FetchedPage] = {}
    reroute_urls: list[str] = []

    if playwright_urls:
        # verbose=False is deliberate: crawl4ai defaults it True and prints into our process.
        async with _crawler_class()(config=BrowserConfig(verbose=False)) as crawler:
            raw_results = await crawler.arun_many(
                playwright_urls, config=run_config, dispatcher=dispatcher
            )
            results = list(raw_results)

        for url, result in _pair(playwright_urls, results):
            if result is None:
                pages_by_url[url] = FetchedPage(
                    source_id=registry.add(url),
                    url=url,
                    outcome="error",
                    status_code=None,
                    title=None,
                    markdown="",
                    error="no result returned for this URL",
                )
                continue

            markdown = _markdown_of(result)
            title = _title_of(result)
            status_code = getattr(result, "status_code", None)
            error_message = getattr(result, "error_message", None)
            outcome = classify(status_code, error_message, _content_type(result), markdown)
            if outcome == "pdf":
                # An extensionless PDF URL, discovered only after the fetch: reroute it
                # through the PDF batch once rather than reporting the internal signal.
                reroute_urls.append(url)
                continue

            source_id = registry.add(url, title=title)
            pages_by_url[url] = FetchedPage(
                source_id=source_id,
                url=url,
                outcome=outcome,
                status_code=status_code,
                title=title,
                markdown=markdown,
                error=error_message,
            )

    pdf_batch_urls = pdf_urls + reroute_urls
    if pdf_batch_urls:
        pdf_crawler_strategy_cls, pdf_scraping_strategy_cls = _pdf_crawler_parts()
        pdf_run_config = CrawlerRunConfig(
            page_timeout=config.fetch.page_timeout_ms,
            scraping_strategy=pdf_scraping_strategy_cls(),
            cache_mode=CacheMode.BYPASS,
            stream=False,
            verbose=False,
        )
        async with _crawler_class()(
            crawler_strategy=pdf_crawler_strategy_cls(), config=BrowserConfig(verbose=False)
        ) as pdf_crawler:
            raw_pdf_results = await pdf_crawler.arun_many(
                pdf_batch_urls, config=pdf_run_config, dispatcher=dispatcher
            )
            pdf_results = list(raw_pdf_results)

        for url, result in _pair(pdf_batch_urls, pdf_results):
            if result is None:
                pages_by_url[url] = FetchedPage(
                    source_id=registry.add(url),
                    url=url,
                    outcome="error",
                    status_code=None,
                    title=None,
                    markdown="",
                    error="no result returned for this URL",
                )
                continue

            markdown = _markdown_of(result)
            title = _title_of(result)
            status_code = getattr(result, "status_code", None)
            error_message = getattr(result, "error_message", None)
            if not error_message and not markdown.strip():
                # Empty extraction must never register as `non_html` (R2): it is a failure of
                # this specific fetch, not evidence the page was never HTML in the first place.
                error_message = "empty PDF extraction"
            # An HTML-equivalent content type: `PDFCrawlerStrategy` hardcodes `application/pdf`
            # response headers on success, and reclassifying with that would re-trigger the
            # "pdf" reroute signal forever.
            outcome = classify(status_code, error_message, "text/html", markdown)
            source_id = registry.add(url, title=title)
            pages_by_url[url] = FetchedPage(
                source_id=source_id,
                url=url,
                outcome=outcome,
                status_code=status_code,
                title=title,
                markdown=markdown,
                error=error_message,
            )

    pages = [pages_by_url[url] for url in urls]

    # Recorded here, in the shared path, so `fetch_pages` and `fetch_raw` disclose alike.
    # The model already sees each failure in its rendered block; this is the operator's copy.
    for page in pages:
        if page.outcome != "fetched":
            run_log.record("fetch_failed", _failure_detail(page))

    captures_dir = sources_dir(config, registry)
    for page in pages:
        _write_source_file(captures_dir, page, run_log)

    content = "\n\n".join(_render(page, config.fetch.per_page_char_cap) for page in pages)
    return content, pages


def build_fetch_tool(
    config: HarnessConfig, registry: SourceRegistry, run_log: RunLog | None = None
) -> BaseTool:
    """Build the `fetch_pages` tool, closing over `config`, the shared `registry` and `run_log`.

    Creates `<workspace_dir>/<run_id>/sources` up front, so an unwritable workspace fails at
    startup rather than silently losing captures mid-run.
    """
    sources_dir(config, registry).mkdir(parents=True, exist_ok=True)

    log = run_log if run_log is not None else RunLog()
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

        Failures (blocked, timed out, non-HTML, otherwise unfetchable) are reported per URL with
        their outcome rather than raising, so one bad URL never fails the batch. Equivalent
        spellings of the same page (trailing slash, fragment, case) are fetched once.
        """
        content, pages = await _fetch(urls, config, registry, log)
        # Only a real capture is even a digest CANDIDATE (R5) — a failed fetch stays "unread".
        # The mark itself is deferred to the delegation boundary: agent.py's
        # `_ReaderDigestMiddleware` promotes these to "digested" only when the reader's digest
        # actually reaches the lead, so a crash after a successful fetch never over-claims.
        for page in pages:
            if page.outcome == "fetched":
                note_digest_candidate(page.source_id)
        return content, pages

    return _install_url_limit_contract(fetch_pages, max_urls)


def _install_url_limit_contract(fetch_tool: BaseTool, max_urls: int) -> BaseTool:
    """Append the config-driven URL cap to `fetch_tool` and install its validation explainer.

    Shared by `build_fetch_tool` and `build_fallback_tool` — the wording is policy, and two
    hand-pasted copies could drift. Appended rather than written into the docstring: the limit
    is config, and a literal would go stale the moment an operator changed it (D2).
    """
    fetch_tool.description = (
        f"{fetch_tool.description}\n\nAt most {max_urls} URLs may be requested per call; "
        "a call carrying more is rejected without fetching anything."
    )

    # `exc` is `object`: langchain may hand over a pydantic v1 or v2 `ValidationError`. A
    # callable, not a fixed string: this swallows EVERY validation failure for the tool, so a
    # wrong type must not be misreported as an over-limit call (D2).
    def _explain_validation_error(exc: object) -> str:
        """Turn a rejected call into a message the model can act on and retry."""
        return (
            f"{fetch_tool.name} rejected this call without fetching anything: {exc}. "
            f"At most {max_urls} URLs may be requested per call."
        )

    fetch_tool.handle_validation_error = _explain_validation_error
    return fetch_tool
