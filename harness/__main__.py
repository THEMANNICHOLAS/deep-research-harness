"""CLI entrypoint: `python -m harness "<question>"`.

Loads config, preflights the `head` and `verifier` roles before anything is spent (R6), builds
the agent, drives it while echoing todo-list progress (R10), and writes the report. The report
path is the final line of stdout — frozen, because R1 depends on it. Nothing may print after it.

The agent may ask clarifying questions via `ask_user` before researching (R2, D5): the run
interrupts, `main` prints each question, reads an answer, and resumes the same thread with
it as the tool's result.

Two ceilings bound the run (R7): a round cap counted by the stream loop itself in MODEL
TURNS (`AgentSettings.max_rounds` — see `_note_model_turns` inside `main`; `recursion_limit`
survives only as a runaway backstop), and a wall clock (`AgentSettings.wall_clock_seconds`)
armed at the first top-level `task(subagent_type="researcher")` dispatch (Step 3 Drift C — the
lead's own `search_web`/`fetch_pages` calls moved onto the nested researcher/reader tiers,
which this top-level stream never sees) and running continuously after that, including through
a later clarification wait. A run that lands on the round cap mid-research gets one bounded
synthesis pass to write a final answer from what it already read (`_SYNTHESIZE_NOW`) before the
report is written; hitting either bound, or any other mid-run failure, still writes a report
disclosing what happened (`RunOutcome.cut_short`).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import tomllib
from collections.abc import Callable, Iterable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata, add_usage
from rich.console import Console, RenderableType

from harness.activity import ActivitySink, DisplayError, brief_summary
from harness.config import ConfigError, load_config
from harness.display import (
    Activity,
    Alert,
    AnswerDraft,
    Question,
    QuestionAnswered,
    ReaderItem,
    ReadersUpdated,
    Renderer,
    RoundsUpdated,
    RunFinished,
    StageTracker,
    TodoItem,
    TodosUpdated,
    ToolCall,
    WelcomeScreen,
    WelcomeView,
    build_help_panel,
    build_model_picker,
    build_renderer,
)
from harness.input import KeyEvent, LineBuffer, read_keys, restore_terminal, scoped_keys
from harness.paragraphs import split_paragraphs
from harness.report import CutShortReason, RunOutcome, partition_sources, write_report
from harness.runlog import RunLog
from harness.sources import SourceRegistry, extract_urls
from harness.tools.search import SearchPreflightError, preflight_search
from harness.verify import VerificationResult, verify_paragraphs

if TYPE_CHECKING:
    # Annotation-only: the runtime imports of langgraph and the agent/model stack are
    # deferred into `main` — they cost several seconds, and `--help` or a config error
    # should not pay them (see the deferred-import block there).
    from collections.abc import AsyncGenerator

    from langchain_core.runnables import RunnableConfig
    from langgraph.types import Interrupt

    from harness.config import HarnessConfig

_EMPTY_USAGE: UsageMetadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

# What the model is told when the developer answers a clarifying question with nothing.
_NO_ANSWER_GIVEN = "(The developer gave no answer to this question.)"

# R2's pre-research window / Step 3 Drift C: the wall clock arms the first time the lead
# proposes a `task` dispatch to this subagent type — that IS "research started" in the 3-tier
# design (the nested `search_web`/`fetch_pages` calls inside a researcher's own subgraph never
# reach this top-level stream at all).
_RESEARCHER_SUBAGENT_TYPE = "researcher"

# What the lead is told when the round cap lands mid-research (R7): one bounded pass to turn
# what was already read into a final answer, instead of dying by exception mid-tool-call and
# leaving `## Answer` to whatever prose happened to come last.
_SYNTHESIZE_NOW = (
    "The research round cap has been reached. Stop researching now: do not call any more "
    "tools. Using only the sources you have already read, write your complete final answer, "
    "citing each claim with its [Sn] marker, and note explicitly which planned work the cap "
    "cut off."
)

# Supersteps allowed for the synthesis pass: room for a couple of model turns plus the
# per-turn middleware overhead, so a lead that keeps calling tools despite `_SYNTHESIZE_NOW`
# is stopped quickly by `GraphRecursionError` (reported as the same `round_cap`).
_SYNTHESIS_RECURSION_LIMIT = 10

# Roles preflighted before any agent work, in the order they are checked. Adding a role to
# the config means adding it here — nothing else in `main` knows the list. All four roles:
# they share one provider, but a per-MODEL failure (retired ID, quota, the reader tier's
# region opt-in — see docs/decisions.md) passes the head's check and would otherwise surface
# only mid-run, after real budget is spent (PR #18 review).
_PREFLIGHT_ROLES = ("head", "researcher", "reader", "verifier")

# The runaway backstop's sizing, named alongside `_SYNTHESIS_RECURSION_LIMIT` rather than left
# inline: both are recursion-limit safety margins and a tuning pass should find them together.
_BACKSTOP_SUPERSTEPS_PER_ROUND = 20
_BACKSTOP_FLOOR = 100


def _sum_usage(messages: list[BaseMessage]) -> UsageMetadata:
    """Sum `usage_metadata` across every `AIMessage` in the final state."""
    usages = [
        message.usage_metadata
        for message in messages
        if isinstance(message, AIMessage) and message.usage_metadata
    ]
    total = reduce(add_usage, usages, None)
    return total if total is not None else _EMPTY_USAGE


def _message_text(message: AIMessage) -> str:
    """The prose in one `AIMessage`, whichever content shape the provider used.

    `content` is `str | list[str | dict]`, and `str(content)` on the list shape renders a raw
    Python repr — `[{'type': 'text', 'text': '...'}]` — which would land verbatim under
    `## Answer`. The configured models return the string shape today, so this guards a
    provider or model swap rather than an observed bug.
    """
    content = message.content
    if isinstance(content, str):
        return content.strip()

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def _final_answer(messages: list[BaseMessage]) -> str:
    """The last `AIMessage` carrying real prose, or `""` if the run never produced one.

    NOT `messages[-1].content`: on a cut-short run the last message is usually a
    `ToolMessage` or a content-less tool-call `AIMessage`, which would put raw tool output
    ("Updated todo list to [...]") under `## Answer`. `report.py` renders the empty case.
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = _message_text(message)
            if content:
                return content
    return ""


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


