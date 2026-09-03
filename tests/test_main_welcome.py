"""Behavioral tests for the welcome screen loop in harness.__main__ (Phase 2)."""

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

import harness.__main__ as main_module
from harness.config import RoleConfig
from harness.input import KeyEvent

_HARNESS_TOML = Path(__file__).resolve().parent.parent / "harness.toml"


def _console() -> Console:
    return Console(file=StringIO())


def _type(text: str) -> list[KeyEvent]:
    return [KeyEvent("char", ch) for ch in text]


def test_typing_a_question_then_enter_returns_it(make_config):
    config = make_config()
    keys = [*_type("hi"), KeyEvent("enter")]

    result = main_module._run_welcome(config, keys=keys, console=_console())

    assert result == "hi"


def test_word_backspace_deletes_the_trailing_word_before_submit(make_config):
    config = make_config()
    keys = [
        *_type("solar tariffs"),
        KeyEvent("word_backspace"),
        *_type("prices"),
        KeyEvent("enter"),
    ]

    result = main_module._run_welcome(config, keys=keys, console=_console())

    assert result == "solar prices"


def test_interrupt_returns_none(make_config):
    config = make_config()
    keys = [*_type("something"), KeyEvent("interrupt")]

    result = main_module._run_welcome(config, keys=keys, console=_console())

    assert result is None


def test_eof_returns_none_like_interrupt(make_config):
    """3F Minor c: Ctrl+D is decoded (`KeyEvent("eof")`) and must not be silently ignored —
    on the welcome screen it quits cleanly, exactly like Ctrl+C, returning no question.
    """
    config = make_config()
    keys = [*_type("something"), KeyEvent("eof")]

    result = main_module._run_welcome(config, keys=keys, console=_console())

    assert result is None


def test_the_key_source_is_closed_before_the_live_screen_is_torn_down(make_config, monkeypatch):
    """Risk #1's ordering guarantee, pinned so it does not rest on a manual terminal check.

    `read_keys()` restores `termios` in its `finally`, which runs when the generator is
    closed. That close must happen BEFORE `Live` leaves the alternate screen, so a Ctrl+C
    mid-loop cannot leave the terminal in raw mode.
    """
    config = make_config()
    order: list[str] = []

    def _generator_keys():
        try:
            yield KeyEvent("interrupt")
        finally:
            order.append("keys closed")

    real_screen = main_module.WelcomeScreen

    class _RecordingScreen(real_screen):
        def __exit__(self, *exc_info):
            order.append("screen exited")
            return super().__exit__(*exc_info)

    monkeypatch.setattr(main_module, "WelcomeScreen", _RecordingScreen)

    main_module._run_welcome(config, keys=_generator_keys(), console=_console())

    assert order == ["keys closed", "screen exited"]


@pytest.mark.parametrize(
    ("text", "expected_kind"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("/help", "command"),
        ("/model", "command"),
        ("/nope", "unknown"),
        ("/sources", "unknown"),  # pins the amendment: /sources is dropped, not registered
        ("what is x", "question"),
        ("a/b", "question"),
    ],
)
def test_classify_submission_table(text, expected_kind):
    submission = main_module._classify_submission(text)

    assert submission.kind == expected_kind


def test_model_flow_opens_picker_and_applies_the_highlighted_choice(make_config):
    config = make_config()
    config.roles["head"] = RoleConfig(
        provider="opencode", model="glm-5.2", choices=["glm-5.2", "glm-5.3", "kimi-k3", "hy3"]
    )
    before = _HARNESS_TOML.read_bytes()
    keys = [
        *_type("/model"),
        KeyEvent("enter"),  # opens the picker, starting on the current model
        KeyEvent("down"),
        KeyEvent("down"),
        KeyEvent("enter"),  # applies the highlighted choice
        KeyEvent("interrupt"),  # end the loop; nothing left to submit
    ]

    result = main_module._run_welcome(config, keys=keys, console=_console())

    assert result is None
    assert config.roles["head"].model == "kimi-k3"  # choices[2], two downs from choices[0]
    assert _HARNESS_TOML.read_bytes() == before


def test_model_picker_clamps_at_both_ends(make_config):
    config = make_config()
    config.roles["head"] = RoleConfig(
        provider="opencode", model="glm-5.2", choices=["glm-5.2", "glm-5.3", "kimi-k3"]
    )
    keys = [
        *_type("/model"),
        KeyEvent("enter"),  # picker index starts at 0 (current model)
        KeyEvent("up"),  # must clamp at 0, not wrap to the last entry
        KeyEvent("down"),
        KeyEvent("down"),  # now at the last index (2)
        KeyEvent("down"),  # must clamp at the last index, not wrap to 0
        KeyEvent("enter"),
        KeyEvent("interrupt"),
    ]

    main_module._run_welcome(config, keys=keys, console=_console())

    assert config.roles["head"].model == "kimi-k3"


