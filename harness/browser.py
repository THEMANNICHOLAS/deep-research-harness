"""One Chromium session, launched at startup and reused for every fetch call (R2).

crawl4ai is imported lazily, inside `BrowserSession.start`, via the same
`harness.tools.fetch._crawler_class` seam `_fetch` itself uses — `browser.py` must never
import `AsyncWebCrawler` directly, or tests that patch that one seam would miss this
construction site.
"""

import asyncio
from typing import Any

from harness.config import HarnessConfig
from harness.runlog import RunLog, or_default

_SETUP_HINT = "crawl4ai manages its own Playwright/Chromium (try: crawl4ai-setup)"

_NOT_RUNNING_DETAIL = (
    f"the browser session is not running (a previous relaunch failed) ({_SETUP_HINT})"
)

# The HTTP handle's own detail: it has no relaunch machinery and no Chromium, so
# `_NOT_RUNNING_DETAIL`'s relaunch/`crawl4ai-setup` framing would send a debugger down
# entirely the wrong path.
_HTTP_NOT_RUNNING_DETAIL = (
    "the browser-free HTTP fetch strategy is not running (the session was never started, "
    "or was closed)"
)


class BrowserPreflightError(Exception):
    """Raised when Chromium cannot be launched at the startup health check."""


class BrowserSession:
    """One started `AsyncWebCrawler`, held for the whole run and relaunched at most once.

    `main()` calls `start()` once at startup and `close()` in its `finally` (idempotent, so
    an early-exit close and the final close never conflict). Every fetch call goes through
    `arun_many`, which relaunches the crawler exactly once per run if the underlying handle
    dies, and re-raises to the caller on a second death (that fetch call fails, the run
    continues). The lead dispatches parallel researchers, each fetching concurrently, so a
    dead handle can be discovered by several `arun_many` calls at once — a lock plus a
    generation counter lets every sibling that fails while a relaunch is in flight RIDE that
    one relaunch instead of racing to trigger (or re-raise past) their own.
    """

    def __init__(
        self, config: HarnessConfig, run_log: RunLog | None = None, *, run_id: str
    ) -> None:
        # `run_id` is keyword-only and required: it names the per-run workspace subtree the
        # HTTP crawler's downloads are contained in, and there is no safe default (falling
        # back would put them in `~/.crawl4ai/downloads`). Keyword-only so it can never be
        # mis-bound by an existing positional `BrowserSession(config, run_log)` call.
        self._config = config
        self._run_id = run_id
        self._run_log = or_default(run_log)
        self._crawler: Any = None
        self._http: Any = None
        self._relaunched = False
        self._lock = asyncio.Lock()
        self._generation = 0

    async def start(self) -> None:
        """Construct and start both underlying crawlers, or raise `BrowserPreflightError`.

        Browser first, HTTP second: the expensive, actually-failure-prone launch fails fast
        before the cheap one is paid for. Each has its own try/except so a Chromium failure
        and an HTTP-strategy failure never report as each other.

        Both imports sit INSIDE the first try, not hoisted above it (PR review, Phase 2): an
        `ImportError` from crawl4ai, or a circular import of `harness.tools.fetch`, must reach
        `__main__`'s `except BrowserPreflightError` like any other launch failure. Hoisted, it
        escaped as a raw traceback that skipped `renderer.close()` and left the Rich
        full-screen buffer unrestored on a TTY.
        """
        try:
            from crawl4ai import BrowserConfig  # type: ignore[import-untyped]

            from harness.tools.fetch import _build_http_crawler, _crawler_class

            crawler = _crawler_class()(config=BrowserConfig(verbose=False))
            await crawler.start()
        except Exception as exc:
            raise BrowserPreflightError(
                f"Chromium could not be launched: {exc} ({_SETUP_HINT})"
            ) from exc
        self._crawler = crawler

        try:
            http_crawler = _build_http_crawler(self._config, self._run_id)
            await http_crawler.start()
        except Exception as exc:
            # The Chromium half is already live: a half-failed start must not leak it past
            # the raise, or every such preflight exit orphans a headless Chromium process
            # (`close()` is idempotent and swallows teardown errors).
            await self.close()
            raise BrowserPreflightError(
                f"the browser-free HTTP fetch strategy could not be started: {exc}"
            ) from exc
        self._http = http_crawler

    async def close(self) -> None:
        """Idempotent teardown: a teardown error must never mask the run's real outcome."""
        for attr in ("_http", "_crawler"):
            crawler = getattr(self, attr)
            if crawler is None:
                continue
            try:
                await crawler.close()
            except Exception:
                pass
            setattr(self, attr, None)

    async def arun_many(self, urls: list[str], config: Any = None, dispatcher: Any = None) -> list:
        """Fetch `urls` through the held crawler, relaunching once per session on failure.

        Concurrency-safe (Phase 1 fix #3): several parallel researchers can each discover the
        same dead handle at once. Only the first failure to reach the lock actually relaunches;
        every sibling that fails while that relaunch is in flight sees the generation counter
        already advanced and rides the new crawler instead of triggering (or re-raising past)
        a relaunch of its own.
        """
        generation = self._generation
        crawler = self._crawler
        if crawler is None:
            # `close()` holds `_crawler` at None for the WHOLE of the relaunch's Chromium
            # launch, so a call that ARRIVES in that window is not evidence of a failure.
            # Waiting on the lock parks it until the relaunch settles, then re-reads: still
            # None means the relaunch genuinely failed, which is the only case that detail
            # describes truthfully. Without this the arrival disclosed a failure that never
            # happened, to the model and to the report's gaps section.
            async with self._lock:
                generation = self._generation
                crawler = self._crawler
            if crawler is None:
                raise BrowserPreflightError(_NOT_RUNNING_DETAIL)
        try:
            return list(await crawler.arun_many(urls, config=config, dispatcher=dispatcher))
        except Exception as exc:
            async with self._lock:
                if self._generation == generation:
                    # Nobody relaunched while we were failing, so this failure is ours to act on.
                    if self._relaunched:
                        raise  # the session's one relaunch is already spent
                    self._relaunched = True
                    self._run_log.record(
                        "browser_relaunched", f"the browser session died and was relaunched: {exc}"
                    )
                    # Rebuilds BOTH handles (browser AND the warm HTTP crawler): `start()`
                    # is the only construction site, so the HTTP handle must not be left
                    # dangling after a relaunch either.
                    await self.close()
                    await self.start()  # a failed relaunch raises BrowserPreflightError
                    self._generation += 1
                # else: a sibling relaunched while we were failing -- ride its new crawler.
            crawler = self._crawler
            if crawler is None:
                raise BrowserPreflightError(_NOT_RUNNING_DETAIL) from exc
            return list(await crawler.arun_many(urls, config=config, dispatcher=dispatcher))

    async def http_arun_many(
        self, urls: list[str], config: Any = None, dispatcher: Any = None
    ) -> list:
        """Fetch `urls` through the warm browser-free HTTP crawler (R6).

        Deliberately WITHOUT `arun_many`'s relaunch machinery: a dead aiohttp session is not
        the Chromium failure mode that machinery exists for, and a batch-level failure here
        costs nothing — `_fetch` catches it, every URL reads as thin, and the browser pass
        recovers all of them, which is exactly the pre-R6 behavior.
        """
        if self._http is None:
            raise BrowserPreflightError(_HTTP_NOT_RUNNING_DETAIL)
        return list(await self._http.arun_many(urls, config=config, dispatcher=dispatcher))