def _sources_read(registry: SourceRegistry) -> int:
    """How many sources have actually been READ so far — the ledger's per-task meta count (R2).

    `read_mode != "unread"` rather than `len(registry.all())`: a URL is registered the moment
    it is seen in search results, so the raw count would climb far ahead of anything actually
    fetched and read.
    """
    return sum(1 for source in registry.all() if source.read_mode != "unread")


def _todo_items(
    todos: list[dict[str, Any]], registry: SourceRegistry, sink: ActivitySink
) -> tuple[TodoItem, ...]:
    """Build the `TodosUpdated` snapshot from the graph's raw todo list (Phase 6).

    Only the ACTIVE (`in_progress`) row carries meta: the mockup shows it beside the task in
    flight, and repeating one run-level total on every row would read as a per-task number it
    is not. Prefers `"{n} in flight"` from `sink.live_reader_count()` (the mockup's variant)
    over the older `"{n} sources"` count, since a reader dispatch in progress is more
    immediately actionable than a running total of what has been read so far; falls back to
    the sources count when no reader is currently live, and to `None` when neither applies.
    """
    sources_read = _sources_read(registry)
    live_readers = sink.live_reader_count()

    def _meta(status: str) -> str | None:
        if status != "in_progress":
            return None
        if live_readers:
            return f"{live_readers} in flight"
        if sources_read:
            return f"{sources_read} sources"
        return None

    return tuple(
        TodoItem(content=todo["content"], status=todo["status"], meta=_meta(todo["status"]))
        for todo in todos
    )


def _research_tool_calls(node_update: dict[str, Any]) -> list[dict[str, Any]]:
    """The top-level `task(subagent_type="researcher")` calls proposed in one node update.

    Also drives the wall clock, which arms exactly once, at the first such call seen in the
    stream (Step 3 Drift C — see `_RESEARCHER_SUBAGENT_TYPE`).
    """
    calls: list[dict[str, Any]] = []
    for message in node_update.get("messages") or []:
        if isinstance(message, AIMessage):
            calls.extend(
                dict(call)
                for call in message.tool_calls
                if call["name"] == "task"
                and (call.get("args") or {}).get("subagent_type") == _RESEARCHER_SUBAGENT_TYPE
            )
    return calls


