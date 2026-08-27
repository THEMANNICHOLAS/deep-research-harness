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

`main` also owns the two lifecycles the session borrows and never closes: the `BrowserSession`
and the renderer, both closed in the `finally` around `Session.run()`, and every error line is
printed AFTER that close so it survives the alternate screen.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import tomllib
from collections.abc import Callable, Iterable
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
    AnswerDraft,
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


async def _read_answer(renderer: Renderer, prompt: str = "> ") -> str:
    """Read one clarification answer without blocking the event loop (risk #2).

    Non-TTY (`not sys.stdin.isatty()`): unchanged from before this phase, byte-for-byte -- a
    daemon thread feeding an `asyncio.Future`, not `asyncio.to_thread`: the default executor's
    workers are NOT daemons, so the timeout fires on schedule but `asyncio.run()` then blocks
    at interpreter shutdown joining a worker still parked in `input()` — the run would print
    its cut-short report and hang at an already-dead `> ` prompt. The prompt goes to STDERR,
    not through `input`'s own argument, which writes it with no trailing newline and so put a
    pending `> ` on the same stdout line as the report path, breaking the frozen "path is the
    last line of stdout" contract. This is the path CI and piped/scripted runs take — raw mode
    requires a real tty.

    TTY: the `ask_user` overlay is the prompt (no `suspend()`, nothing written to stderr — a
    write during a `screen=True` Live lands on the alternate screen anyway). A daemon thread
    runs the blocking `read_keys()` generator and forwards each event through an
    `asyncio.Queue` via `call_soon_threadsafe`; the async side only ever `await`s that queue,
    which is what keeps the loop thread free so the wall clock (`asyncio.timeout` in `main`)
    can still fire while a question is pending.
    """
    if not sys.stdin.isatty():
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

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[KeyEvent] = asyncio.Queue()

    def _key_worker() -> None:
        with scoped_keys(read_keys()) as keys:
            for event in keys:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except RuntimeError:
                    # The loop is already closed — nothing left to forward.
                    return
                if event.kind in ("enter", "interrupt"):
                    # Return (rather than break) so this happens INSIDE the `with`, letting
                    # `scoped_keys`' close probe run the generator's own `finally` teardown on
                    # this, the happy, path.
                    return
        # The generator ended without an `enter`/`interrupt` (EOF): forward a synthetic
        # `enter` so the async side is never left awaiting forever — `_answer_questions`
        # turns an empty answer into `_NO_ANSWER_GIVEN`.
        try:
            loop.call_soon_threadsafe(queue.put_nowait, KeyEvent("enter"))
        except RuntimeError:
            pass

    threading.Thread(target=_key_worker, daemon=True).start()

    buffer = LineBuffer()
    try:
        while True:
            # The await is what keeps the wall clock alive: the loop thread stays free while
            # this is pending, so `asyncio.timeout` can fire even with a question open.
            event = await queue.get()
            if event.kind == "enter":
                break
            if event.kind == "interrupt":
                raise KeyboardInterrupt  # R6 -- `main()`'s existing Ctrl+C teardown runs.
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
            renderer.emit(AnswerDraft(buffer.text(), buffer.cursor_row, buffer.cursor_col))
        return buffer.text()
    finally:
        # Runs on submit, on `KeyboardInterrupt` above, and on `CancelledError` when the wall
        # clock expires with the overlay open — the one restore path is idempotent (step 1)
        # regardless of which of those three ways this is left.
        restore_terminal()


async def _answer_questions(
    interrupt: Interrupt, renderer: Renderer, registry: SourceRegistry, tracker: StageTracker
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
            answer = await _read_answer(renderer)
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
    `StringIO`-backed `Console`, no terminal involved. Production passes `read_keys()` and a
    fresh `Console`.
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
            if event.kind == "interrupt":
                return None
            elif event.kind == "char" and event.char is not None:
                buffer.insert(event.char)
            elif event.kind == "backspace":
                buffer.backspace()
            elif event.kind == "word_backspace":
                buffer.word_backspace()
            elif event.kind == "left":
                buffer.move_left()
            elif event.kind == "right":
                buffer.move_right()
            elif event.kind == "newline":
                buffer.newline()
            elif event.kind == "up":
                if state.mode == "model_picker":
                    state.picker_index = max(0, state.picker_index - 1)
                else:
                    buffer.move_up()
            elif event.kind == "down":
                if state.mode == "model_picker":
                    choices = head.choices or []
                    if choices:
                        state.picker_index = min(len(choices) - 1, state.picker_index + 1)
                else:
                    buffer.move_down()
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
    if not question:
        # Production passes the raw `read_keys()` generator and a fresh `Console` — see
        # `_run_welcome`'s own docstring for the testability seam and the terminal-teardown
        # ordering this relies on.
        question = _run_welcome(config, keys=read_keys(), console=Console())
        if question is None:
            # Ctrl+C or EOF out of the welcome screen: a clean exit, no run started, no report.
            return 0

    # The renderer starts BEFORE the heavy imports and the preflight call: together they are
    # several silent seconds, and a terminal that shows nothing through them reads as hung.
    renderer = build_renderer()
    tracker = StageTracker(renderer)

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
            renderer.close()
            print(f"error: {exc}", file=sys.stderr)
            return 1

    renderer.emit(Activity("preflight: checking the search backend"))
    try:
        await preflight_search(config)
    except SearchPreflightError as exc:
        # `renderer.close()` first, matching the model-preflight path above: the TUI owns the
        # alternate screen, so an error printed under it would vanish when the screen exits.
        renderer.close()
        print(f"error: {exc}", file=sys.stderr)
        return 1

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
        renderer.close()
        print(f"error: {exc}", file=sys.stderr)
        return 1

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
            interrupt, renderer, registry, tracker
        ),
        started_at=started_at,
    )

    # Same close/print/exit-1 shape as the preflight loop: the session resolves every role
    # through `build_chat_model`, so a missing or TODO role raises `ModelError` on the first
    # line of `run()` — unhandled it would escape as a traceback under the alternate screen
    # (PR #18 review).
    outcome: RunOutcome | None = None
    model_error: str | None = None
    # `finally`, because the live region owns terminal state: `Live.start` hides the cursor and
    # rich registers no atexit restore, so a `write_report` OSError (unwritable or full reports
    # dir) escaping `run()` would leave the developer's shell with no cursor after the traceback.
    try:
        outcome = await session.run()
    except ModelError as exc:
        model_error = f"error: {exc}"
    finally:
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
            print("error: the wall clock expired before a final answer existed", file=sys.stderr)
        else:
            print(f"error: {session.cut_short_detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
