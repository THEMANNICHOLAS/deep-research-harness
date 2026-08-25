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

    def __init__(self, config: HarnessConfig, run_log: RunLog | None = None) -> None:
        self._config = config
        self._run_log = or_default(run_log)
        self._crawler: Any = None
        self._relaunched = False
        self._lock = asyncio.Lock()
        self._generation = 0

    async def start(self) -> None:
        """Construct and start the underlying crawler, or raise `BrowserPreflightError`."""
        try:
            from crawl4ai import BrowserConfig  # type: ignore[import-untyped]

            from harness.tools.fetch import _crawler_class

            crawler = _crawler_class()(config=BrowserConfig(verbose=False))
            await crawler.start()
        except Exception as exc:
            raise BrowserPreflightError(
                f"Chromium could not be launched: {exc} ({_SETUP_HINT})"
            ) from exc
        self._crawler = crawler

    async def close(self) -> None:
        """Idempotent teardown: a teardown error must never mask the run's real outcome."""
        if self._crawler is None:
            return
        try:
            await self._crawler.close()
        except Exception:
            pass
        self._crawler = None

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
                    await self.close()
                    await self.start()  # a failed relaunch raises BrowserPreflightError
                    self._generation += 1
                # else: a sibling relaunched while we were failing -- ride its new crawler.
            crawler = self._crawler
            if crawler is None:
                raise BrowserPreflightError(_NOT_RUNNING_DETAIL) from exc
            return list(await crawler.arun_many(urls, config=config, dispatcher=dispatcher))