def test_unknown_command_sets_a_notice_and_does_not_exit_the_loop(make_config):
    config = make_config()
    keys = [
        *_type("/nope"),
        KeyEvent("enter"),
        *_type("hi"),
        KeyEvent("enter"),
    ]

    result = main_module._run_welcome(config, keys=keys, console=_console())

    # The unknown command did not end the loop — a later real question still gets through.
    assert result == "hi"


def test_unknown_command_notice_names_the_command(make_config, monkeypatch):
    config = make_config()
    notices: list[str] = []

    class _FakeScreen:
        def __init__(self, console):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def update(self, view):
            if view.notice:
                notices.append(view.notice)

    monkeypatch.setattr(main_module, "WelcomeScreen", _FakeScreen)
    keys = [*_type("/nope"), KeyEvent("enter"), KeyEvent("interrupt")]

    main_module._run_welcome(config, keys=keys, console=_console())

    assert notices
    assert "/nope" in notices[0]


async def test_main_with_a_question_on_argv_never_enters_the_welcome_loop(make_config, monkeypatch):
    from harness.models import ModelError

    monkeypatch.setattr(main_module, "load_config", lambda: make_config())

    def _boom(*args, **kwargs):
        raise AssertionError("_run_welcome must not be called when argv already has a question")

    monkeypatch.setattr(main_module, "_run_welcome", _boom)

    async def _fake_preflight(*args, **kwargs):
        raise ModelError("stop here — this test only checks _run_welcome was not called")

    # Deferred `from harness.models import preflight` inside `main` rebinds to this patched
    # attribute at call time, matching the existing pattern in tests/test_agent.py.
    monkeypatch.setattr("harness.models.preflight", _fake_preflight)

    exit_code = await main_module.main(["a question"])

    assert exit_code == 1


async def test_main_with_no_argv_question_reaches_the_welcome_loop(make_config, monkeypatch):
    monkeypatch.setattr(main_module, "load_config", lambda: make_config())
    # An interactive terminal is a precondition for the welcome screen: without a tty there
    # is nothing to drive it, so `main` falls back to argparse's usage error instead (see
    # the non-tty test below). pytest's captured stdin is not a tty, so say so explicitly.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # `main` starts the session key reader as soon as it believes it has a terminal (Phase 3),
    # and the real `read_keys()` would then put pytest's captured stdin into raw mode — a bare
    # `termios.error` on the reader thread. A source that ends at once stands in for it.
    monkeypatch.setattr(main_module, "read_keys", lambda: iter(()))
    calls = {"count": 0}

    def _fake_run_welcome(config, *, keys, console):
        calls["count"] += 1
        return None

    monkeypatch.setattr(main_module, "_run_welcome", _fake_run_welcome)

    exit_code = await main_module.main([])

    assert calls["count"] == 1
    assert exit_code == 0


async def test_no_argv_question_without_a_tty_errors_instead_of_reading_raw_keys(
    make_config, monkeypatch
):
    """Piped stdin / cron / nohup must not reach `read_keys()`.

    Before the welcome screen existed, argparse rejected a missing positional. Entering raw
    mode on a non-tty raises `termios.error` instead, so the non-interactive path keeps the
    old usage error.
    """
    monkeypatch.setattr(main_module, "load_config", lambda: make_config())
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def _boom(*args, **kwargs):
        raise AssertionError("_run_welcome must not be called without a tty")

    monkeypatch.setattr(main_module, "_run_welcome", _boom)

    with pytest.raises(SystemExit) as excinfo:
        await main_module.main([])

    assert excinfo.value.code == 2