def _describe_tool_call(call: dict[str, Any]) -> str:
    """One activity line describing a researcher-dispatch proposal.

    `brief_summary`, not the raw description: this renders as a wrapping `Activity` line with
    no render-time truncation, so the model's full delegation prompt painted a paragraph-sized
    blob that pushed the frame past the terminal height (PR #25 review).
    """
    args = call.get("args") or {}
    return f'task(researcher): "{brief_summary(str(args.get("description", "")))}"'


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
    from langgraph.errors import GraphRecursionError
    from langgraph.types import Command

    from harness.agent import build_agent
    from harness.models import ModelError, preflight

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

    # Live disclosure (best-effort + disclose): every incident a tool records is echoed to
    # the terminal as soon as the stream yields control back, and `alerts_emitted` keeps a
    # later poll from re-printing what an earlier one already showed.
    alerts_emitted = 0

    def _emit_new_alerts() -> None:
        nonlocal alerts_emitted
        incidents = run_log.incidents()
        for incident in incidents[alerts_emitted:]:
            renderer.emit(Alert(incident.detail))
        alerts_emitted = len(incidents)

    # Fix-pass item 1: `ActivitySink` PUSHES via `on_change` rather than being drained from the
    # stream loop -- the middleware writes from inside the lead's `task` tool NODE, and one node
    # is one superstep, so no top-level `astream` chunk arrives until the whole
    # researcher->reader pipeline has finished (measured: `live_reader_count() == 0` at every
    # chunk of a run whose reader genuinely ran). Hoisted here, before `_on_activity_change` and
    # the sink itself exist, so the callback's `nonlocal`s resolve to real values from its very
    # first invocation, which can happen from deep inside `build_agent(...)`'s first dispatch.
    tool_calls_emitted = 0
    # `last_readers` is the dedupe: without it every mutation would repaint an unchanged strip
    # (`ReadersUpdated` is a replacement snapshot, not a delta — Contracts).
    last_readers: tuple[Any, ...] | None = None
    last_todos: list[dict[str, Any]] | None = None
    # The live-reader-count half of the todo meta's freshness (Phase 6): the todo LIST dedupe
    # (`last_todos`, refreshed from the stream loop below) stays as-is, but the active row's
    # meta must also refresh when a reader dispatch starts or finishes even though the list
    # itself did not change shape.
    last_in_flight: int | None = None

    def _emit_new_tool_calls() -> None:
        nonlocal tool_calls_emitted
        records = sink.records()
        for record in records[tool_calls_emitted:]:
            renderer.emit(
                ToolCall(
                    call_id=record.call_id,
                    tool=record.tool,
                    arg_summary=record.arg_summary,
                    result_summary=record.result_summary,
                    elapsed_seconds=record.elapsed_seconds,
                    retry=record.retry,
                )
            )
        tool_calls_emitted = len(records)

    def _emit_readers() -> None:
        nonlocal last_readers
        readers = sink.readers()
        if readers == last_readers:
            return
        renderer.emit(
            ReadersUpdated(
                tuple(
                    ReaderItem(
                        id=reader.id,
                        brief=reader.brief,
                        status_text=reader.status_text,
                        done=reader.done,
                    )
                    for reader in readers
                )
            )
        )
        last_readers = readers

    def _on_activity_change() -> None:
        """Pushed by the `ActivitySink` the instant it changes (fix-pass item 1) -- the single
        path from a tool-activity mutation to the renderer; the drain calls this replaced are
        gone from the stream loop below.

        Nothing is swallowed: a failure to build or emit an event is a real bug. But this runs
        inside `awrap_tool_call`, so a bare exception on a `task` dispatch would be absorbed by
        that tier's retry/error guard and reported as `"READER FAILED"` after re-running the
        whole subagent. Re-raised as `DisplayError`, which `harness/agent.py` excludes from that
        guard, so a display bug fails the run AS a display bug.
        """
        nonlocal last_in_flight
        try:
            _push()
        except DisplayError:
            raise
        except Exception as exc:
            raise DisplayError(f"the display failed while rendering tool activity: {exc}") from exc

    def _push() -> None:
        """`_on_activity_change`'s body, split out so the wrapper above has one `try` to guard."""
        nonlocal last_in_flight
        _emit_new_tool_calls()
        _emit_readers()
        # The todo LIST dedupe (`last_todos`) stays untouched -- this only refreshes the ACTIVE
        # row's meta when the live-reader count moved since the last emit, so a reader
        # starting/finishing mid-dispatch is reflected without re-emitting on every mutation.
        if last_todos is not None:
            in_flight = sink.live_reader_count()
            if in_flight != last_in_flight:
                renderer.emit(TodosUpdated(_todo_items(last_todos, registry, sink)))
                last_in_flight = in_flight

    sink = ActivitySink(on_change=_on_activity_change)
    # Same close/print/exit-1 shape as the preflight loop: `build_agent` resolves every
    # role through `build_chat_model`, so a missing or TODO role raises `ModelError` here —
    # unhandled it would escape as a traceback under the alternate screen (PR #18 review).
    try:
        agent = build_agent(config, registry, run_log, sink)
    except ModelError as exc:
        renderer.close()
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # One stable thread for the whole run: the checkpointer requires an id, and the
    # interrupt/resume loop below must keep resuming the SAME thread.
    thread_id = str(uuid4())
    run_config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        # A runaway BACKSTOP, never the round cap. The cap is counted in the stream loop below
        # (`_note_model_turns`) in the unit it actually means — model turns — because
        # supersteps-per-round is a topology detail owned by the installed deepagents/langchain
        # versions: middleware `after_model` nodes sit INSIDE the loop (4 supersteps per round
        # today), so a `max_rounds`-derived recursion_limit silently halved the advertised
        # budget and drifts again on any upgrade. Sized ~5x anything the counted cap could
        # legitimately need, so it only trips if the counting fails or the graph loops without
        # producing model turns.
        "recursion_limit": (
            config.agent.max_rounds * _BACKSTOP_SUPERSTEPS_PER_ROUND + _BACKSTOP_FLOOR
        ),
    }
    stream_input: Any = {"messages": [HumanMessage(content=question)]}

    final_state: dict[str, Any] | None = None
    clock_armed = False
    cut_short: CutShortReason | None = None
    cut_short_detail: str | None = None

    # R7's round accounting, run-level (clarification resumes no longer refresh it).
    max_rounds = config.agent.max_rounds
    rounds_used = 0
    counted_turn_ids: set[str] = set()
    awaiting_tool_ids: set[str] = set()
    cap_hit = False  # round `max_rounds` ended proposing tools: a synthesis pass is owed
    overrun = False  # a turn past the cap arrived anyway: stop with what already exists

    def _note_model_turns(node_update: dict[str, Any]) -> None:
        """Advance the round count for each model turn in one node update (R7).

        Counted here, in code this module owns, never derived from `recursion_limit`:
        supersteps-per-round is a graph-topology detail of the installed framework versions,
        so any user-facing budget derived from it drifts silently on upgrade. Deduplicated by
        message id because middleware nodes may re-emit an already-counted message; the reader
        subagent's internal turns never reach this stream, so rounds are the LEAD's turns.
        """
        nonlocal rounds_used, cap_hit, overrun
        for message in node_update.get("messages") or []:
            if isinstance(message, ToolMessage):
                awaiting_tool_ids.discard(message.tool_call_id)
                continue
            if not isinstance(message, AIMessage):
                continue
            if message.id is not None:
                if message.id in counted_turn_ids:
                    continue
                counted_turn_ids.add(message.id)
            rounds_used += 1
            renderer.emit(RoundsUpdated(rounds_used, max_rounds))
            if rounds_used > max_rounds:
                overrun = True
            elif rounds_used == max_rounds:
                # The turn AT the cap may already be the tool-free final answer — only a turn
                # proposing more tool work owes a synthesis pass, and only after those tools
                # finish, so the thread never ends on dangling tool calls.
                call_ids = {
                    call_id
                    for call in message.tool_calls
                    if isinstance(call_id := call.get("id"), str)
                }
                if call_ids:
                    awaiting_tool_ids.update(call_ids)
                    cap_hit = True

    try:
        # `asyncio.timeout(None)` starts disarmed, so a clarifying wait before the first
        # research call is never bounded. This shape rather than `asyncio.wait_for` because
        # the deadline is unknown until mid-stream and must span both the `astream` iteration
        # and the `_read_answer` await inside the same loop.
        async with asyncio.timeout(None) as clock:
            while True:
                # Scoped to THIS pass: interrupt detection below must never read a previous
                # pass's `__interrupt__`, or a resumed pass emitting no `values` chunk would
                # re-ask the same question forever. `final_state` still holds the newest
                # state actually seen, so the report is assembled from real data.
                pass_state: dict[str, Any] | None = None
                # `aclosing`, because the round cap leaves this loop by `break`: a bare break
                # abandons the generator to garbage collection with langgraph tasks in flight.
                async with aclosing(
                    # `cast`: `astream` is typed as a bare AsyncIterator, but it is an async
                    # generator at runtime, which is what `aclosing` needs.
                    cast(
                        "AsyncGenerator[Any, None]",
                        agent.astream(
                            stream_input, config=run_config, stream_mode=["updates", "values"]
                        ),
                    )
                ) as stream:
                    async for mode, chunk in stream:
                        if mode == "updates":
                            for node_update in chunk.values():
                                # An interrupt arrives as `{"__interrupt__": (Interrupt(...),)}`,
                                # whose value is a tuple, not a dict — `.get` raises on it.
                                if not node_update or not isinstance(node_update, dict):
                                    continue
                                todos = node_update.get("todos")
                                if todos is not None and todos != last_todos:
                                    renderer.emit(TodosUpdated(_todo_items(todos, registry, sink)))
                                    last_todos = todos
                                    last_in_flight = sink.live_reader_count()
                                calls = _research_tool_calls(node_update)
                                if calls:
                                    tracker.advance("researching")
                                    for call in calls:
                                        renderer.emit(Activity(_describe_tool_call(call)))
                                    if not clock_armed:
                                        clock.reschedule(
                                            asyncio.get_running_loop().time()
                                            + config.agent.wall_clock_seconds
                                        )
                                        clock_armed = True
                                _note_model_turns(node_update)
                            # Tool-call/reader-strip/todo-meta refreshes are no longer polled
                            # here (fix-pass item 1): `_on_activity_change` pushes them the
                            # instant the sink changes, from inside the tool dispatch itself.
                            _emit_new_alerts()
                            if overrun or (cap_hit and not awaiting_tool_ids):
                                break
                        else:  # mode == "values"
                            pass_state = chunk
                            # Assigned HERE, inside the iteration: every cut-short path leaves
                            # this loop by exception, so an assignment after the `async for`
                            # never runs and the report would lose both the answer and the
                            # token usage on exactly the runs that need disclosing.
                            final_state = chunk

                # Interrupts first, BEFORE the cap: `ask_user` counts as a tool call on the
                # capped round and so sets `cap_hit`, but it pauses the graph instead of
                # returning a `ToolMessage`, so `awaiting_tool_ids` never drains. Handling the
                # cap here dropped the question and then resumed a paused thread — the run
                # died and wrote nothing. The cap still applies once the answer is delivered.
                interrupts = (pass_state or {}).get("__interrupt__")
                if interrupts:
                    tracker.advance("clarifying")
                    # `interrupts[0]`, not all of them: the lead is a single agent node, so at
                    # most one is ever pending, and `Command(resume=...)` delivers ONE value —
                    # fanning several into one decisions list would mis-pair them.
                    stream_input = Command(
                        resume={
                            "decisions": await _answer_questions(
                                interrupts[0], renderer, registry, tracker
                            )
                        }
                    )
                    continue

                if cap_hit or overrun:
                    cut_short = "round_cap"
                    # `overrun` means a turn PAST the cap already started new work, so its tool
                    # calls may be dangling — appending a synthesis request there would hand
                    # the model an invalid sequence. Otherwise the capped round's tools have
                    # all answered (`awaiting_tool_ids` drained), and one bounded pass turns
                    # what was read into a real final answer instead of mid-run chatter.
                    if not overrun:
                        renderer.emit(
                            Activity(f"round cap ({max_rounds}) reached — asking for a synthesis")
                        )
                        synthesis_config: RunnableConfig = {
                            **run_config,
                            "recursion_limit": _SYNTHESIS_RECURSION_LIMIT,
                        }
                        async with aclosing(
                            cast(
                                "AsyncGenerator[Any, None]",
                                agent.astream(
                                    {"messages": [HumanMessage(content=_SYNTHESIZE_NOW)]},
                                    config=synthesis_config,
                                    stream_mode=["updates", "values"],
                                ),
                            )
                        ) as synthesis:
                            async for mode, chunk in synthesis:
                                if mode == "values":
                                    final_state = chunk
                                else:
                                    _emit_new_alerts()
                break
    except TimeoutError as exc:
        # `clock.expired()`, not a bare `except TimeoutError`: a timeout raised INSIDE the
        # run (an `asyncio.wait_for` in a tool, say) would otherwise be reported as "the wall
        # clock stopped this run", which is untrue and hides the real failure.
        if clock.expired():
            cut_short = "wall_clock"
        else:
            cut_short = "error"
            cut_short_detail = f"{type(exc).__name__}: {exc}"
    except GraphRecursionError:  # must precede `Exception` — it subclasses RuntimeError
        # Two sources, one meaning: the synthesis pass's small limit (a lead that kept calling
        # tools despite `_SYNTHESIZE_NOW`) or the runaway backstop on `run_config`. Either way
        # the run ended on a rounds-related bound.
        cut_short = "round_cap"
    except Exception as exc:  # noqa: BLE001 — never `BaseException`; KeyboardInterrupt has its own clause below
        cut_short = "error"
        cut_short_detail = f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:
        # D2: a user abort maps onto the existing hard-error path (no new outcome kind) — its
        # own clause because `KeyboardInterrupt` is a `BaseException`, not caught by `Exception`.
        cut_short = "error"
        cut_short_detail = "user abort (Ctrl+C)"

    # One last poll: the final tool executions (or a cut-short pass) may have recorded
    # incidents after the last updates chunk was handled.
    _emit_new_alerts()

    messages: list[BaseMessage] = final_state["messages"] if final_state else []
    usage = _sum_usage(messages)

    answer = _final_answer(messages)
    # Split exactly once (D2): `verify_paragraphs` and `report.py`'s `## Answer` renderer
    # share this one list; nothing ever re-splits `answer`.
    paragraphs = split_paragraphs(answer)
    verification = None
    if cut_short == "error":
        # The head model is near-certainly still unreachable, so one call per paragraph — each
        # with its own bounded backoff — would burn minutes before the report is even written.
        # The skip is disclosed via `check_failures`, never silent.
        verification = VerificationResult(
            check_failures=[
                "verification skipped: the run ended in an error, so claims were not checked"
            ]
        )
    elif answer:
        tracker.advance("verifying")
        renderer.emit(
            Activity(f"checking {len(paragraphs)} paragraph(s) against their cited sources")
        )
        try:
            verification = await verify_paragraphs(
                paragraphs,
                config,
                registry,
                # Per-paragraph progress: each pooled check is one model call that can take
                # minutes, so without this the verifying stage shows nothing until it ends.
                on_paragraph=lambda i, n: renderer.emit(Activity(f"checking paragraph {i}/{n}")),
            )
        except Exception as exc:  # noqa: BLE001
            # Best-effort + disclose: a pass that fails wholesale is reported IN the report.
            # Per-paragraph failures are handled inside the pass itself.
            verification = VerificationResult(
                check_failures=[f"verification pass failed: {type(exc).__name__}: {exc}"]
            )

    outcome = RunOutcome(
        question=question,
        answer=answer,
        registry=registry,
        usage=usage,
        cut_short=cut_short,
        cut_short_detail=cut_short_detail,
        todos=last_todos or [],
        started_at=started_at,
        paragraphs=paragraphs,
        verification=verification,
        incidents=run_log.incidents(),
    )
    # D2's gate: a hard error, a user abort (mapped onto `cut_short == "error"` above), and a
    # wall-clock expiry with no final answer all write NO report — stderr error, exit 1. Round
    # cap and any wall-clock expiry that already has an answer keep the disclosed report.
    has_answer = bool(answer.strip())
    should_write_report = (
        cut_short is None or cut_short == "round_cap" or (cut_short == "wall_clock" and has_answer)
    )

    tracker.advance("writing")
    # `finally`, because the live region owns terminal state: `Live.start` hides the cursor and
    # rich registers no atexit restore, so a `write_report` OSError (unwritable or full reports
    # dir) escaping here would leave the developer's shell with no cursor after the traceback.
    path: Path | None = None
    try:
        if should_write_report:
            path = write_report(outcome, config)
        tracker.finish()
        usable, unusable = partition_sources(config, registry)
        renderer.emit(
            RunFinished(
                stage_timings=tracker.timings(),
                usable_sources=len(usable),
                unusable_sources=len(unusable),
                cut_short=cut_short,
                verification_failures=len(verification.check_failures) if verification else 0,
                incidents=len(run_log.incidents()),
                report_path=path,
            )
        )
    finally:
        renderer.close()
    # Error prints belong AFTER close(): under `Live(screen=True)` anything written to the
    # terminal — stderr included, it shares the device — while the Live runs lands on the
    # alternate screen and is discarded with it. Down here the normal buffer is restored, so
    # the detail survives the run (Phase 4's "the no-report error message becomes visible").
    if path is None:
        if cut_short == "wall_clock":
            # The wall clock fired before a final answer existed (risk #2's blank/whitespace-answer
            # case lands here too, via `has_answer`).
            print("error: the wall clock expired before a final answer existed", file=sys.stderr)
        else:
            print(f"error: {cut_short_detail}", file=sys.stderr)
    return 0 if should_write_report else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
