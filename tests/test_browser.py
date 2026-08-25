"""Behavioral tests for harness.browser.BrowserSession."""

import asyncio

import pytest

from harness.browser import BrowserPreflightError, BrowserSession
from harness.runlog import RunLog
from tests.conftest import _FakeMarkdown, _FakeResult


async def test_session_reuses_one_crawler_across_multiple_batches(install_crawler, make_config):
    """R2's core assertion: one started session spans many batches without reconstructing."""
    config = make_config()
    fake_cls = install_crawler([_FakeResult("https://a.test")])
    session = BrowserSession(config)
    await session.start()

    await session.arun_many(["https://a.test"])
    await session.arun_many(["https://a.test"])

    assert len(fake_cls.constructed_with) == 1


async def test_one_batch_failure_relaunches_once_and_returns_the_retried_results(
    install_crawler, make_config
):
    """Risk #1: a single dead-handle failure relaunches once and the retry succeeds."""
    config = make_config()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="A", fit_markdown="A"))
    ]
    fake_cls = install_crawler(results, fail_batches=1)
    run_log = RunLog()
    session = BrowserSession(config, run_log)
    await session.start()

    returned = await session.arun_many(["https://a.test"])

    assert returned == results
    assert len(fake_cls.constructed_with) == 2
    kinds = [incident.kind for incident in run_log.incidents()]
    assert "browser_relaunched" in kinds


async def test_a_second_batch_failure_raises_instead_of_relaunching_again(
    install_crawler, make_config
):
    """The relaunch is per-SESSION, not per-call: a second death re-raises to the caller."""
    config = make_config()
    fake_cls = install_crawler([_FakeResult("https://a.test")], fail_batches=2)
    session = BrowserSession(config)
    await session.start()

    with pytest.raises(RuntimeError):
        await session.arun_many(["https://a.test"])

    assert len(fake_cls.constructed_with) == 2


async def test_close_is_idempotent_and_closes_the_underlying_crawler_once(
    install_crawler, make_config
):
    config = make_config()
    fake_cls = install_crawler([])
    session = BrowserSession(config)
    await session.start()

    await session.close()
    await session.close()

    assert len(fake_cls.closed) == 1


async def test_concurrent_batch_failures_share_one_relaunch(install_crawler, make_config):
    """Fix #3: parallel researchers can discover a dead handle at once. Two concurrent
    `arun_many` calls whose FIRST attempt both fail must relaunch exactly once between them
    (one sibling triggers it, the other rides it) rather than each relaunching separately."""
    config = make_config()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="A", fit_markdown="A")),
        _FakeResult("https://b.test", markdown=_FakeMarkdown(raw_markdown="B", fit_markdown="B")),
    ]
    fake_cls = install_crawler(results, fail_batches=2)
    run_log = RunLog()
    session = BrowserSession(config, run_log)
    await session.start()

    first, second = await asyncio.gather(
        session.arun_many(["https://a.test"]), session.arun_many(["https://b.test"])
    )

    # The fake's `arun_many` returns its full canned `results` regardless of which URL was
    # asked for (see `_make_fake_crawler_class`) -- both retries succeeding is the assertion,
    # not which slice of `results` came back.
    assert first == results
    assert second == results
    assert len(fake_cls.constructed_with) == 2, "one shared relaunch, not one per sibling"
    kinds = [incident.kind for incident in run_log.incidents()]
    assert kinds.count("browser_relaunched") == 1, "only one sibling should trigger the relaunch"


async def test_arun_many_after_a_failed_relaunch_raises_preflight_error_not_attribute_error(
    install_crawler, monkeypatch, make_config
):
    """Fix #4: once a relaunch itself fails, `_crawler` stays `None` — a later `arun_many` must
    raise `BrowserPreflightError`, not `AttributeError: 'NoneType' object has no attribute
    'arun_many'`, which is what used to reach the model and the report's disclosure."""
    config = make_config()
    fake_cls = install_crawler([_FakeResult("https://a.test")], fail_batches=1)
    session = BrowserSession(config, RunLog())
    await session.start()

    async def _dead_start() -> None:
        raise BrowserPreflightError("Chromium could not be launched: boom (try crawl4ai-setup)")

    monkeypatch.setattr(session, "start", _dead_start)

    with pytest.raises(BrowserPreflightError):
        await session.arun_many(["https://a.test"])

    with pytest.raises(BrowserPreflightError):
        await session.arun_many(["https://a.test"])

    assert len(fake_cls.constructed_with) == 1


async def test_arrival_during_a_healthy_relaunch_rides_it_instead_of_raising(
    install_crawler, monkeypatch, make_config
):
    """The fix in `arun_many`: a second call that ARRIVES while `_crawler` is None because a
    sibling's relaunch is still in flight must ride that relaunch once it completes, not treat
    the None as evidence of a FAILED relaunch. Distinct from
    `test_concurrent_batch_failures_share_one_relaunch`: there both siblings' own first
    attempts fail; here the second call's own attempt never fails at all -- it only ever
    observes a healthy in-flight relaunch triggered by the first."""
    config = make_config()
    results = [
        _FakeResult("https://a.test", markdown=_FakeMarkdown(raw_markdown="A", fit_markdown="A"))
    ]
    fake_cls = install_crawler(results, fail_batches=1)
    run_log = RunLog()
    session = BrowserSession(config, run_log)
    await session.start()

    relaunch_started = asyncio.Event()
    original_start = BrowserSession.start

    async def _slow_start() -> None:
        # Reached from inside arun_many's `async with self._lock:` block, AFTER `close()` has
        # already reset `_crawler` to None -- setting the event here is what lets the second
        # call's `arun_many` observe that None state deterministically, rather than by luck.
        relaunch_started.set()
        await asyncio.sleep(0.05)
        await original_start(session)

    monkeypatch.setattr(session, "start", _slow_start)

    async def _second_call() -> list:
        await relaunch_started.wait()
        return await session.arun_many(["https://a.test"])

    first, second = await asyncio.gather(session.arun_many(["https://a.test"]), _second_call())

    assert first == results
    assert second == results
    assert len(fake_cls.constructed_with) == 2
    kinds = [incident.kind for incident in run_log.incidents()]
    assert kinds.count("browser_relaunched") == 1


async def test_start_failure_raises_browser_preflight_error_naming_chromium(
    monkeypatch, make_config
):
    """R1's signal: a launch failure is wrapped as `BrowserPreflightError`, not raw."""
    config = make_config()

    class _DeadCrawler:
        def __init__(self, config: object = None) -> None:
            pass

        async def start(self) -> None:
            raise RuntimeError("could not launch")

    monkeypatch.setattr("harness.tools.fetch._crawler_class", lambda: _DeadCrawler)
    session = BrowserSession(config)

    with pytest.raises(BrowserPreflightError, match="(?i)chromium"):
        await session.start()