async def test_an_unexpected_failure_after_the_reader_still_restores_the_terminal(
    make_config, monkeypatch
):
    """Round 2, item 3: raw mode is entered with the `KeyReader`, so EVERY exit out of `main`
    past that point has to release it.

    `_abort` and the run's own `finally` cover the paths anyone anticipated; an unexpected
    exception in between (here `build_renderer`, standing in for any step after the reader
    exists) escaped with the developer's shell still in raw mode — no echo, no line editing,
    on a terminal that also has a traceback on it.
    """
    monkeypatch.setattr(main_module, "load_config", lambda: make_config())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # The real `read_keys()` would put pytest's captured stdin into raw mode; a source that
    # ends at once stands in for it, exactly as in the welcome-loop test above.
    monkeypatch.setattr(main_module, "read_keys", lambda: iter(()))

    closes = {"count": 0}

    class _ClosureRecordingReader(main_module.KeyReader):
        def close(self) -> None:
            closes["count"] += 1
            super().close()

    monkeypatch.setattr(main_module, "KeyReader", _ClosureRecordingReader)

    def _boom():
        raise RuntimeError("the renderer could not start")

    monkeypatch.setattr(main_module, "build_renderer", _boom)

    with pytest.raises(RuntimeError):
        await main_module.main(["a question"])

    assert closes["count"] >= 1, "the key reader was never closed — the terminal stayed raw"


async def test_a_restart_returns_to_welcome_once_then_a_quit_exits(make_config, monkeypatch):
    """Phase 6 D6: a session that requests a restart sends control back to the welcome
    screen exactly once -- the browser is created/started/closed once each across BOTH
    iterations (it lives outside the loop), and the second run's registry gets a fresh
    `run_id`.
    """
    monkeypatch.setattr(main_module, "load_config", lambda: make_config())
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(main_module, "read_keys", lambda: iter(()))

    # Neutralize the preflights (as `patch_run` does for a `main()` test), since a fake
    # `Session` never reaches the model or search backends this run would otherwise probe.
    async def _noop_preflight(cfg, role):
        return None

    monkeypatch.setattr("harness.models.preflight", _noop_preflight)

    async def _noop_search_preflight(cfg):
        return None

    monkeypatch.setattr(main_module, "preflight_search", _noop_search_preflight)

    # Two questions: the first session requests a restart, so the welcome screen is shown a
    # SECOND time and hands back a second question -- that second session ends in an ordinary
    # quit-after-report (R5: `run()` returns the outcome, exit 0), not a further restart.
    welcome_calls = {"count": 0}

    def _fake_run_welcome(config, *, keys, console):
        welcome_calls["count"] += 1
        return "question one" if welcome_calls["count"] == 1 else "question two"

    monkeypatch.setattr(main_module, "_run_welcome", _fake_run_welcome)

    browser_calls = {"start": 0, "close": 0}

    async def _counting_start(self):
        browser_calls["start"] += 1

    async def _counting_close(self):
        browser_calls["close"] += 1

    monkeypatch.setattr(main_module.BrowserSession, "start", _counting_start)
    monkeypatch.setattr(main_module.BrowserSession, "close", _counting_close)

    run_ids: list[str] = []
    session_calls = {"count": 0}

    class _FakeSession:
        def __init__(
            self,
            config,
            registry,
            run_log,
            renderer,
            tracker,
            question,
            *,
            sink,
            browser,
            answer_source,
            started_at,
            interactive,
        ):
            session_calls["count"] += 1
            run_ids.append(registry.run_id)
            self._is_first = session_calls["count"] == 1
            self.restart_requested = self._is_first
            self.cut_short = None
            self.cut_short_detail = None

        async def run(self):
            # The first session's `/new` never wrote a report (`None`, D6); the second is an
            # ordinary successful run whose outcome `run()` hands back (R5).
            return None if self._is_first else "outcome-sentinel"

    monkeypatch.setattr("harness.session.Session", _FakeSession)

    exit_code = await main_module.main([])

    assert welcome_calls["count"] == 2
    assert session_calls["count"] == 2
    assert browser_calls["start"] == 1
    assert browser_calls["close"] == 1
    assert len(set(run_ids)) == 2
    assert exit_code == 0


def test_the_welcome_screen_reads_from_the_session_long_key_readers_iterator(
    make_config, monkeypatch
):
    """Phase 3/D5: the welcome screen no longer owns its key source — `main` starts ONE
    `KeyReader` for the whole session and hands `_run_welcome` that reader's blocking
    iterator, so the same thread later feeds the composer.

    The iterator must therefore behave exactly like the raw `read_keys()` generator did:
    block for each key, and end when the reader closes (its `None` sentinel).
    """
    config = make_config()

    def _fake_read_keys():
        yield from [*_type("q"), KeyEvent("enter")]

    monkeypatch.setattr(main_module, "read_keys", _fake_read_keys)
    reader = main_module.KeyReader()
    try:
        result = main_module._run_welcome(config, keys=reader.blocking(), console=_console())
    finally:
        reader.close()

    assert result == "q"
