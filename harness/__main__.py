"""CLI entrypoint: `python -m harness "<question>"`.

Argparse, the welcome screen, the four preflights and the process exit code — everything up to
the point a research session can start. The session itself (the lead's turn loop, its
background researchers, the budgets and the report gate) lives in `harness/session.py`; `main`
builds one `Session`, awaits `run()`, and maps its result onto an exit code: an outcome is 0, a
`None` is 1 with the reason on stderr. The report path is the final line of stdout — frozen,
because R1 depends on it. Nothing may print after it.

The lead may ask clarifying questions via `ask_user` (R2, D5): the run interrupts, this module
prints each question and reads an answer (`_answer_questions`, handed to the session as its
`answer_interrupt` callback), and the session resumes the same thread with it.

With a terminal, `main` also runs a `Composer` beside the session (Phase 3): one `KeyReader`
thread feeds the welcome screen and then the composer, whose lines are queued as `UserMessage`s
for the lead's next turn or handed to an open `ask_user` question. Without one the run is
headless — no composer, no post-report chat, and `_read_answer` reads stdin instead.

`main` also owns the three lifecycles the session borrows and never closes: the
`BrowserSession`, the renderer and the `KeyReader`, all closed in the `finally` around
`Session.run()` (and by `_abort` on every earlier failure exit, since raw mode is entered
before the preflights run). The `KeyReader` additionally sits inside a `finally` spanning
everything past its own construction, so no unexpected exception can hand the shell back in
raw mode. Every error line is printed AFTER that close so it survives the alternate screen.
"""

from __future__ import annotations

import argparse
import asyncio
import queue
import sys
import threading
import tomllib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from rich.console import Console, RenderableType

from harness.activity import ActivitySink
from harness.browser import BrowserPreflightError, BrowserSession
from harness.config import ConfigError, load_config
from harness.display import (
    Activity,
    ComposerDraft,
    Question,
    QuestionAnswered,
    Renderer,
    StageTracker,
    WelcomeScreen,
    WelcomeView,
    build_help_panel,
    build_model_picker,
    build_renderer,
)
from harness.input import KeyEvent, LineBuffer, read_keys, restore_terminal, scoped_keys
from harness.runlog import RunLog
from harness.sources import SourceRegistry, extract_urls
from harness.tools.search import SearchPreflightError, preflight_search

if TYPE_CHECKING:
    # Annotation-only: the runtime imports of langgraph and the agent/model stack are
    # deferred into `main` — they cost several seconds, and `--help` or a config error
    # should not pay them (see the deferred-import block there).
    from langgraph.types import Interrupt

    from harness.config import HarnessConfig
    from harness.report import RunOutcome
    from harness.session import Session

# What the model is told when the developer answers a clarifying question with nothing.
_NO_ANSWER_GIVEN = "(The developer gave no answer to this question.)"

# Roles preflighted before any agent work, in the order they are checked. Adding a role to
# the config means adding it here — nothing else in `main` knows the list. All four roles:
# they share one provider, but a per-MODEL failure (retired ID, quota, the reader tier's
# region opt-in — see docs/decisions.md) passes the head's check and would otherwise surface
# only mid-run, after real budget is spent (PR #18 review).
_PREFLIGHT_ROLES = ("head", "researcher", "reader", "verifier")


def _apply_key(buffer: LineBuffer, event: KeyEvent) -> None:
    """Apply one editing key to `buffer`; anything else (enter, interrupt, eof) is ignored.

    The one buffer-editing dispatch, shared by the welcome screen and the session composer
    (D5). Each place that reads keys still decides what SUBMIT and QUIT mean for it — those
    are the two keys whose meaning differs between them — but the editing keys behave
    identically everywhere, and a second copy of this chain would be where they drift apart.
    """
    if event.kind == "char" and event.char is not None:
        buffer.insert(event.char)
    elif event.kind == "newline":
        buffer.newline()
    elif event.kind == "backspace":
        buffer.backspace()
    elif event.kind == "word_backspace":
        buffer.word_backspace()
    elif event.kind == "left":
        buffer.move_left()
    elif event.kind == "right":
        buffer.move_right()
    elif event.kind == "up":
        buffer.move_up()
    elif event.kind == "down":
        buffer.move_down()


