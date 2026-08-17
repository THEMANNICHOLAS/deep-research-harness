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
from urllib.parse import urlsplit

import httpx
from crawl4ai import (  # type: ignore[import-untyped]
    AsyncWebCrawler,
    BrowserConfig,
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

from harness import blocklist
from harness.config import HarnessConfig
from harness.sources import SourceRegistry, normalize_url

FetchOutcome = Literal["fetched", "blocked", "timeout", "non_html", "error", "skipped"]

# Statuses that record a domain into the persistent blocklist (R3). 429/503 stay ordinary
# retryable/blocked outcomes — a rate limit or transient outage is not evidence the whole
# domain refuses this harness the way a 403/401 is.
_BLOCKLIST_STATUSES = frozenset({401, 403})

# 401 included: both blocklist statuses classify the same way, so an auth-walled domain
# reads "blocked" on first encounter rather than "error" with a raw crawl4ai string.
_BLOCKED_STATUSES = frozenset({401, 403, 429, 503})
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
    content_type: str | None = None


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


def _host_of(url: str) -> str | None:
    """Return `url`'s lowercased host for blocklist gating/recording, or `None` if it has
    none recoverable.

    Runs `url` through `normalize_url` first — the SAME parsing conventions the source
    registry uses — rather than hand-rolling a second URL parser. `normalize_url` is total
    (a URL too malformed to parse returns unchanged rather than raising), so this guards the
    same `ValueError` `urlsplit`/`.hostname` can raise on that unchanged, still-malformed
    string. No placeholder key is invented for a URL with no recoverable host — it is simply
    never blocked or recorded.
    """
    try:
        hostname = urlsplit(normalize_url(url)).hostname
    except ValueError:
        return None
    return hostname.lower() if hostname else None


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
            content_type=None,
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
            content_type=None,
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
            content_type=None,
        )

    markdown = _markdown_of(result)
    title = _title_of(result)
    error_message = getattr(result, "error_message", None)
    status_code = getattr(result, "status_code", None)
    if status_code is None:
        status_code = _status_from_error(error_message)
    content_type = _content_type(result)
    outcome = classify(status_code, error_message, content_type, markdown)
    return FetchedPage(
        source_id="",
        url=url,
        outcome=outcome,
        status_code=status_code,
        title=title,
        markdown=markdown,
        error=error_message,
        content_type=content_type,
    )


async def _is_pdf(client: httpx.AsyncClient, url: str, timeout_ms: int) -> bool:
    """HEAD-precheck `url` for `application/pdf` before its body is ever fetched (D5).

    Returns True only on a 2xx response whose `content-type` contains `application/pdf`
    (case-insensitive). Never raises: any non-2xx status, missing/other content type, or
    ANY exception returns False so the caller falls through to a normal fetch attempt.

    `client` is shared across the batch — one connection pool per `fetch_pages` call
    rather than one per URL.

    The `except` is deliberately broad rather than `httpx.HTTPError`. A malformed URL — and
    the model supplies these — raises `httpx.InvalidURL` or `idna.IDNAError` during URL
    parsing, neither of which is an `httpx.HTTPError`; letting one escape would propagate
    out of `asyncio.gather` and sink the whole `fetch_pages` call, breaking this module's
    "no single URL can fail the batch" contract.

    Risk !#3: this costs one extra round trip per URL. `asyncio.wait_for` bounds the whole
    HEAD, because httpx's `timeout=` is per-phase (connect/read/write/pool each get the full
    value), so a stalling server could otherwise hold a concurrency slot for a multiple of
    it. A server that rejects HEAD is no worse off than before this precheck existed.
    """
    try:
        response = await asyncio.wait_for(
            client.head(url, follow_redirects=True), timeout_ms / 1000
        )
    except Exception:
        return False
    if response.status_code // 100 != 2:
        return False
    content_type = response.headers.get("content-type", "")
    return "application/pdf" in content_type.lower()


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
    head_client: httpx.AsyncClient,
    url: str,
    run_config: object,
    deadline_ms: int,
    max_retries: int,
    semaphore: asyncio.Semaphore,
) -> FetchedPage:
    """Attempt `url` up to `max_retries + 1` times, returning the first non-retryable page
    or the last attempt once the budget is exhausted. No backoff between attempts — none
    is specified, and the per-attempt deadline already bounds each one.

    The PDF precheck runs once here, ahead of the retry loop, rather than inside it, so a
    URL that gets retried still issues exactly one HEAD. It shares `semaphore` with the
    fetch attempts so it counts toward the same concurrency bound. The resulting `non_html`
    page can never escalate to Chromium: `_is_thin` only escalates a `non_html` result whose
    `content_type` is absent or HTML-like, and `application/pdf` is neither (R1).
    """
    async with semaphore:
        if await _is_pdf(head_client, url, deadline_ms):
            return FetchedPage(
                source_id="",
                url=url,
                outcome="non_html",
                status_code=None,
                title=None,
                markdown="",
                error=None,
                content_type="application/pdf",
            )

    page: FetchedPage | None = None
    for _ in range(max_retries + 1):
        async with semaphore:
            page = await _fetch_one(crawler, url, run_config, deadline_ms)
        if not _is_retryable(page):
            return page
    assert page is not None  # max_retries is gt=0, so the loop always runs
    return page


