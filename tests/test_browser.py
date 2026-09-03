"""Behavioral tests for harness.browser.BrowserSession."""

import asyncio

import pytest

from harness.browser import BrowserPreflightError, BrowserSession
from harness.config import run_downloads_dir
from harness.runlog import RunLog
from tests.conftest import _FakeMarkdown, _FakeResult


async def test_session_reuses_one_crawler_across_multiple_batches(install_crawler, make_config):
    """R2's core assertion: one started session spans many batches without reconstructing."""
    config = make_config()
    fake_cls = install_crawler([_FakeResult("https://a.test")])
    session = BrowserSession(config, run_id="test-run")
    await session.start()

    await session.arun_many(["https://a.test"])
    await session.arun_many(["https://a.test"])

    # `start()` also builds the warm HTTP crawler (Phase 2, R6) -- count BROWSER
    # constructions specifically.
    assert fake_cls.constructed_kinds.count("browser") == 1


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
    session = BrowserSession(config, run_log, run_id="test-run")
    await session.start()

    returned = await session.arun_many(["https://a.test"])

    assert returned == results
    # `start()` also builds the warm HTTP crawler (Phase 2, R6) -- count BROWSER
    # constructions specifically: one from the initial start, one from the relaunch.
    assert fake_cls.constructed_kinds.count("browser") == 2
    kinds = [incident.kind for incident in run_log.incidents()]
    assert "browser_relaunched" in kinds


async def test_a_second_batch_failure_raises_instead_of_relaunching_again(
    install_crawler, make_config
):
    """The relaunch is per-SESSION, not per-call: a second death re-raises to the caller."""
    config = make_config()
    fake_cls = install_crawler([_FakeResult("https://a.test")], fail_batches=2)
    session = BrowserSession(config, run_id="test-run")
    await session.start()

    with pytest.raises(RuntimeError):
        await session.arun_many(["https://a.test"])

    # `start()` also builds the warm HTTP crawler (Phase 2, R6) -- count BROWSER
    # constructions specifically.
    assert fake_cls.constructed_kinds.count("browser") == 2


async def test_close_is_idempotent_and_closes_the_underlying_crawler_once(
    install_crawler, make_config
):
    config = make_config()
    fake_cls = install_crawler([])
    session = BrowserSession(config, run_id="test-run")
    await session.start()

    await session.close()
    await session.close()

    # Two handles (browser + warm HTTP crawler, Phase 2 R6), each closed exactly once.
    assert len(fake_cls.closed) == 2


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
    session = BrowserSession(config, run_log, run_id="test-run")
    await session.start()

    first, second = await asyncio.gather(
        session.arun_many(["https://a.test"]), session.arun_many(["https://b.test"])
    )

    # The fake's `arun_many` returns its full canned `results` regardless of which URL was
    # asked for (see `_make_fake_crawler_class`) -- both retries succeeding is the assertion,
    # not which slice of `results` came back.
    assert first == results
    assert second == results
    # `start()` also builds the warm HTTP crawler (Phase 2, R6) -- count BROWSER
    # constructions specifically.
    assert fake_cls.constructed_kinds.count("browser") == 2, (
        "one shared relaunch, not one per sibling"
    )
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
    session = BrowserSession(config, RunLog(), run_id="test-run")
    await session.start()

    async def _dead_start() -> None:
        raise BrowserPreflightError("Chromium could not be launched: boom (try crawl4ai-setup)")

    monkeypatch.setattr(session, "start", _dead_start)

    with pytest.raises(BrowserPreflightError):
        await session.arun_many(["https://a.test"])

    with pytest.raises(BrowserPreflightError):
        await session.arun_many(["https://a.test"])

    # `start()` also builds the warm HTTP crawler (Phase 2, R6) -- count BROWSER
    # constructions specifically: the ONE real `start()` call, before it was monkeypatched.
    assert fake_cls.constructed_kinds.count("browser") == 1


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
    session = BrowserSession(config, run_log, run_id="test-run")
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
    # `start()` also builds the warm HTTP crawler (Phase 2, R6) -- count BROWSER
    # constructions specifically.
    assert fake_cls.constructed_kinds.count("browser") == 2
    kinds = [incident.kind for incident in run_log.incidents()]
    assert kinds.count("browser_relaunched") == 1


async def test_a_failed_http_half_of_start_closes_the_already_launched_browser(
    install_crawler, monkeypatch, make_config
):
    """The partial-start leak (PR 38 review): Chromium launches, then the warm HTTP crawler
    fails to start. `start()` must close the live Chromium before raising, or every such
    preflight exit orphans a headless browser process -- `__main__`'s handler and the
    mid-run relaunch path both rely on `start()` never leaving a half-open session."""
    config = make_config()
    fake_cls = install_crawler([])

    def _dead_http_crawler(config: object, run_id: str) -> object:
        raise RuntimeError("no sockets left")

    monkeypatch.setattr("harness.tools.fetch._build_http_crawler", _dead_http_crawler)
    session = BrowserSession(config, run_id="test-run")

    with pytest.raises(BrowserPreflightError, match="HTTP fetch strategy"):
        await session.start()

    assert len(fake_cls.closed) == 1, "the launched Chromium must be closed, not leaked"
    # And the session is fully reset: a later `close()` (e.g. `__main__`'s finally) is a no-op.
    await session.close()
    assert len(fake_cls.closed) == 1


async def test_rebind_run_updates_the_run_log_and_the_downloads_dir(install_crawler, make_config):
    """3F Minor b/c: `/new` keeps ONE `BrowserSession` for the whole process (D6), but a
    mid-run relaunch incident must reach the CURRENT run's `RunLog`, not a previous run's
    (or the bootstrap's, `harness/__main__.py`), and the HTTP crawler's downloads dir --
    baked in at construction, `harness/tools/fetch.py`'s `_build_http_crawler` -- must follow
    the CURRENT run's id, not the one the session originally started with.
    """
    config = make_config()
    fake_cls = install_crawler([])
    first_log = RunLog()
    session = BrowserSession(config, first_log, run_id="run-one")
    await session.start()
    first_http = session._http

    second_log = RunLog()
    await session.rebind_run(second_log, "run-two")

    assert session._run_log is second_log
    # A second HTTP crawler was constructed for the new run_id; the first was closed, not
    # left dangling.
    assert fake_cls.constructed_kinds.count("http") == 2
    assert session._http is not first_http
    assert first_http in fake_cls.closed
    http_config = fake_cls.http_strategies[-1].kwargs["browser_config"]
    assert http_config.kwargs["downloads_path"] == str(run_downloads_dir(config, "run-two"))

    await session.close()


async def test_rebind_run_before_start_only_updates_state(install_crawler, make_config):
    """No HTTP crawler exists yet to rebuild -- `rebind_run` must not require one; `start()`
    (or the next relaunch) picks up the new `run_id` on its own."""
    config = make_config()
    install_crawler([])
    session = BrowserSession(config, run_id="run-one")

    await session.rebind_run(RunLog(), "run-two")

    assert session._run_id == "run-two"
    assert session._http is None


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
    session = BrowserSession(config, run_id="test-run")

    with pytest.raises(BrowserPreflightError, match="(?i)chromium"):
        await session.start()