class KeyReader:
    """The session's ONE raw-key source: a daemon thread pushing `KeyEvent`s into a queue (D5).

    Every consumer reads from here — the welcome screen (`blocking()`, a plain iterator, since
    it runs before the event loop matters), and the composer (`get()`, off the loop thread).
    Nothing else may call `read_keys()`: raw mode is process-global, and a second reader would
    race this one for stdin.

    A stdlib `queue.Queue`, not an `asyncio.Queue` fed by `call_soon_threadsafe`: the welcome
    screen is synchronous and blocks the loop thread while it reads, so loop callbacks would
    never run to deliver its keys. `close()`'s `None` sentinel is what keeps the executor
    thread behind a pending `get()` from being left blocked at interpreter shutdown, where the
    Windows `msvcrt` read cannot be interrupted.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[KeyEvent | None] = queue.Queue()
        self._closed = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        """Forward every key until the source ends, then close the iterator behind it."""
        try:
            # `scoped_keys` owns the `termios` restore, exactly as at every other call site.
            with scoped_keys(read_keys()) as keys:
                for event in keys:
                    self._queue.put(event)
        finally:
            # EOF or a raising read: wake whatever is waiting rather than stranding it.
            self._queue.put(None)

    def blocking(self) -> Iterator[KeyEvent]:
        """A blocking iterator over the keys, ending at the close sentinel."""
        while True:
            event = self._queue.get()
            if event is None:
                return
            yield event

    async def get(self) -> KeyEvent | None:
        """The next key, or `None` once closed — awaited without blocking the loop thread.

        The blocking `get` runs on an executor thread, which is what leaves the loop free for
        the wall clock and every other task while nobody is typing.
        """
        return await asyncio.get_running_loop().run_in_executor(None, self._queue.get)

    def close(self) -> None:
        """Release the terminal and unblock any pending `get()`. Idempotent.

        Idempotent because `main` closes it on several paths that can overlap — an ordered
        close beside the renderer teardown, plus the catch-all `finally` that covers an
        unexpected exception — and a second sentinel would sit in the queue behind the first.
        """
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        restore_terminal()


class Composer:
    """The session-long input line: keys in, `UserMessage`s (or one clarifying answer) out (D5).

    Owned by `main` and run as its own task beside `Session.run()`, so typing never waits on a
    model call and a model call never waits on typing (R1). Enter sends: to the pending
    `ask_user` answer if one is open, otherwise onto the session's event queue for the lead's
    next turn.
    """

    def __init__(self, reader: KeyReader, renderer: Renderer, session: Session) -> None:
        self._reader = reader
        self._renderer = renderer
        self._session = session
        self._buffer = LineBuffer()
        self._answer: asyncio.Future[str] | None = None

    async def run(self) -> None:
        self._emit_draft()
        while True:
            event = await self._reader.get()
            if event is None:
                # The key source ended (stdin EOF on the reader thread, or `close()`): nobody
                # can type again, so returning alone strands this composer's consumers — a
                # pending `ask_user` answer would never resolve, and an idle interactive
                # session would wait forever at `events.get()`. `_send("")` resolves the
                # question exactly as an empty Enter does (declined), and drops for the lead.
                self._send("")
                self._session.request_quit()
                return
            if event.kind in ("interrupt", "eof"):
                # The session decides what a quit MEANS (failed run before the report, clean
                # exit after it) — see `Session.request_quit`.
                self._session.request_quit()
                return
            if event.kind == "enter":
                text = self._buffer.text()
                self._buffer = LineBuffer()
                self._send(text)
            else:
                _apply_key(self._buffer, event)
            self._emit_draft()

    def _emit_draft(self) -> None:
        self._renderer.emit(
            ComposerDraft(self._buffer.text(), self._buffer.cursor_row, self._buffer.cursor_col)
        )

    def _send(self, text: str) -> None:
        """Route one submitted line: a pending clarifying answer first, then the lead.

        An empty line is dropped for the lead (a stray Enter is not a message) but ACCEPTED as
        an answer, where `_answer_questions` already turns it into `_NO_ANSWER_GIVEN` — the
        developer must be able to decline a question.
        """
        if self._answer is not None and not self._answer.done():
            self._answer.set_result(text)
            return
        if text.strip():
            self._session.receive_user_message(text)

    async def answer(self) -> str:
        """Wait for the next submitted line, as the answer to an open `ask_user` question."""
        self._answer = asyncio.get_running_loop().create_future()
        try:
            return await self._answer
        finally:
            self._answer = None


async def _read_answer(prompt: str = "> ") -> str:
    """Read one clarification answer without blocking the event loop (risk #2).

    The HEADLESS path only: with a terminal, `Composer.answer()` reads the key thread instead
    (Phase 3), and this module has exactly one `read_keys()` caller. A daemon thread feeding an
    `asyncio.Future`, not `asyncio.to_thread`: the default executor's workers are NOT daemons,
    so the timeout fires on schedule but `asyncio.run()` then blocks at interpreter shutdown
    joining a worker still parked in `input()` — the run would print its cut-short report and
    hang at an already-dead `> ` prompt. The prompt goes to STDERR, not through `input`'s own
    argument, which writes it with no trailing newline and so put a pending `> ` on the same
    stdout line as the report path, breaking the frozen "path is the last line of stdout"
    contract. This is the path CI and piped/scripted runs take.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def _worker() -> None:
        try:
            print(prompt, end="", file=sys.stderr, flush=True)
            answer = input()
        except (EOFError, OSError):
            # stdin is closed or unreadable (piped from /dev/null, run under a service
            # manager). Resolve with nothing rather than dying before `_resolve` is
            # scheduled, which left `await future` pending forever with no report at all.
            # `_answer_questions` turns "" into `_NO_ANSWER_GIVEN`, so the model is told
            # the question went unanswered and the run proceeds to a report like any other.
            answer = ""

        def _resolve() -> None:
            # The wall clock may already have cancelled this future — setting a result on
            # a done future raises `InvalidStateError`.
            if not future.done():
                future.set_result(answer)

        try:
            loop.call_soon_threadsafe(_resolve)
        except RuntimeError:
            # The loop is already closed — nothing left to resolve, and this must not
            # raise out of a daemon thread.
            pass

    threading.Thread(target=_worker, daemon=True).start()
    return await future


async def _answer_questions(
    interrupt: Interrupt,
    renderer: Renderer,
    registry: SourceRegistry,
    tracker: StageTracker,
    composer: Composer | None = None,
) -> list[dict[str, Any]]:
    """Render each pending `ask_user` question and collect one answer per action request.

    One decision per request, in the same order — the middleware raises `ValueError` on a
    count mismatch.

    An answer is user-supplied text exactly like the initial question, so any URL pasted into
    it is approved here (Phase 4, D2/R2) — the natural reply to "which page do you mean?" is
    the URL itself, and without this it stayed provenance-rejected for the rest of the run.

    The overlay is in-frame now (Phase 5): no `suspend()`. `tracker.pause()`/`resume()` and
    the `QuestionAnswered` emit sit around `_read_answer` in a `finally`, so a `KeyboardInterrupt`
    or a wall-clock cancellation mid-question cannot leave the displayed clock paused and the
    overlay stuck open. The WALL clock (`asyncio.timeout` in `main`) is a different clock
    entirely (risk #2) and nothing here touches it.
    """
    decisions: list[dict[str, Any]] = []
    for request in interrupt.value["action_requests"]:
        args = request.get("args", {})
        question = args.get("question") or request.get("description") or str(args)
        renderer.emit(Question(question))
        tracker.pause()
        try:
            # With a terminal the composer is already reading keys, so the answer comes from
            # the same line the developer types everything else into; headless runs read stdin.
            answer = await (composer.answer() if composer is not None else _read_answer())
        finally:
            tracker.resume()
            renderer.emit(QuestionAnswered())
        for url in extract_urls(answer):
            registry.approve(url)
        # Best-effort + disclose: a bare Enter must not reach the model as an empty tool
        # result, which reads as "answered with nothing said" and hides the open ambiguity.
        decisions.append({"type": "respond", "message": answer.strip() or _NO_ANSWER_GIVEN})
    return decisions


# --- Welcome screen (Phase 2, PLAN-tui-redesign) --------------------------------------------
#
# `python -m harness` with no argv `question` drops into an interactive welcome screen instead
# of erroring on a missing positional (D2 — argv mode itself is unchanged, see `main`).


@dataclass
class _WelcomeState:
    """Mutable welcome-loop state, separate from the pure `WelcomeView` it renders into."""

    mode: Literal["normal", "model_picker"] = "normal"
    picker_index: int = 0
    notice: str | None = None
    panel: RenderableType | None = None


@dataclass(frozen=True)
class _Command:
    name: str
    summary: str
    handler: Callable[[_WelcomeState, HarnessConfig], None]


def _handle_help(state: _WelcomeState, config: HarnessConfig) -> None:
    rows = [(command.name, command.summary) for command in _COMMANDS.values()]
    state.panel = build_help_panel(rows)


def _handle_model(state: _WelcomeState, config: HarnessConfig) -> None:
    head = config.roles["head"]
    choices = head.choices or []
    state.mode = "model_picker"
    state.picker_index = choices.index(head.model) if head.model in choices else 0
    state.panel = build_model_picker(choices, state.picker_index, head.model)


# A DATA structure (name -> `_Command`), not an if/elif chain (Contracts): adding `/sources`
# later is one entry here plus its handler, no change to `_run_welcome`'s loop.
#
# That holds for a command that only paints a panel. A command needing its OWN key handling
# does not get off as cheaply: `_run_welcome`'s `up`/`down`/`enter` branches test
# `state.mode == "model_picker"` inline, so a second interactive command means a second mode
# branch in that shared loop. Give `_Command` an optional key handler before adding one,
# rather than growing the chain this table exists to avoid (PR #25 review).
_COMMANDS: dict[str, _Command] = {
    "/help": _Command("/help", "Show available commands and key hints.", _handle_help),
    "/model": _Command("/model", "Pick the head model for this session.", _handle_model),
}


@dataclass(frozen=True)
class _Submission:
    kind: Literal["question", "command", "unknown", "empty"]
    text: str


def _classify_submission(text: str) -> _Submission:
    """Pure classification of one submitted buffer, table-driven off `_COMMANDS`.

    Rules: empty/whitespace -> `empty`; leading `/` -> looked up in `_COMMANDS` -> `command`
    or `unknown`; anything else -> `question` (leading non-slash text is a question even if
    it contains a `/` later, e.g. `"a/b"`).
    """
    stripped = text.strip()
    if not stripped:
        return _Submission("empty", stripped)
    if stripped.startswith("/"):
        if stripped in _COMMANDS:
            return _Submission("command", stripped)
        return _Submission("unknown", stripped)
    return _Submission("question", stripped)


def _package_version() -> str:
    """The version shown in the welcome screen's status bar, read from `pyproject.toml`.

    Not `importlib.metadata.version`: `[tool.uv] package = false` means this project is never
    actually installed as a package, so that lookup would raise `PackageNotFoundError`.
    """
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    return str(data["project"]["version"])


def _run_welcome(
    config: HarnessConfig, *, keys: Iterable[KeyEvent], console: Console
) -> str | None:
    """Drive the welcome screen until a question is submitted or the user aborts.

    `keys`/`console` are the testability seam: tests feed a list of `KeyEvent`s and a
    `StringIO`-backed `Console`, no terminal involved. Production passes the session
    `KeyReader`'s blocking iterator (Phase 3) and a fresh `Console`.
    """
    buffer = LineBuffer()
    state = _WelcomeState()
    head = config.roles["head"]
    roles = (
        ("researcher", config.roles["researcher"].model),
        ("reader", config.roles["reader"].model),
        ("verifier", config.roles["verifier"].model),
    )
    budget = f"{config.agent.max_rounds} rounds / {config.agent.wall_clock_seconds // 60} min"
    status_left = f"{Path.cwd()}:searxng@{urlsplit(config.search.base_url).netloc}"
    status_right = _package_version()

    def _view() -> WelcomeView:
        panel = state.panel
        if state.mode == "model_picker" and head.choices:
            panel = build_model_picker(head.choices, state.picker_index, head.model)
        return WelcomeView(
            question=buffer.text(),
            cursor_col=buffer.cursor_col,
            cursor_row=buffer.cursor_row,
            head_model=head.model,
            roles=roles,
            budget=budget,
            status_left=status_left,
            status_right=status_right,
            notice=state.notice,
            panel=panel,
        )

    # `scoped_keys` (harness/input.py) owns releasing raw mode — the close probe lives once,
    # next to the generator that sets raw mode, rather than at each call site. Nesting it
    # INSIDE `WelcomeScreen` is what gives the risk #1 ordering: the key source's `termios`
    # restore runs BEFORE `WelcomeScreen` leaves the alternate screen, so a Ctrl+C or
    # exception mid-loop cannot leave the terminal raw. Do not reorder these.
    with WelcomeScreen(console) as screen, scoped_keys(keys) as key_source:
        screen.update(_view())
        for event in key_source:
            if event.kind in ("interrupt", "eof"):
                # Both quit keys leave the welcome screen: Ctrl+C aborts, and Ctrl+D (eof) is
                # the shell habit for "done here" — on a screen with nothing to lose, both are
                # a clean exit with no run started (3F Minor c; decode_posix/decode_windows
                # already distinguish them for callers that care).
                return None
            elif event.kind == "up" and state.mode == "model_picker":
                state.picker_index = max(0, state.picker_index - 1)
            elif event.kind == "down" and state.mode == "model_picker":
                choices = head.choices or []
                if choices:
                    state.picker_index = min(len(choices) - 1, state.picker_index + 1)
            elif event.kind == "enter":
                if state.mode == "model_picker":
                    if head.choices:
                        head.model = head.choices[state.picker_index]
                    state.mode = "normal"
                    state.panel = None
                else:
                    submission = _classify_submission(buffer.text())
                    if submission.kind == "question":
                        return submission.text
                    if submission.kind == "command":
                        state.notice = None
                        _COMMANDS[submission.text].handler(state, config)
                        buffer = LineBuffer()
                    elif submission.kind == "unknown":
                        state.notice = f"unknown command: {submission.text}"
                        state.panel = None
                        buffer = LineBuffer()
                    # empty -> ignore, nothing submitted
            else:
                # Every editing key, through the dispatch the composer shares (Phase 3).
                _apply_key(buffer, event)
            screen.update(_view())
    return None


async def main(argv: list[str] | None = None) -> int:
    """Run one research question end to end. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m harness", description="Answer one research question with cited sources."
    )
    # `nargs="?"`: `python -m harness "<question>"` still parses identically to before this
    # phase — only an ABSENT question now falls through to the welcome screen below, rather
    # than argparse itself erroring on a missing required positional (D2).
    parser.add_argument("question", nargs="?", help="The research question to answer.")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    question = args.question
    if not question and not sys.stdin.isatty():
        # No question AND no interactive terminal (piped stdin, cron, nohup): the welcome
        # screen cannot be driven, and `read_keys()` would raise a bare `termios.error`
        # putting stdin in raw mode. Restore the pre-welcome behavior for this case — the
        # same usage message argparse produced when `question` was a required positional.
        parser.error("the following arguments are required: question")
    # ONE key source for the whole session, started before the welcome screen and handed to
    # every consumer after it (D5). `None` without a terminal: there are no raw keys to read,
    # the welcome screen is unreachable, and `_read_answer`'s stdin path answers questions.
    reader = KeyReader() if sys.stdin.isatty() else None

    # One `try/finally` around everything past the reader's construction: raw mode is
    # entered WITH the reader, so an unexpected exception anywhere below (not just the
    # failures `_abort` and the run's own `finally` anticipate) would otherwise hand the
    # developer's shell back in raw mode. `close()` is idempotent, so the ordered closes
    # inside — which must run BEFORE the renderer tears the alternate screen down — stay
    # where they are and this one is a no-op after them.
    try:
        if not question:
            assert reader is not None  # guaranteed by the `parser.error` above
            # `reader.blocking()`, not a fresh `read_keys()`: the welcome screen borrows the
            # session's key thread rather than opening a second one. See `_run_welcome`'s own
            # docstring for the testability seam and the terminal-teardown ordering it relies on.
            question = _run_welcome(config, keys=reader.blocking(), console=Console())
            if question is None:
                # Ctrl+C or EOF out of the welcome screen: a clean exit, no run started, no
                # report. The outer `finally` closes the reader.
                return 0

        # The renderer starts BEFORE the heavy imports and the preflight call: together they are
        # several silent seconds, and a terminal that shows nothing through them reads as hung.
        renderer = build_renderer()
        tracker = StageTracker(renderer)

        def _abort(message: str) -> int:
            """Every pre-session failure exit: release the terminal, then print (never before).

            Raw mode is now entered before the preflights run, so an early return has to leave it
            — otherwise a failed preflight hands the developer's shell back in raw mode. The
            reader closes BEFORE the renderer for the same reason the welcome screen closes its
            key source first: the restore must happen while the alternate screen still owns the
            terminal. The print comes last because anything written under a `Live(screen=True)` is
            discarded with that screen.
            """
            if reader is not None:
                reader.close()
            renderer.close()
            print(message, file=sys.stderr)
            return 1

        renderer.emit(Activity("loading the agent stack"))
        # Deferred on purpose: deepagents (via harness.agent) and openai (via harness.models)
        # cost ~5s of import time between them — `--help`, a config error, and the first painted
        # frame must not wait on them. Tests patch `harness.models.preflight` and
        # `harness.models.build_chat_model`; binding at call time picks those patches up.
        from harness.models import ModelError, preflight
        from harness.session import Session

        # Every role the run will actually call, checked before any agent work. A loop, not a
        # block per role: Phase 2 adds more, and each pasted copy is another place the close/print
        # /exit-1 shape can drift.
        for role in _PREFLIGHT_ROLES:
            renderer.emit(Activity(f"preflight: checking the {role} model endpoint"))
            try:
                await preflight(config, role)
            except ModelError as exc:
                return _abort(f"error: {exc}")

        renderer.emit(Activity("preflight: checking the search backend"))
        try:
            await preflight_search(config)
        except SearchPreflightError as exc:
            return _abort(f"error: {exc}")

        # Stamped before the agent can write anything: a cut-short report uses it to tell THIS
        # run's workspace notes from a previous run's leftovers (`report._notes_section`), and it
        # names this run's `<workspace_dir>/<run_id>/` directory, which keeps a previous run's
        # captures out of this run's verification.
        started_at = datetime.now()

        registry = SourceRegistry(run_id=started_at.strftime("%Y-%m-%d-%H%M%S"))
        # Phase 4 strict provenance (D2/R2): a pasted "read this page" URL is the only other
        # sanctioned way a URL becomes fetchable (no `--url` flag) — approved here, before the
        # agent runs, so it is fetchable from the run's very first tool call.
        for url in extract_urls(question):
            registry.approve(url)
        run_log = RunLog()

        # After `run_log`, not beside the other preflights above: `BrowserSession` records its OWN
        # relaunch incident onto it (Phase 1 Discoveries) rather than taking a caller-supplied hook,
        # so it cannot be constructed any earlier.
        renderer.emit(Activity("preflight: launching the browser"))
        browser = BrowserSession(config, run_log, run_id=registry.run_id)
        try:
            await browser.start()
        except BrowserPreflightError as exc:
            return _abort(f"error: {exc}")

        # `ActivitySink` PUSHES via `on_change` rather than being drained from the turn loop: the
        # middleware writes from inside a researcher's own graph, and one node is one superstep, so
        # no top-level chunk arrives until the whole researcher->reader pipeline has finished. The
        # thunk exists because the sink must be constructed BEFORE the `Session` that services it
        # (it is a constructor argument) while the callback is a method ON that session; `session`
        # is only read at call time, and nothing can push into the sink before it exists.
        session: Session | None = None

        def _on_activity_change() -> None:
            if session is not None:
                session.on_activity_change()

        # Same thunk trick as `_on_activity_change`, for the same reason: the composer needs the
        # session it drives, and the session needs the composer's `answer()` for `ask_user`.
        composer: Composer | None = None

        sink = ActivitySink(on_change=_on_activity_change)
        session = Session(
            config,
            registry,
            run_log,
            renderer,
            tracker,
            question,
            sink=sink,
            browser=browser,
            # `_answer_questions` keeps its `renderer`/`registry`/`tracker` arguments and stays in
            # this module (Phase 4 moves the interrupt handling itself); the session only needs
            # "given an interrupt, get me the decisions".
            answer_interrupt=lambda interrupt: _answer_questions(
                interrupt, renderer, registry, tracker, composer
            ),
            started_at=started_at,
            # A key source is what makes a session interactive: with one, an idle lead waits for
            # the developer and the chat continues past the report; without one, the run is
            # headless and ends at the report exactly as before (Phase 3).
            interactive=reader is not None,
        )
        if reader is not None:
            composer = Composer(reader, renderer, session)

        # Same close/print/exit-1 shape as the preflight loop: the session resolves every role
        # through `build_chat_model`, so a missing or TODO role raises `ModelError` on the first
        # line of `run()` — unhandled it would escape as a traceback under the alternate screen
        # (PR #18 review).
        outcome: RunOutcome | None = None
        model_error: str | None = None
        composer_task: asyncio.Task[None] | None = None
        # `finally`, because the live region owns terminal state: `Live.start` hides the
        # cursor and rich registers no atexit restore, so a `write_report` OSError (an
        # unwritable or full reports dir) escaping `run()` would leave the developer's shell
        # with no cursor after the traceback.
        try:
            # Two tasks, not one: the composer reads keys for as long as the session lives, so
            # neither ever waits on the other (R1). The SESSION decides when the run is over —
            # the composer is torn down after it, never the other way round.
            if composer is not None:
                composer_task = asyncio.create_task(composer.run())
            outcome = await session.run()
        except ModelError as exc:
            model_error = f"error: {exc}"
        finally:
            if composer_task is not None:
                composer_task.cancel()
                # Awaited, not just cancelled: an un-retrieved cancelled task is reported as
                # "Task was destroyed but it is pending" when the loop closes.
                await asyncio.gather(composer_task, return_exceptions=True)
            if reader is not None:
                reader.close()
            await browser.close()
            renderer.close()

        # Error prints belong AFTER close(): under `Live(screen=True)` anything written to the
        # terminal — stderr included, it shares the device — while the Live runs lands on the
        # alternate screen and is discarded with it. Down here the normal buffer is restored, so
        # the detail survives the run.
        if model_error is not None:
            print(model_error, file=sys.stderr)
            return 1
        if outcome is None:
            if session.cut_short == "wall_clock":
                # The wall clock fired before a submitted answer existed.
                print(
                    "error: the wall clock expired before a final answer existed", file=sys.stderr
                )
            else:
                print(f"error: {session.cut_short_detail}", file=sys.stderr)
            return 1
        return 0
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