def _is_thin(page: FetchedPage, min_words: int) -> bool:
    """True when the HTTP attempt reads like a JS shell rather than real content (D3):
    word count of the generated markdown is the signal, chosen over raw-HTML shell markers
    (`<div id="root">`, noscript patterns) because it measures what the agent actually
    consumes, not the page's raw structure.

    Escalates on `outcome == "fetched"`, and also on `outcome == "non_html"` when the page
    looks like an empty HTML page rather than a genuine non-HTML resource (Reconciliation
    #2): `classify()` maps a 200 `text/html` response with empty generated markdown — the
    canonical `<div id="root"></div>` SPA shell — to `non_html`, not `fetched`, so excluding
    that outcome would leave the strongest JS-shell signal unable to ever escalate. A missing
    `content_type` is treated as HTML-shaped too, since crawl4ai only omits the header, it
    never fabricates a non-HTML one. `timeout`, `error`, and `blocked` outcomes never
    escalate — those are genuine failures, not thin-but-successful content.
    """
    if len(page.markdown.split()) >= min_words:
        return False
    if page.outcome == "fetched":
        return True
    if page.outcome == "non_html":
        return page.content_type is None or "html" in page.content_type.lower()
    return False


async def _escalate_one(
    browser: object,
    url: str,
    run_config: object,
    deadline_ms: int,
    semaphore: asyncio.Semaphore,
) -> FetchedPage:
    """One deadlined browser attempt for `url`, reusing `_fetch_one` rather than a second
    fetch path. This is deliberately not `_fetch_with_retries`: escalation is contractually
    at most one attempt and must not consume the R4 retry budget.
    """
    async with semaphore:
        return await _fetch_one(browser, url, run_config, deadline_ms)


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

    # Gate before any request (R3): a URL whose host was previously recorded (403/401) is
    # never fetched at all — no task, no HEAD, no `arun`. Loaded once per tool call so one
    # call issues at most one read regardless of how many URLs it carries. `to_thread`
    # keeps the disk read off the event loop, where it would stall every other
    # concurrently-running fetch's deadline timer.
    blocked_hosts = await asyncio.to_thread(
        blocklist.load, config.fetch.blocklist_path, config.fetch.blocklist_ttl_days
    )

    pages_list: list[FetchedPage | None] = [None] * len(urls)
    to_fetch: list[str] = []
    fetch_indices: list[int] = []
    for i, url in enumerate(urls):
        host = _host_of(url)
        if host is not None and host in blocked_hosts:
            pages_list[i] = FetchedPage(
                source_id="",
                url=url,
                outcome="skipped",
                status_code=None,
                title=None,
                markdown="",
                error=(
                    f"{host} is on the blocklist (a prior 403/401 was recorded for this "
                    "domain) and was skipped without a fetch"
                ),
                content_type=None,
            )
        else:
            to_fetch.append(url)
            fetch_indices.append(i)

    # Shared by the HTTP and (lazily built, below) browser run configs — built once so the
    # policy list lives in exactly one place rather than as two parallel literals. Building
    # this dict issues no request, so it's built unconditionally even if every URL is
    # skipped; only the crawler construction itself is gated on `to_fetch`.
    _shared_run_config_kwargs: dict[str, object] = {
        "excluded_tags": _EXCLUDED_TAGS,
        "markdown_generator": DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
        "cache_mode": CacheMode.BYPASS,
        "stream": False,
        "verbose": False,
    }

    if to_fetch:
        # page_timeout aligned to http_deadline_ms, mirroring the browser config below
        # (Reconciliation #3's pattern): asyncio.wait_for in _fetch_one is the hard bound,
        # and crawl4ai's own timeout must never bind FIRST — an operator raising
        # http_deadline_ms would otherwise hit a hidden lower cap misreported as a
        # crawl4ai-side failure.
        run_config = CrawlerRunConfig(
            page_timeout=config.fetch.http_deadline_ms,
            **_shared_run_config_kwargs,
        )
        semaphore = asyncio.Semaphore(config.fetch.http_concurrency)

        # verbose=False above is deliberate: crawl4ai defaults it True and prints into our
        # process. The HTTP strategy/config below exposes no separate verbose flag to
        # silence, and AsyncWebCrawler.__init__ falls back to BrowserConfig() (verbose
        # defaults True) to build its logger regardless of strategy, so this crawler still
        # prints one startup banner (Discovery #1, deferred — the HTTP path's banner
        # remains). The browser crawler below passes BrowserConfig(verbose=False)
        # explicitly, closing the gap for that path.
        # One HEAD-precheck client for the whole batch: constructing an AsyncClient (and
        # its connection pool) per URL would scale setup cost with http_concurrency for
        # no benefit.
        async with httpx.AsyncClient(timeout=config.fetch.http_deadline_ms / 1000) as head_client:
            async with AsyncWebCrawler(
                crawler_strategy=AsyncHTTPCrawlerStrategy(
                    browser_config=HTTPCrawlerConfig(downloads_path=config.fetch.downloads_dir)
                )
            ) as crawler:
                fetched_pages = await asyncio.gather(
                    *(
                        _fetch_with_retries(
                            crawler,
                            head_client,
                            url,
                            run_config,
                            config.fetch.http_deadline_ms,
                            config.fetch.max_retries,
                            semaphore,
                        )
                        for url in to_fetch
                    )
                )

        for idx, page in zip(fetch_indices, fetched_pages, strict=True):
            pages_list[idx] = page

    thin = [
        i
        for i, page in enumerate(pages_list)
        if page is not None and _is_thin(page, config.fetch.min_markdown_words)
    ]
    if thin:
        # The browser attempt gets its own CrawlerRunConfig (Reconciliation #3): reusing the
        # HTTP one's wait_until="domcontentloaded" fires before client-side render, so
        # Chromium could return the same near-empty markdown the HTTP path already produced.
        # page_timeout is aligned to browser_deadline_ms (as the HTTP config's is to
        # http_deadline_ms) so crawl4ai's own page timeout never preempts the escalation
        # budget — asyncio.wait_for(browser_deadline_ms) in _escalate_one remains the hard
        # no-hang bound (R2); this page_timeout is the cooperative one, deliberately the same
        # number. Built only here, inside `if thin:`, so a run with no thin results does no
        # extra work and never launches Chromium.
        browser_run_config = CrawlerRunConfig(
            page_timeout=config.fetch.browser_deadline_ms,
            wait_until="networkidle",
            **_shared_run_config_kwargs,
        )
        browser_semaphore = asyncio.Semaphore(config.fetch.browser_concurrency)
        async with AsyncWebCrawler(
            config=BrowserConfig(verbose=False, downloads_path=config.fetch.downloads_dir)
        ) as browser:
            escalated = await asyncio.gather(
                *(
                    _escalate_one(
                        browser,
                        urls[i],
                        browser_run_config,
                        config.fetch.browser_deadline_ms,
                        browser_semaphore,
                    )
                    for i in thin
                )
            )
        # The escalation's outcome wins unconditionally, even a timeout with empty markdown
        # over an HTTP attempt that had returned a few words — no "keep the better result"
        # fallback (decision carried in from the phase gate).
        for i, page in zip(thin, escalated, strict=True):
            pages_list[i] = page

    # Record AFTER escalation over the final per-URL outcomes, so a 403/401 from the
    # Chromium path blocklists the domain too (R3 has no HTTP-only scoping — a WAF that
    # tolerates plain HTTP but bot-blocks the browser would otherwise re-launch Chromium
    # on every future run). Still one write per newly blocked host, off the event loop
    # for the same reason as the load above; skipped pages carry no status and never
    # re-record. 429/503 stay ordinary blocked/retryable outcomes and never record.
    newly_blocked: set[str] = set()
    for maybe_page in pages_list:
        if maybe_page is not None and maybe_page.status_code in _BLOCKLIST_STATUSES:
            host = _host_of(maybe_page.url)
            if host is not None:
                newly_blocked.add(host)
    for host in newly_blocked:
        await asyncio.to_thread(
            blocklist.record, config.fetch.blocklist_path, host, config.fetch.blocklist_ttl_days
        )

    pages: list[FetchedPage] = []
    for url, maybe_page in zip(urls, pages_list, strict=True):
        assert maybe_page is not None  # every index above was filled: skip, fetch, or escalate
        source_id = registry.add(url, title=maybe_page.title)
        pages.append(maybe_page.model_copy(update={"source_id": source_id}))

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
