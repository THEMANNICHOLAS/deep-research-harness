"""The research session: the lead's turn loop, its background researchers, and the run's exits.

One `Session` owns everything between "the question is known" and "a report exists or the run
failed" (D2). `harness/__main__.py` keeps only the CLI, the welcome screen, the preflights and
the process exit code.

The loop is event-driven rather than one long `astream` (D1/D2). The lead starts researchers
with `dispatch_researcher`, which returns at once; each researcher runs as an `asyncio.Task`
over `build_researcher_graph`'s compiled graph and, when it finishes, puts a `ResearcherReturn`
on `Session.events`. The loop awaits at least one event, drains whatever else is pending, folds
the batch into ONE `HumanMessage` (each return's findings verbatim, closed by a `Roster:` line)
and runs one lead turn on the same thread. Research ends when the lead calls `submit_report`
(D3) — the report's answer is that tool's ARGUMENT, never whatever prose came last, so a
narration turn can never be mistaken for a final answer.

Two ceilings still bound the run (R7), moved here unchanged in substance: a round cap counted
in MODEL TURNS by `_note_model_turns` (`recursion_limit` survives only as a runaway backstop),
and a wall clock that spans the RESEARCH alone (R6) — armed at the first `dispatch_researcher`
the harness accepts, disarmed the moment `submit_report` is, so no time spent after the report
can cut the run short. Crossing either, or the synthesis reserve measured back from the clock,
buys one bounded pass in which the lead is told to call `submit_report` from what it has.

The lead may also `ask_user` at any point (R4). That interrupt is answered inside the turn by
`_collect_answers`, which renders the question and its numbered choices, pauses the displayed
stage clock, and resumes the same thread with the answer — `__main__` supplies only the
`answer_source` the text is read from.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata, add_usage
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.types import Command, Interrupt

from harness import activity
from harness.activity import ActivitySink, DisplayError, brief_summary, format_status_elapsed
from harness.agent import (
    _retry_on_non_search_abort,
    _summarize_tool_result,
    build_agent,
    build_researcher_graph,
    subagent_failure_text,
)
from harness.config import HarnessConfig
from harness.display import (
    Activity,
    AgentText,
    Alert,
    CommandReply,
    LeadToolCall,
    Question,
    QuestionAnswered,
    Renderer,
    ReportWritten,
    ResearcherItem,
    ResearchersUpdated,
    RoundsUpdated,
    RunFinished,
    RunStarted,
    SourcesUpdated,
    StageTracker,
    TodoItem,
    TodosUpdated,
    ToolCall,
    UserTurn,
)
from harness.paragraphs import split_paragraphs
from harness.report import CutShortReason, RunOutcome, partition_sources, write_report
from harness.runlog import RunLog
from harness.sources import SourceRegistry, extract_urls
from harness.tools.dispatch import DISPATCH_RESEARCHER_TOOL_NAME
from harness.verify import VerificationResult, verify_paragraphs

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from harness.browser import BrowserSession

_EMPTY_USAGE: UsageMetadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

# The session crumb is the opening question's first five words (Preferences) — long enough to
# tell two runs apart, short enough to leave the source counter its half of the session bar.
_CRUMB_WORDS = 5

# What the model is told when the developer answers a clarifying question with nothing.
_NO_ANSWER_GIVEN = "(The developer gave no answer to this question.)"

# What the lead is told when either bound (the round cap or the synthesis margin) lands
# mid-research (R7): one bounded pass to turn what was already reported into a final answer,
# instead of dying by exception mid-turn. Shared body so the two reasons' instructions never
# diverge in substance, only in which bound they name (G3, 3F review). It must name
# `submit_report` explicitly (D3): the report is written from that tool's argument alone, so an
# instruction to merely "write your final answer" would end the run with nothing to write.
_SYNTHESIZE_NOW_INSTRUCTION = (
    "Stop researching now: do not dispatch any more researchers. Using only what the "
    "researchers have already reported, call `submit_report` with your complete final answer, "
    "citing each claim with its [Sn] marker, and note explicitly which planned work "
    "{cut_off} cut off. Prose outside `submit_report` is not the report."
)
_SYNTHESIZE_NOW = "The research round cap has been reached. " + _SYNTHESIZE_NOW_INSTRUCTION.format(
    cut_off="the cap"
)
_SYNTHESIZE_NOW_MARGIN = (
    "The synthesis reserve has been reached. "
    + _SYNTHESIZE_NOW_INSTRUCTION.format(cut_off="the reserve")
)

# The one nudge a lead gets when it ends a turn with an empty roster, an empty event queue and
# no report (D3): without `submit_report` there is nothing to write, and a lead that simply
# narrated an answer into chat would otherwise fail the run for a mechanical reason. Exactly
# once — a second idle turn ends the run rather than looping forever.
_SUBMIT_NOW = (
    "No researcher is running and nothing else is pending. If your research is complete, call "
    "`submit_report` now with your complete final answer — that tool call IS the report, and "
    "nothing is written without it. If it is not complete, dispatch the researcher you need."
)

# Supersteps allowed for the synthesis pass: room for a couple of model turns plus the
# per-turn middleware overhead, so a lead that keeps calling tools despite `_SYNTHESIZE_NOW`
# is stopped quickly by `GraphRecursionError` (reported as the same `round_cap`).
_SYNTHESIS_RECURSION_LIMIT = 10

# The runaway backstop's sizing, named alongside `_SYNTHESIS_RECURSION_LIMIT` rather than left
# inline: both are recursion-limit safety margins and a tuning pass should find them together.
_BACKSTOP_SUPERSTEPS_PER_ROUND = 20
_BACKSTOP_FLOOR = 100


@dataclass(frozen=True)
class ResearcherReturn:
    """One researcher's findings, delivered to the lead as its own turn (R2)."""

    id: str
    label: str
    findings: str
    elapsed_s: float


@dataclass(frozen=True)
class UserMessage:
    """Text the developer typed, queued for the lead's next turn (R1, Phase 3 fills it)."""

    text: str


@dataclass(frozen=True)
class ModelSwitch:
    """A validated `/model <role> <choice>` command, queued for the NEXT turn boundary (D4).

    Like `_Quit`, this never reaches `_batch_message` — `_next_batch` applies it (rebuilding
    whatever graph the switched role belongs to) and strips it before the batch is handed to
    `_next_input`, so the lead is never told "a ModelSwitch happened" as text.
    """

    role: str
    choice: str


class _Quit:
    """The queue's quit sentinel: "the developer asked to stop", posted by `request_quit`.

    A sentinel rather than a public event type (Phase 3): quitting is not something the LEAD
    is ever told about — every batch builder strips it — so giving it a place in `SessionEvent`
    would put it in reach of `_batch_message` and, one edit later, in the model's context.
    """


_QUIT = _Quit()

# What can end up as TEXT in the lead's next turn (R2) -- `ModelSwitch` is deliberately not a
# member: it is applied and stripped inside `_next_batch` before a batch is ever handed to
# `_batch_message`/`_next_input`, so those stay typed against exactly what they may read.
SessionEvent = ResearcherReturn | UserMessage

# Everything that can sit on the raw event queue (Phase 6): `SessionEvent` plus the two things
# that never reach `_batch_message` -- `ModelSwitch`, applied and stripped in `_next_batch`,
# and `_Quit`, filtered there too.
_QueueEvent = SessionEvent | ModelSwitch

# The three slash commands the session loop handles itself (Phase 6, D4/D6) -- never sent to
# the model. A data table, not an if/elif chain, so the unknown-command and bare-`/` replies
# (both list every command) can never drift out of step with what `_handle_command` accepts.
_SESSION_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/sources", "List every source captured so far."),
    ("/model <role> <choice>", "Switch a role's model at the next turn boundary."),
    ("/new", "End this session and return to the welcome screen."),
)


def _pending_tool_call_ids(message: AIMessage) -> set[str]:
    """The string `tool_call` ids `message` proposes — the work a cut must wait out.

    One home for both cut-short bounds: the round cap (`_note_model_turns`) and R7's synthesis
    margin each defer their break until the crossing turn's own tool calls have answered, or
    LangGraph auto-heals the dangling entries with synthesized "cancelled" `ToolMessage`s and
    the in-flight research silently vanishes.
    """
    return {call_id for call in message.tool_calls if isinstance(call_id := call.get("id"), str)}


def _margin_reached(elapsed: float, wall_clock_seconds: int, margin_seconds: int) -> bool:
    """Whether elapsed research time has crossed R7's synthesis reserve.

    Extracted from the turn loop for ONE reason: testability. The loop's own margin check is
    only reachable through a full run, and at the `margin_seconds == 0` boundary `asyncio`'s
    timeout cancellation always wins the race against app-level code, so a full-run test
    resolves to a wall-clock cut whether or not the disable guard is present — it cannot tell
    a correct implementation from one missing the guard. As pure arithmetic the boundary is
    directly assertable instead.

    `margin_seconds <= 0` means DISABLED, and must never be read as "a threshold equal to the
    wall clock": that would fire the reserve at the same instant the hard clock expires, racing
    it for the same run.
    """
    if margin_seconds <= 0:
        return False
    return elapsed >= wall_clock_seconds - margin_seconds


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
    Python repr — `[{'type': 'text', 'text': '...'}]` — which would land verbatim in a
    researcher's findings. The configured models return the string shape today, so this guards
    a provider or model swap rather than an observed bug.
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
    """The last `AIMessage` carrying real prose, or `""` if the graph never produced one.

    This is deepagents' own `task` result contract, reproduced here because `Session` now runs
    the researcher graph itself: `deepagents/middleware/subagents.py` walks back to the last
    non-empty `AIMessage` text for exactly the same reason — a trailing content-less tool-call
    message would otherwise be forwarded as empty findings.
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = _message_text(message)
            if content:
                return content
    return ""


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


def _dispatch_tool_calls(node_update: dict[str, Any]) -> list[dict[str, Any]]:
    """The `dispatch_researcher` calls proposed in one node update.

    The replacement for the old `task(subagent_type="researcher")` scan: the lead has no `task`
    tool at all now, so a dispatch is identified by tool NAME alone and `subagent_type` appears
    nowhere above the researcher tier.
    """
    calls: list[dict[str, Any]] = []
    for message in node_update.get("messages") or []:
        if isinstance(message, AIMessage):
            calls.extend(
                dict(call)
                for call in message.tool_calls
                if call["name"] == DISPATCH_RESEARCHER_TOOL_NAME
            )
    return calls


def _describe_tool_call(call: dict[str, Any]) -> str:
    """The ARG summary of a researcher-dispatch proposal — the transcript line's parentheses.

    The label, not the objective: the label is authored to be a 2-5 word roster entry, whereas
    the objective is the model's full delegation brief and painted a paragraph-sized blob that
    pushed the frame past the terminal height (PR #25 review). `brief_summary` still bounds it,
    since the label is model-supplied and nothing enforces its length.

    The tool NAME is no longer prefixed here (Phase 5): `LeadToolCall` carries it as its own
    field, and repeating it would render as `dispatch_researcher(dispatch_researcher: ...)`.
    """
    args = call.get("args") or {}
    return brief_summary(str(args.get("label", "")))


def _resolve_answer(answer: str, choices: tuple[str, ...]) -> str:
    """One typed line -> what the model is told, applying R4's digit rule.

    A lone digit numbering an OFFERED choice resolves to that choice's text; anything else is
    free text verbatim — "5" against three choices, "12", "2x". The choices are an offer, not
    a menu lock, and an out-of-range number is never clamped onto a choice the developer did
    not pick. Best-effort + disclose: a bare Enter must not reach the model as an empty tool
    result, which reads as "answered with nothing said" and hides the open ambiguity.
    """
    text = answer.strip()
    if not text:
        return _NO_ANSWER_GIVEN
    # Membership in the rendered labels, not `int(text)`: `"²".isdigit()` is True while
    # `int("²")` raises, and a pasted answer is arbitrary text.
    numbers = [str(index) for index in range(1, len(choices) + 1)]
    if text in numbers:
        return choices[numbers.index(text)]
    return text


class Session:
    """One research session: the lead, its researchers, and the run's single report gate.

    Constructed by `__main__.main` from exactly what it holds once the preflights pass; `run()`
    compiles both graphs, drives the loop to a report or a failure, and returns the written
    `RunOutcome` or `None`. `main` owns the browser and renderer lifecycles and the exit code.
    """

    def __init__(
        self,
        config: HarnessConfig,
        registry: SourceRegistry,
        run_log: RunLog,
        renderer: Renderer,
        tracker: StageTracker,
        question: str,
        *,
        sink: ActivitySink | None,
        browser: "BrowserSession | None",
        answer_source: Callable[[], Awaitable[str]],
        started_at: datetime,
        interactive: bool = False,
    ) -> None:
        self._config = config
        self._registry = registry
        self._run_log = run_log
        self._renderer = renderer
        self._tracker = tracker
        self._question = question
        self._sink = activity.or_default(sink)
        self._browser = browser
        self._answer_source = answer_source
        self._started_at = started_at
        self._interactive = interactive

        self.events: asyncio.Queue[_QueueEvent | _Quit] = asyncio.Queue()
        self.running: dict[str, asyncio.Task[None]] = {}
        self.answer: str | None = None
        self.agent: Runnable
        # Set by `request_quit`, read by the turn loop and the post-report chat loop.
        self._quit = False
        # Set by `/new` (D6), read by `__main__` after `run()` returns: a restart is not a
        # failure, so `__main__` must not print the failed-run stderr line before looping back
        # to the welcome screen.
        self.restart_requested = False
        # `run()`'s own task, so a quit before the report can cancel the turn in flight rather
        # than waiting for it to end (R5: that quit is a FAILED run, and must be immediate).
        self._run_task: asyncio.Task[Any] | None = None
        # One stable thread for the whole session: the checkpointer requires an id, and every
        # turn — including an interrupt resume — must land on the SAME thread.
        self.thread_id = str(uuid4())

        self._graph: Runnable
        self._run_config: RunnableConfig = {}
        self._next_id = 0
        # A researcher failure that must END the run rather than be reported as findings (a
        # mid-run search abort, or a display bug): raised out of the loop at the next boundary.
        self._fatal: BaseException | None = None

        # Cut-short bookkeeping, read by `__main__` for the no-report stderr line.
        self.cut_short: CutShortReason | None = None
        self.cut_short_detail: str | None = None

        self._clock: asyncio.Timeout | None = None
        # EVER armed, not currently armed: this guards `_arm_clock`'s early return, so a clock
        # disarmed by `submit_report` can never be re-armed by a later dispatch. What is
        # ticking right now is the `clock_armed` property, read off the timeout itself.
        self._clock_ever_armed = False
        self._research_started_at: float | None = None

        # R7's round accounting, run-level (clarification resumes do not refresh it).
        self._rounds_used = 0
        self._counted_turn_ids: set[str] = set()
        self._awaiting_tool_ids: set[str] = set()
        self._cap_hit = False  # round `max_rounds` ended proposing tools: a synthesis is owed
        self._overrun = False  # a turn past the cap arrived anyway: stop with what exists
        self._margin_hit = False  # R7's reserve: the synthesis margin threshold was crossed
        self._forced_synthesis = False  # inside the bounded pass: `submit` stops refusing

        self._final_state: dict[str, Any] | None = None
        # `_finish` computes both; `_emit_finished` reads them, because the end-of-run summary
        # is now emitted after the post-report chat rather than inside `_finish`.
        self._verification: VerificationResult | None = None
        self._report_path: Path | None = None
        # Message ids whose prose already reached the transcript: middleware nodes re-emit an
        # already-seen `AIMessage`, and the same dedupe `_note_model_turns` needs for rounds is
        # what keeps one narration turn from being printed twice.
        self._agent_text_ids: set[str] = set()
        self._last_todos: list[dict[str, Any]] | None = None
        self._alerts_emitted = 0
        self._last_source_count = 0
        self._tool_calls_emitted = 0
        self._last_in_flight: int | None = None
        # The lead's in-flight tool calls, by `tool_call_id`: `(name, arg_summary)` held from
        # the proposal until the matching `ToolMessage` lets the transcript line be rewritten
        # with its result (Phase 5).
        self._pending_lead_calls: dict[str, tuple[str, str]] = {}

    def request_quit(self) -> None:
        """Ctrl+C / Ctrl+D from the composer: end the session at the next safe point (R5).

        Two different endings, decided by whether a report exists yet. BEFORE one does, the
        quit is a FAILED run and must be immediate, so the turn in flight is cancelled — the
        `CancelledError` lands in `run()`'s abort clause and does exactly what Ctrl+C has
        always done (no report, `cut_short = "error"`, exit 1). AFTER the report, the run has
        already succeeded: nothing is cancelled, the sentinel simply wakes the chat loop, which
        returns and lets `run()` hand back the outcome it already has (exit 0).
        """
        self._quit = True
        self.events.put_nowait(_QUIT)
        if self.answer is None and self._run_task is not None:
            self._run_task.cancel()

    def receive_user_message(self, text: str) -> None:
        """Queue one developer line for the lead's next turn (the composer's Enter, D5).

        The one production writer of `UserMessage`s: the composer calls this rather than
        reaching into `events`, which keeps the queue's element type — including the private
        `_Quit` sentinel — a session-internal concern, and spares `__main__` an import from
        this heavy module outside `main()`'s deferred block.

        A line whose STRIPPED text starts with `/` (Phase 6, Contracts) is a command, handled
        here in the session loop and never turned into a `UserMessage` — so it can never reach
        the model, whatever the command turns out to be.
        """
        stripped = text.strip()
        if stripped.startswith("/"):
            self._handle_command(stripped)
            return
        self.events.put_nowait(UserMessage(text))

    # --- slash commands (Phase 6, D4/D6) ----------------------------------------------------

    def _command_list_text(self) -> str:
        return "\n".join(f"{name} — {summary}" for name, summary in _SESSION_COMMANDS)

    def _handle_command(self, stripped: str) -> None:
        """Dispatch one already-slash-prefixed, already-stripped line (Contracts).

        Table-driven for the recognized three; a bare `/` (nothing after it) and any other
        `/x` both reply with the command list, differing only in whether an "unknown command"
        line precedes it — there is no command name to call unknown when nothing was typed
        after the slash.
        """
        name = stripped.split()[0]
        if name == "/sources":
            self._renderer.emit(CommandReply(self._sources_reply()))
        elif name == "/model":
            self._handle_model_command(stripped.split()[1:])
        elif name == "/new":
            self._handle_new_command()
        elif name == "/":
            self._renderer.emit(CommandReply(self._command_list_text()))
        else:
            self._renderer.emit(
                CommandReply(f"unknown command {name}\n{self._command_list_text()}")
            )

    def _sources_reply(self) -> str:
        """`/sources`' reply text -- one line per `SourceRegistry.all()` entry (Reuse: binding).

        Never touches the model: this is a pure read of the registry, rendered straight into a
        local `CommandReply`.
        """
        sources = self._registry.all()
        if not sources:
            return "none captured yet"
        return "\n".join(
            f"[{source.id}] {source.title or source.url} — {source.url} ({source.read_mode})"
            for source in sources
        )

    def _handle_model_command(self, args: list[str]) -> None:
        """Validate `/model <role> <choice>` and, if valid, queue a `ModelSwitch` (D4).

        Every rejection is an immediate `CommandReply` -- no rebuild, no event queued, no
        model traffic -- so a typo can never silently rebuild the wrong role's graph.
        """
        if len(args) != 2:
            self._renderer.emit(CommandReply("usage: /model <role> <choice>"))
            return
        role, choice = args
        role_config = self._config.roles.get(role)
        if role_config is None:
            self._renderer.emit(CommandReply(f"unknown role: {role}"))
            return
        if not role_config.choices:
            self._renderer.emit(
                CommandReply(f"role {role!r} has no configured model choices to switch between")
            )
            return
        if choice not in role_config.choices:
            self._renderer.emit(CommandReply(f"unknown choice {choice!r} for role {role!r}"))
            return
        self.events.put_nowait(ModelSwitch(role, choice))

    def _handle_new_command(self) -> None:
        """`/new` (D6): end this run and ask `__main__` to return to the welcome screen.

        Reuses `request_quit`'s own teardown (Reuse: binding) -- the running researchers are
        cancelled through the same `_cancel_running` an ordinary Ctrl+C uses, and `run()` exits
        exactly as a pre-report quit does. `restart_requested` is the one thing that tells
        `__main__` this was a developer-requested restart rather than a failure, so it must be
        set BEFORE the quit -- `request_quit` may cancel `run()`'s own task immediately.
        """
        self.restart_requested = True
        self.request_quit()

    async def _apply_model_switch(self, switch: ModelSwitch) -> None:
        """Apply one validated `ModelSwitch` at the turn boundary (D4).

        `config.roles[role].model` is session-config-only (never written to disk, Out of
        scope). Only `head` and the researcher tier's own two roles own a compiled graph this
        session holds -- `verifier` is resolved fresh from `self._config` at verification time
        (`harness.verify.verify_paragraphs`), so switching it needs no rebuild here at all.
        """
        self._config.roles[switch.role].model = switch.choice
        if switch.role == "head":
            # `aget_state`/`aupdate_state` are the compiled graph's own checkpointer methods,
            # not part of the generic `Runnable` interface `self.agent` is typed against
            # elsewhere in this module (matching `tests/test_session.py:259`'s existing
            # pattern of reaching through the same untyped seam).
            old_state = await self.agent.aget_state(self._run_config)  # type: ignore[attr-defined]
            old_messages = old_state.values["messages"]
            # The todos channel too (3F Minor a): carrying `messages` alone left the checklist
            # reset to empty on the new thread while the transcript above it still showed the
            # old plan. `.get(..., [])` because a switch before the lead ever wrote any todos
            # has no such channel in `old_state.values` yet.
            old_todos = old_state.values.get("todos", [])
            self.agent = build_agent(
                self._config, self._registry, self._run_log, self._sink, self._browser, self
            )
            self.thread_id = str(uuid4())
            self._run_config = {
                "configurable": {"thread_id": self.thread_id},
                "recursion_limit": _recursion_backstop(self._config),
            }
            await self.agent.aupdate_state(  # type: ignore[attr-defined]
                self._run_config, {"messages": old_messages}
            )
            if old_todos:
                await self._seed_todos(old_todos)
        elif switch.role in ("researcher", "reader"):
            self._graph = build_researcher_graph(
                self._config, self._registry, self._run_log, self._sink, self._browser
            )
        self._renderer.emit(CommandReply(f"switched {switch.role} to {switch.choice}"))

    async def _seed_todos(self, old_todos: list[dict[str, Any]]) -> None:
        """Seed the todos channel onto the freshly rebuilt agent's brand-new thread.

        Discoveries: plain `aupdate_state({"messages": ..., "todos": ...})` on a thread with
        NO prior checkpoint resolves an unspecified `as_node` to the graph's own input node
        (`"__start__"`), and THAT node's writers only cover the graph's input schema
        (`messages`) -- any other key in the SAME call, `todos` included, is silently dropped,
        not an error. A SEPARATE call naming the deepagents middleware node that actually owns
        the todos channel (`as_node="TodoListMiddleware.after_model"`, found by introspecting
        `agent.nodes` at runtime -- no public seam names it) does persist it correctly. Tied to
        `deepagents==0.7.5` (pyproject.toml): a version bump that renames, removes, or changes
        that node's behavior degrades to "no todos carried across the switch" -- verified by
        re-reading the state after the attempt (this node's writer has been observed to fail
        SILENTLY as well as by raising, so a bare `except` alone would miss that case), and
        disclosed as a `RunLog` incident rather than crashing the switch outright.
        """
        detail: str | None = None
        try:
            await self.agent.aupdate_state(  # type: ignore[attr-defined]
                self._run_config,
                {"todos": old_todos},
                as_node="TodoListMiddleware.after_model",
            )
            seeded = await self.agent.aget_state(self._run_config)  # type: ignore[attr-defined]
            if not seeded.values.get("todos"):
                detail = "the seed attempt completed without persisting the todos channel"
        except Exception as exc:  # noqa: BLE001 — best-effort; see the docstring above
            detail = f"{type(exc).__name__}: {exc}"
        if detail is not None:
            self._run_log.record(
                "model_switch_todos_not_carried",
                f"the todos channel could not be reseeded across the model switch: {detail}",
            )

    # --- the two lead tools -----------------------------------------------------------------

    def dispatch(self, label: str, objective: str, output_format: str, boundaries: str) -> str:
        """Start one researcher in the background, or refuse (D1/D3).

        Synchronous by design: `dispatch_researcher` must return within its own tool node, so
        the lead's turn ends while the researcher is still working. The task handle lives in
        `self.running` until `_run_researcher` retires it.
        """
        if self.answer is not None:
            return "refused: research is closed — the report is written"
        # The refusal is the harness's, not the prompt's — a lead that ignores the advice
        # still cannot exceed `[agent] max_researchers`.
        if len(self.running) >= self._config.agent.max_researchers:
            return f"refused: {len(self.running)} researchers already running — wait for a return"

        self._next_id += 1
        researcher_id = f"researcher/{self._next_id}"
        brief = (
            f"Objective: {objective}\n\nOutput format: {output_format}\n\nBoundaries: {boundaries}"
        )
        # Before the task exists, so the roster row is already open when the researcher's very
        # first tool call pushes an activity change through the same sink.
        self._sink.start_researcher(researcher_id, label)
        self._emit_researchers()
        self.running[researcher_id] = asyncio.create_task(
            self._run_researcher(researcher_id, label, brief)
        )
        self._arm_clock()
        return f"{researcher_id} ({label}) started"

    def submit(self, answer: str) -> str:
        """Accept the lead's final answer and close research (D3), or refuse (3F F2).

        Closing research cancels whatever is still running, so a submit while the roster is
        non-empty silently throws away those angles. The prompt already says not to; this is
        the harness's own guard, like the dispatch cap. The one exception is the forced
        synthesis pass: it is bounded and is the run's last chance to produce a report, so a
        submit there is accepted and the cancellations are disclosed instead.
        """
        if self.answer is not None:
            return "refused: research is closed — the report is written"
        if self.running and not self._forced_synthesis:
            return (
                f"refused: {len(self.running)} researchers still running — wait for their returns"
            )
        self.answer = answer
        # R6: the clock spans first question → report written. Research is over at this line, so
        # everything the session spends after it — verification, the report itself, and the
        # post-report chat Phase 3 adds — is unclocked and can no longer cut the run short.
        if self._clock is not None:
            self._clock.reschedule(None)
        return "report accepted — research is closed"

    # --- researchers ------------------------------------------------------------------------

    @property
    def clock_armed(self) -> bool:
        """Whether the wall clock is ticking right now (R6).

        Read off the timeout rather than kept as a second flag, so it cannot disagree with the
        object that would actually fire: armed at the first ACCEPTED dispatch, disarmed at
        `submit_report`, and False both before either and for a run that never dispatched.
        """
        return self._clock is not None and self._clock.when() is not None

    def _arm_clock(self) -> None:
        """Arm the wall clock at the first accepted dispatch (R6; re-keyed off `task` by D1)."""
        if self._clock_ever_armed or self._clock is None:
            return
        self._research_started_at = asyncio.get_running_loop().time()
        self._clock.reschedule(self._research_started_at + self._config.agent.wall_clock_seconds)
        self._clock_ever_armed = True

    async def _invoke_researcher(self, brief: str) -> str:
        state = await self._graph.ainvoke(
            {"messages": [HumanMessage(content=brief)]},
            config={"recursion_limit": _recursion_backstop(self._config)},
        )
        return _final_answer(state["messages"])

    async def _run_researcher(self, researcher_id: str, label: str, brief: str) -> None:
        """Run one researcher to completion and queue its return.

        The `task` tool's whole failure policy, reproduced because no tool node wraps this any
        more: exactly one retry (the `ToolRetryMiddleware(max_retries=1)` the lead's guard gave
        `task`), except for the pass-through failures, which are the run's own abort conditions
        rather than the researcher's fault — those become `self._fatal` AND still queue an
        EMPTY return so the loop wakes up to raise them. Empty, and with no `subagent_failed`
        incident, because the researcher did nothing wrong (3F F3): `_task_failure_handler`'s
        propagate branch does not blame the subagent for a mid-run search abort or a display
        bug either, and the loop raises `_fatal` before those findings could reach the lead.

        A CANCELLED researcher (Ctrl-C, wall clock, `/new` in Phase 6) queues nothing: it is
        gone, not finished, and a phantom return would put fabricated findings in the lead's
        context. It is still removed from the roster, which is what the `finally` is for.
        """
        started = time.monotonic()
        findings = ""
        failed = False
        try:
            try:
                findings = await self._invoke_researcher(brief)
            except Exception as exc:
                if _retry_on_non_search_abort(exc):
                    findings, failed = await self._retry_researcher(brief)
                else:
                    self._fatal = exc
                    failed = True
        finally:
            self.running.pop(researcher_id, None)

        # `failed` is carried out of the frames that know, rather than sniffed from `findings`
        # or `self._fatal` afterwards: the findings text is the LEAD's contract, not a status
        # field, and a pass-through failure raised by ANOTHER researcher would set `_fatal` and
        # mislabel this one.
        self._finish_roster_row(researcher_id, failed=failed)
        self.events.put_nowait(
            ResearcherReturn(researcher_id, label, findings, time.monotonic() - started)
        )

    def _finish_roster_row(self, researcher_id: str, *, failed: bool) -> None:
        """Close this researcher's roster row without ever letting the display abort the run.

        The sink pushes straight to the renderer, so this can raise `DisplayError` — and on
        both paths that close a row, an escape would be worse than the bug it reports. Here it
        would strand the `ResearcherReturn` below and hang the loop on a queue nothing will
        ever fill; in `_cancel_running` it would escape `run()`'s own `finally`, past every
        handler, as a traceback. Recorded as `_fatal` instead, which the loop raises as the
        failed run a display bug deserves.
        """
        try:
            self._sink.finish_researcher(researcher_id, failed=failed)
            # Inside the same guard as the sink write, deliberately: this is the display call
            # the docstring above is about, and `_cancel_running` reaches the roster only
            # through here — one emit site covers both paths that close a row.
            self._emit_researchers()
        except Exception as exc:  # noqa: BLE001 — nothing may escape; see the docstring
            self._fatal = exc

    async def _retry_researcher(self, brief: str) -> tuple[str, bool]:
        """The one retry a crashed researcher gets; a second crash becomes its findings.

        Returns the findings AND whether the researcher failed — the roster's `done`/`failed`
        split (R8), which only this frame can tell apart.
        """
        try:
            return await self._invoke_researcher(brief), False
        except Exception as exc:
            # A pass-through failure on the RETRY is still the run's abort, not the
            # researcher's fault — same split as the first attempt (3F F3).
            if not _retry_on_non_search_abort(exc):
                self._fatal = exc
                return "", True
            return subagent_failure_text(self._run_log, "researcher", exc), True

    async def _cancel_running(self) -> None:
        """Cancel every live researcher and wait for it, before the run is torn down (Risk #2).

        A snapshot, because each task's own `finally` mutates `self.running` as it unwinds.

        Each cancellation is one `RunLog` incident (3F F2): a cancelled researcher queues no
        return, so its angle is coverage the report silently lost — best-effort + disclose puts
        it in `## Gaps and disclosures` by name rather than leaving the reader to notice a
        planned angle that no paragraph cites.
        """
        labels = {state.id: state.label for state in self._sink.researchers()}
        cancelled = list(self.running.items())
        for researcher_id, task in cancelled:
            task.cancel()
            # It never returned, so the roster shows it as `failed`, not `done`.
            self._finish_roster_row(researcher_id, failed=True)
            self._run_log.record(
                "researcher_cancelled",
                f"{researcher_id} ({labels.get(researcher_id, 'no label')}) was cancelled "
                "before it returned — its findings never reached the lead",
            )
        if cancelled:
            await asyncio.gather(*(task for _, task in cancelled), return_exceptions=True)

    # --- display ----------------------------------------------------------------------------

    def on_activity_change(self) -> None:
        """Pushed by the `ActivitySink` the instant it changes (fix-pass item 1) -- the single
        path from a tool-activity mutation to the renderer.

        Nothing is swallowed: a failure to build or emit an event is a real bug. But this runs
        inside `awrap_tool_call`, so a bare exception on a `task` dispatch would be absorbed by
        that tier's retry/error guard and reported as `"READER FAILED"` after re-running the
        whole subagent. Re-raised as `DisplayError`, which `harness/agent.py` excludes from that
        guard, so a display bug fails the run AS a display bug.
        """
        try:
            self._push_activity()
        except DisplayError:
            raise
        except Exception as exc:
            raise DisplayError(f"the display failed while rendering tool activity: {exc}") from exc

    def _push_activity(self) -> None:
        """`on_activity_change`'s body, split out so the wrapper above has one `try` to guard."""
        self._emit_new_tool_calls()
        # The todo LIST dedupe (`_last_todos`) stays untouched -- this only refreshes the ACTIVE
        # row's meta when the live-reader count moved since the last emit, so a reader
        # starting/finishing mid-dispatch is reflected without re-emitting on every mutation.
        if self._last_todos is not None:
            in_flight = self._sink.live_reader_count()
            if in_flight != self._last_in_flight:
                self._renderer.emit(
                    TodosUpdated(_todo_items(self._last_todos, self._registry, self._sink))
                )
                self._last_in_flight = in_flight

    def _emit_new_tool_calls(self) -> None:
        records = self._sink.records()
        for record in records[self._tool_calls_emitted :]:
            self._renderer.emit(
                ToolCall(
                    call_id=record.call_id,
                    tool=record.tool,
                    arg_summary=record.arg_summary,
                    result_summary=record.result_summary,
                    elapsed_seconds=record.elapsed_seconds,
                    retry=record.retry,
                )
            )
        self._tool_calls_emitted = len(records)

    def _emit_researchers(self) -> None:
        """The roster (R8) as the display sees it, emitted beside every write to the sink.

        Elapsed is rendered HERE, not in the renderer: only this side knows a running row
        started at `started_at` on the loop's clock. A running row's time is therefore as fresh
        as the last roster write — deliberately not a ticking timer, since the next dispatch or
        return refreshes it and a per-frame recount would need the clock in the display layer.
        """
        now = time.monotonic()
        self._renderer.emit(
            ResearchersUpdated(
                tuple(
                    ResearcherItem(
                        id=state.id,
                        label=state.label,
                        status=state.status,
                        elapsed=format_status_elapsed(
                            (state.finished_at if state.finished_at is not None else now)
                            - state.started_at
                        ),
                    )
                    for state in self._sink.researchers()
                )
            )
        )

    def _emit_new_alerts(self) -> None:
        """Live disclosure (best-effort + disclose): every incident a tool records is echoed to
        the terminal as soon as the stream yields control back, and the counter keeps a later
        poll from re-printing what an earlier one already showed."""
        incidents = self._run_log.incidents()
        for incident in incidents[self._alerts_emitted :]:
            self._renderer.emit(Alert(incident.detail))
        self._alerts_emitted = len(incidents)

    def _emit_source_count(self) -> None:
        """Emit the live source counter (R5), but only when it actually changed.

        Change-gated like the todo dedupe: the poll runs per stream chunk, and a repeated
        identical count would repaint the frame and spam the non-TTY log for nothing.
        """
        count = self._registry.count()
        if count != self._last_source_count:
            self._renderer.emit(SourcesUpdated(count))
            self._last_source_count = count

    # --- the turn loop ----------------------------------------------------------------------

    def _note_model_turns(self, node_update: dict[str, Any]) -> None:
        """Advance the round count for each model turn in one node update (R7).

        Counted here, in code the harness owns, never derived from `recursion_limit`:
        supersteps-per-round is a graph-topology detail of the installed framework versions,
        so any user-facing budget derived from it drifts silently on upgrade. Deduplicated by
        message id because middleware nodes may re-emit an already-counted message; a
        researcher's own turns run in a separate graph and never reach this stream, so rounds
        are the LEAD's turns.

        Post-report chat turns are uncapped (3F Major 2): the research loop has already
        exited by then, so no synthesis pass could rescue anything — counting chat turns
        would only push `_rounds_used` over `max_rounds`, fire that pass on a SUCCESSFUL run,
        stamp `round_cap` onto it, and leave `_overrun` breaking every later chat turn
        mid-stream. The margin check carries the same guard. The guard sits on the COUNTING
        branch, never above the loop: a ToolMessage that arrives after the answer exists —
        the capped submit's own tool result — must still drain `_awaiting_tool_ids`, or the
        capped turn's stream would never break for its synthesis pass.
        """
        max_rounds = self._config.agent.max_rounds
        for message in node_update.get("messages") or []:
            if isinstance(message, ToolMessage):
                self._awaiting_tool_ids.discard(message.tool_call_id)
                continue
            if not isinstance(message, AIMessage):
                continue
            if self.answer is not None:
                continue
            if message.id is not None:
                if message.id in self._counted_turn_ids:
                    continue
                self._counted_turn_ids.add(message.id)
            self._rounds_used += 1
            self._renderer.emit(RoundsUpdated(self._rounds_used, max_rounds))
            if self._rounds_used > max_rounds:
                self._overrun = True
            elif self._rounds_used == max_rounds:
                # The turn AT the cap may already be the tool-free final answer — only a turn
                # proposing more tool work owes a synthesis pass, and only after those tools
                # finish, so the thread never ends on dangling tool calls.
                call_ids = _pending_tool_call_ids(message)
                if call_ids:
                    self._awaiting_tool_ids.update(call_ids)
                    self._cap_hit = True

    def _handle_node_update(self, node_update: dict[str, Any]) -> None:
        """Everything one node update tells the display and the budgets."""
        todos = node_update.get("todos")
        if todos is not None and todos != self._last_todos:
            self._renderer.emit(TodosUpdated(_todo_items(todos, self._registry, self._sink)))
            self._last_todos = todos
            self._last_in_flight = self._sink.live_reader_count()

        # The lead's own prose into the transcript (R2): a narration turn is how the lead tells
        # the developer what a return meant, and nothing else in the display shows it. Skipped
        # for tool-call-only messages, whose text content is empty.
        for message in node_update.get("messages") or []:
            if not isinstance(message, AIMessage):
                continue
            if message.id is not None:
                if message.id in self._agent_text_ids:
                    continue
                self._agent_text_ids.add(message.id)
            text = _message_text(message)
            if text:
                self._renderer.emit(AgentText(text, self._config.roles["head"].model))

        calls = _dispatch_tool_calls(node_update)
        if calls:
            # Not once the answer exists: a post-report dispatch is REFUSED by the tool, and
            # advancing here would re-open a stage `_finish` already closed out — a live
            # "researching" stage over a run that has stopped researching, which nothing left
            # will ever complete. The activity line still shows, so the attempt is visible.
            if self.answer is None:
                self._tracker.advance("researching")
            for call in calls:
                call_id = str(call.get("id") or "")
                arg_summary = _describe_tool_call(call)
                self._pending_lead_calls[call_id] = (DISPATCH_RESEARCHER_TOOL_NAME, arg_summary)
                self._renderer.emit(
                    LeadToolCall(
                        call_id=call_id,
                        name=DISPATCH_RESEARCHER_TOOL_NAME,
                        arg_summary=arg_summary,
                    )
                )

        self._emit_lead_tool_results(node_update)

        self._note_model_turns(node_update)

        # R7's reserve: fire the same bounded synthesis pass the round cap uses once elapsed
        # research time crosses the margin threshold. The threshold decision itself lives in
        # `_margin_reached` (including the `== 0` disable), which is where its boundaries are
        # tested — see that docstring for why it is not inline.
        # `answer is None`: once submit_report is accepted research is closed (R6) — a slow closing
        # reply crossing the margin must not stamp `synthesis_margin` onto a complete report.
        if not self._margin_hit and self.answer is None and self._research_started_at is not None:
            elapsed = asyncio.get_running_loop().time() - self._research_started_at
            if _margin_reached(
                elapsed,
                self._config.agent.wall_clock_seconds,
                self._config.agent.synthesis_margin_seconds,
            ):
                self._margin_hit = True
                # Same bookkeeping as the cap, through the shared `_pending_tool_call_ids`: if
                # THIS crossing update itself carries a fresh `AIMessage` proposing tool work,
                # rather than a `ToolMessage` settling PRIOR work, that work is what the break
                # below would otherwise leave dangling — deferring it until it answers keeps
                # the thread from ending on unanswered `tool_calls`.
                for message in node_update.get("messages") or []:
                    if isinstance(message, AIMessage):
                        self._awaiting_tool_ids.update(_pending_tool_call_ids(message))

    def _emit_lead_tool_results(self, node_update: dict[str, Any]) -> None:
        """Re-emit each pending lead tool call once its own `ToolMessage` lands (Phase 5).

        Matched by `tool_call_id`, never by position: one lead turn can propose several
        dispatches and their results come back in whatever order the tool node finished them.
        `_summarize_tool_result` is the same first-line-and-cap summarizer the researcher tier's
        log already uses — the lead tier is uninstrumented, so nothing else would summarize this.
        """
        for message in node_update.get("messages") or []:
            if not isinstance(message, ToolMessage):
                continue
            pending = self._pending_lead_calls.pop(message.tool_call_id, None)
            if pending is None:
                continue
            name, arg_summary = pending
            self._renderer.emit(
                LeadToolCall(
                    call_id=message.tool_call_id,
                    name=name,
                    arg_summary=arg_summary,
                    result_summary=_summarize_tool_result(message),
                )
            )

    async def _stream_pass(self, stream_input: Any) -> dict[str, Any] | None:
        """One `astream` pass over the lead. Returns the pass's own final `values` chunk.

        Scoped to THIS pass: interrupt detection must never read a previous pass's
        `__interrupt__`, or a resumed pass emitting no `values` chunk would re-ask the same
        question forever. `self._final_state` still holds the newest state actually seen, so
        the report is assembled from real data.
        """
        pass_state: dict[str, Any] | None = None
        # `aclosing`, because the round cap leaves this loop by `break`: a bare break abandons
        # the generator to garbage collection with langgraph tasks in flight.
        async with aclosing(
            # `cast`: `astream` is typed as a bare AsyncIterator, but it is an async generator
            # at runtime, which is what `aclosing` needs.
            cast(
                "AsyncGenerator[Any, None]",
                self.agent.astream(
                    stream_input, config=self._run_config, stream_mode=["updates", "values"]
                ),
            )
        ) as stream:
            async for mode, chunk in stream:
                if mode == "updates":
                    for node_update in chunk.values():
                        # An interrupt arrives as `{"__interrupt__": (Interrupt(...),)}`, whose
                        # value is a tuple, not a dict — `.get` raises on it.
                        if not node_update or not isinstance(node_update, dict):
                            continue
                        self._handle_node_update(node_update)
                    # Tool-call/reader-strip/todo-meta refreshes are not polled here (fix-pass
                    # item 1): `on_activity_change` pushes them from inside the tool dispatch.
                    self._emit_new_alerts()
                    self._emit_source_count()
                    if (
                        self._overrun
                        or (self._cap_hit and not self._awaiting_tool_ids)
                        or (self._margin_hit and not self._awaiting_tool_ids)
                    ):
                        break
                else:  # mode == "values"
                    pass_state = chunk
                    # Assigned HERE, inside the iteration: every cut-short path leaves this
                    # loop by exception, so an assignment after the `async for` never runs and
                    # the report would lose the token usage on exactly the runs that need
                    # disclosing.
                    self._final_state = chunk
        return pass_state

    async def _synthesis_pass(self) -> None:
        """The one bounded pass a cut-short run gets to call `submit_report` (R7).

        `_overrun` means a turn PAST the cap already started new work, so its tool calls may be
        dangling — appending a synthesis request there would hand the model an invalid
        sequence. Otherwise the capped round's (or margin's) tools have all answered, and one
        bounded pass turns what was reported into a real final answer instead of mid-run
        chatter.
        """
        # `_cap_hit`/`_overrun` win whenever set, even if `_margin_hit` is ALSO set from the
        # SAME chunk — the round cap is the harder bound, so its disclosure takes priority.
        self.cut_short = "round_cap" if (self._cap_hit or self._overrun) else "synthesis_margin"
        # Provisional, and read only if this pass produces no `submit_report`: a cut-short run
        # with no answer writes no report, so `cut_short_detail` is the ONLY thing `__main__`
        # has to print, and it printed a bare `error: None` before (3F F1). The report itself
        # renders the detail on `cut_short == "error"` alone, so a run this pass DOES rescue
        # never shows it.
        self.cut_short_detail = (
            f"the {'round cap' if self.cut_short == 'round_cap' else 'synthesis margin'} was "
            "reached and the lead never called submit_report"
        )
        if self._overrun:
            return
        # From here the pass is FORCED: `submit` stops refusing on a non-empty roster, because
        # this bounded pass is the run's last chance to produce a report at all (3F F2).
        self._forced_synthesis = True

        if self.cut_short == "round_cap":
            self._renderer.emit(
                Activity(
                    f"round cap ({self._config.agent.max_rounds}) reached — asking for a synthesis"
                )
            )
            synthesize_now = _SYNTHESIZE_NOW
        else:
            self._renderer.emit(
                Activity(
                    f"synthesis margin ({self._config.agent.synthesis_margin_seconds}s) "
                    "reached — asking for a synthesis"
                )
            )
            synthesize_now = _SYNTHESIZE_NOW_MARGIN

        synthesis_config: RunnableConfig = {
            **self._run_config,
            "recursion_limit": _SYNTHESIS_RECURSION_LIMIT,
        }
        async with aclosing(
            cast(
                "AsyncGenerator[Any, None]",
                self.agent.astream(
                    {"messages": [HumanMessage(content=synthesize_now)]},
                    config=synthesis_config,
                    stream_mode=["updates", "values"],
                ),
            )
        ) as synthesis:
            async for mode, chunk in synthesis:
                if mode == "values":
                    self._final_state = chunk
                else:
                    self._emit_new_alerts()
                    self._emit_source_count()

    async def _turn(self, next_input: Any) -> None:
        """One lead turn: stream it, answer any clarifying question, honour the bounds.

        Interrupts are handled INSIDE the turn, before any bound: `ask_user` counts as a tool
        call on the capped round and so sets `_cap_hit`, but it pauses the graph instead of
        returning a `ToolMessage`, so `_awaiting_tool_ids` never drains. Handling the cap first
        dropped the question and then resumed a paused thread — the run died and wrote nothing.
        The cap still applies once the answer is delivered.
        """
        stream_input = next_input
        while True:
            pass_state = await self._stream_pass(stream_input)
            interrupts = (pass_state or {}).get("__interrupt__")
            if interrupts:
                # Same guard as the dispatch path: `_finish` has already closed every stage
                # out, so a post-report question must not re-open one nothing will complete.
                if self.answer is None:
                    self._tracker.advance("clarifying")
                # `interrupts[0]`, not all of them: the lead is a single agent node, so at most
                # one is ever pending, and `Command(resume=...)` delivers ONE value — fanning
                # several into one decisions list would mis-pair them.
                decisions = await self._collect_answers(interrupts[0])
                # A mid-research answer hands the header back to the researchers still
                # running; without this the stage sat on `clarifying` until the NEXT
                # dispatch and `stage_timings` recorded a truncated `researching`.
                if self.answer is None and self.running:
                    self._tracker.advance("researching")
                stream_input = Command(resume={"decisions": decisions})
                continue
            if self._cap_hit or self._overrun or self._margin_hit:
                await self._synthesis_pass()
            return

    async def _collect_answers(self, interrupt: Interrupt) -> list[dict[str, Any]]:
        """Ask each pending `ask_user` question and collect one answer per action request.

        One decision per request, in the same order — the middleware raises `ValueError` on a
        count mismatch.

        Only WHERE the text comes from belongs to `__main__` (`answer_source`: the composer
        while a keyboard exists, the stdin bridge otherwise); everything around it is the
        session's, because the session already owns the renderer, the registry and the stage
        tracker this needs.

        An answer is user-supplied text exactly like the initial question, so any URL pasted
        into it is approved here (D2/R2) — the natural reply to "which page do you mean?" is
        the URL itself, and without this it stayed provenance-rejected for the rest of the run.

        `tracker.pause()`/`resume()` and the `QuestionAnswered` emit sit around the read in a
        `finally`, so a `KeyboardInterrupt` or a wall-clock cancellation mid-question cannot
        leave the displayed clock paused and the overlay stuck open. The WALL clock is a
        different clock entirely and nothing here touches it.
        """
        decisions: list[dict[str, Any]] = []
        for request in interrupt.value["action_requests"]:
            args = request.get("args", {})
            question = args.get("question") or request.get("description") or str(args)
            # Clamped HERE, not only in the schema: the interrupt path never executes the
            # tool, so `args_schema`'s `max_length=4` is a model-facing hint — a lead that
            # sends six choices anyway must not get six numbered rows (R4).
            choices = tuple(args.get("choices") or ())[:4]
            self._renderer.emit(Question(question, choices=choices))
            self._tracker.pause()
            try:
                answer = await self._answer_source()
            finally:
                self._tracker.resume()
                self._renderer.emit(QuestionAnswered())
            for url in extract_urls(answer):
                self._registry.approve(url)
            decisions.append({"type": "respond", "message": _resolve_answer(answer, choices)})
        return decisions

    def _batch_message(self, batch: list[SessionEvent]) -> str:
        """Fold one drained event batch into the single `HumanMessage` the lead sees (R2).

        Arrival order, findings verbatim, one `Roster:` line closing the whole batch — so the
        lead reads the roster exactly once per turn and can tell "wait" from "everyone is in".
        """
        parts: list[str] = []
        for event in batch:
            if isinstance(event, ResearcherReturn):
                parts.append(f"[{event.id} — {event.label}] returned:\n{event.findings}")
            else:
                parts.append(event.text)
        roster = self._sink.researchers()
        # Finished is finished: a researcher that crashed is off the roster and nothing more is
        # coming from it, so the lead reads it under `done` rather than waiting on it forever.
        done = ", ".join(state.id for state in roster if state.status != "running") or "none"
        running = ", ".join(state.id for state in roster if state.status == "running") or "none"
        return "\n\n".join(parts) + f"\nRoster: done {done} · running {running}"

    def _drain_events(self) -> list[_QueueEvent | _Quit]:
        """Everything queued right now, oldest first — never blocking."""
        batch: list[_QueueEvent | _Quit] = []
        while True:
            try:
                batch.append(self.events.get_nowait())
            except asyncio.QueueEmpty:
                return batch

    async def _next_batch(self) -> list[SessionEvent]:
        """Block for at least one event, drain the rest, strip the quit sentinel, and apply
        any `ModelSwitch` in arrival order BEFORE returning what is left (Design: "ModelSwitch
        first, then the normal message build").

        The one place a batch is built, so `_QUIT` can never reach `_batch_message` from either
        the turn loop or the post-report chat loop, and a `ModelSwitch` can never reach it
        either — applying it here, not in `_batch_message`, is what keeps the model from ever
        being told "a ModelSwitch happened" as text. An EMPTY result with NO quit sentinel in
        the drained batch means only `ModelSwitch`es arrived: looped back for the next wakeup
        rather than returned, so `run()`'s own `if not batch` (a failed pre-report quit) is
        never mistaken for a `/model` command that had nothing else to send yet.
        """
        while True:
            batch: list[_QueueEvent | _Quit] = [await self.events.get()]
            batch.extend(self._drain_events())
            saw_quit = any(isinstance(event, _Quit) for event in batch)
            remaining: list[SessionEvent] = []
            for event in batch:
                if isinstance(event, _Quit):
                    continue
                if isinstance(event, ModelSwitch):
                    await self._apply_model_switch(event)
                else:
                    remaining.append(event)
            if remaining or saw_quit:
                return remaining

    def _next_input(self, batch: list[SessionEvent]) -> dict[str, Any]:
        """One drained batch as the lead's next input, echoing each user line as it is sent.

        The echo lives here rather than at the call sites so a message cannot reach the model
        without also reaching the transcript — the developer must be able to see that what they
        typed was delivered, and on which turn.
        """
        for event in batch:
            if isinstance(event, UserMessage):
                self._renderer.emit(UserTurn(event.text))
        return {"messages": [HumanMessage(content=self._batch_message(batch))]}

    async def run(self) -> RunOutcome | None:
        """Drive the session to a written report, then chat over it until the developer quits.

        Returns the `RunOutcome` that was written, or `None` when no report was written at all
        (D3/R5: a hard error, a user abort, an answer-less clock, or a lead that never called
        `submit_report`). `__main__` maps that to the exit code; the browser and the renderer
        are its to close, after this returns.

        An INTERACTIVE session does not return the moment the report lands: research ends
        there, but the chat does not (R5), so `_chat_loop` keeps answering over the same thread
        and the same sources until `request_quit`. A headless one returns immediately, exactly
        as before this phase.
        """
        self._run_task = asyncio.current_task()
        # First thing the display is told: the session bar's crumb names WHICH question this
        # screen belongs to, and a long chat scrolls the opening line itself away.
        self._renderer.emit(RunStarted(" ".join(self._question.split()[:_CRUMB_WORDS])))
        self.agent = build_agent(
            self._config, self._registry, self._run_log, self._sink, self._browser, self
        )
        self._graph = build_researcher_graph(
            self._config, self._registry, self._run_log, self._sink, self._browser
        )
        self._run_config = {
            "configurable": {"thread_id": self.thread_id},
            # A runaway BACKSTOP, never the round cap. The cap is counted by
            # `_note_model_turns` in the unit it actually means — model turns — because
            # supersteps-per-round is a topology detail owned by the installed
            # deepagents/langchain versions.
            "recursion_limit": _recursion_backstop(self._config),
        }

        next_input: Any = {"messages": [HumanMessage(content=self._question)]}
        nudged = False
        try:
            # `asyncio.timeout(None)` starts disarmed, so a clarifying wait before the first
            # dispatch is never bounded. This shape rather than `asyncio.wait_for` because the
            # deadline is unknown until the first dispatch and must span every turn and every
            # wait between them.
            async with asyncio.timeout(None) as clock:
                self._clock = clock
                while True:
                    await self._turn(next_input)
                    if self._fatal is not None:
                        raise self._fatal
                    if self.answer is not None or self.cut_short is not None:
                        break
                    # The nudge is the HEADLESS ending: with no key source there is nobody to
                    # wait for, so an idle lead is out of moves and the run must end. An
                    # interactive session falls through to the blocking wait instead — the
                    # developer is still there, and R1 says a message may arrive at any time.
                    if not self.running and self.events.empty() and not self._interactive:
                        if nudged:
                            self.cut_short = "error"
                            self.cut_short_detail = (
                                "the lead ended its turn without calling submit_report"
                            )
                            break
                        nudged = True
                        next_input = {"messages": [HumanMessage(content=_SUBMIT_NOW)]}
                        continue
                    batch = await self._next_batch()
                    if self._fatal is not None:
                        raise self._fatal
                    if not batch:
                        # Nothing but the quit sentinel: a quit that landed BETWEEN turns, so
                        # there was no in-flight turn for `request_quit` to cancel. Same
                        # ending as the cancelled one — no report exists yet (R5).
                        self.cut_short = "error"
                        self.cut_short_detail = "user abort (Ctrl+C)"
                        break
                    next_input = self._next_input(batch)
        except TimeoutError as exc:
            # `clock.expired()`, not a bare `except TimeoutError`: a timeout raised INSIDE the
            # run (an `asyncio.wait_for` in a tool, say) would otherwise be reported as "the
            # wall clock stopped this run", which is untrue and hides the real failure.
            if self._clock is not None and self._clock.expired():
                self.cut_short = "wall_clock"
            else:
                self.cut_short = "error"
                self.cut_short_detail = f"{type(exc).__name__}: {exc}"
        except GraphRecursionError:  # must precede `Exception` — it subclasses RuntimeError
            # Two sources, one meaning: the synthesis pass's small limit (a lead that kept
            # calling tools despite the instruction) or the runaway backstop. Either way the
            # run ended on a rounds-related bound — but if the synthesis pass that ran away was
            # ITSELF the margin's own, keep that label rather than naming the wrong bound (G4).
            if self.cut_short != "synthesis_margin":
                self.cut_short = "round_cap"
                self.cut_short_detail = (
                    "the recursion limit was reached and the lead never called submit_report"
                )
        except Exception as exc:  # noqa: BLE001 — never `BaseException`; see the clause below
            self.cut_short = "error"
            self.cut_short_detail = f"{type(exc).__name__}: {exc}"
        except (KeyboardInterrupt, asyncio.CancelledError):
            # D2: a user abort maps onto the existing hard-error path (no new outcome kind) —
            # its own clause because both are `BaseException`s, not caught by `Exception`. A
            # Ctrl+C raised inside a model/tool call has been observed to surface here as
            # `CancelledError` rather than `KeyboardInterrupt`.
            self.cut_short = "error"
            self.cut_short_detail = "user abort (Ctrl+C)"
        finally:
            # Cleared before the teardown awaits (3F Minor d): past the turn loop a quit has
            # nothing left to cancel, and `_run_task` is main()'s own task in production —
            # leaving it set would let a key landing after run() returned cancel main()'s
            # `finally` mid-`browser.close()`.
            self._run_task = None
            await self._cancel_running()

        outcome = await self._finish()
        # The report is written at submit time, but the run is only OVER once the chat is:
        # `RunFinished` stops the live region and prints the summary on the normal terminal, so
        # it cannot fire until there is nothing left to render (D3).
        if outcome is not None and self._interactive:
            await self._chat_loop()
        self._emit_finished()
        return outcome

    async def _chat_loop(self) -> None:
        """Post-report chat: same thread, same sources, no clock, no new research (R5/D3).

        `dispatch_researcher` and `submit_report` already refuse once `self.answer` is set, so
        nothing here re-implements that gate. A cancellation is a clean end rather than a
        failure — the report is on disk, and `run()` still returns the outcome.

        `KeyboardInterrupt` beside `CancelledError`, as in `run()`'s own abort clause: a Ctrl+C
        is a QUIT, not a failed turn, so it must not fall to `_chat_turn`'s disclosure — and
        being a `BaseException` it is caught by neither that clause nor `run()`'s (which is
        behind us), so unhandled it escapes `run()` entirely, skipping the end-of-run summary
        and the outcome on a run whose report is already on disk.
        """
        try:
            while not self._quit:
                batch = await self._next_batch()
                if self._quit:
                    return
                if batch:
                    await self._chat_turn(self._next_input(batch))
        except (asyncio.CancelledError, KeyboardInterrupt):
            return

    async def _chat_turn(self, next_input: Any) -> None:
        """One post-report turn, with failed turns disclosed instead of fatal (3F Major 1).

        `run()`'s `except` clauses are behind us — a provider error here must not escape,
        skip `_emit_finished`, and turn a run whose report is already on disk into exit 1 or
        a traceback (R5: quitting after the report is a CLEAN exit). `cut_short` stays None
        because the run already succeeded; the error is disclosed as an alert and the chat
        stays open, so the developer can retry the line or quit.
        """
        try:
            await self._turn(next_input)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never `BaseException`, as in `run()`
            self._renderer.emit(
                Alert(
                    f"a chat turn failed ({type(exc).__name__}: {exc}) — the report is safe "
                    "on disk; the chat is still open, retry or quit"
                )
            )

    def _emit_finished(self) -> None:
        """The end-of-run summary, emitted once both research and any chat are over.

        Reads `_verification`/`_report_path` off the session rather than taking the outcome:
        a FAILED run has no `RunOutcome` at all, and its verification failures and cut-short
        reason still belong in the summary.
        """
        usable, unusable = partition_sources(self._config, self._registry)
        self._renderer.emit(
            RunFinished(
                stage_timings=self._tracker.timings(),
                usable_sources=len(usable),
                unusable_sources=len(unusable),
                cut_short=self.cut_short,
                verification_failures=(
                    len(self._verification.check_failures) if self._verification else 0
                ),
                incidents=len(self._run_log.incidents()),
                report_path=self._report_path,
            )
        )

    async def _finish(self) -> RunOutcome | None:
        """Verify, assemble and (if the gate allows) write the report."""
        # One last poll: the final tool executions (or a cut-short pass) may have recorded
        # incidents after the last updates chunk was handled.
        self._emit_new_alerts()
        self._emit_source_count()

        messages: list[BaseMessage] = self._final_state["messages"] if self._final_state else []
        usage = _sum_usage(messages)

        # D3: the answer is the `submit_report` argument, never the last `AIMessage`.
        answer = self.answer or ""
        # Split exactly once (D2): `verify_paragraphs` and `report.py`'s `## Answer` renderer
        # share this one list; nothing ever re-splits `answer`.
        paragraphs = split_paragraphs(answer)
        verification = None
        if self.cut_short == "error":
            # The head model is near-certainly still unreachable, so one call per paragraph —
            # each with its own bounded backoff — would burn minutes before the report is even
            # written. The skip is disclosed via `check_failures`, never silent.
            verification = VerificationResult(
                check_failures=[
                    "verification skipped: the run ended in an error, so claims were not checked"
                ]
            )
        elif answer:
            self._tracker.advance("verifying")
            self._renderer.emit(
                Activity(f"checking {len(paragraphs)} paragraph(s) against their cited sources")
            )
            try:
                verification = await verify_paragraphs(
                    paragraphs,
                    self._config,
                    self._registry,
                    # Per-paragraph progress: each pooled check is one model call that can take
                    # minutes, so without this the verifying stage shows nothing until it ends.
                    on_paragraph=lambda i, n: self._renderer.emit(
                        Activity(f"checking paragraph {i}/{n}")
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                # Best-effort + disclose: a pass that fails wholesale is reported IN the report.
                # Per-paragraph failures are handled inside the pass itself.
                verification = VerificationResult(
                    check_failures=[f"verification pass failed: {type(exc).__name__}: {exc}"]
                )

        outcome = RunOutcome(
            question=self._question,
            answer=answer,
            registry=self._registry,
            usage=usage,
            cut_short=self.cut_short,
            cut_short_detail=self.cut_short_detail,
            todos=self._last_todos or [],
            started_at=self._started_at,
            paragraphs=paragraphs,
            verification=verification,
            incidents=self._run_log.incidents(),
        )
        # D3's gate: a report exists if and only if the lead submitted one and the run did not
        # end in a hard error or a user abort. The round cap, the synthesis reserve and a wall
        # clock that expired after a submitted answer all keep the disclosed report.
        should_write_report = self.answer is not None and self.cut_short != "error"

        self._tracker.advance("writing")
        path: Path | None = None
        if should_write_report:
            path = write_report(outcome, self._config)
        self._tracker.finish()
        # Held for `_emit_finished`, which now runs after any post-report chat rather than here.
        self._verification = verification
        self._report_path = path
        if path is not None:
            # The path in the transcript, the moment it exists: `RunFinished` no longer follows
            # this line immediately, and a developer chatting after the report should not have
            # to quit to find out where it was written.
            self._renderer.emit(ReportWritten(path))
        return outcome if should_write_report else None


def _recursion_backstop(config: HarnessConfig) -> int:
    """The runaway superstep backstop, shared by the lead and every researcher invocation.

    Sized ~5x anything the counted round cap could legitimately need, so it only trips if the
    counting fails or a graph loops without producing model turns.
    """
    return config.agent.max_rounds * _BACKSTOP_SUPERSTEPS_PER_ROUND + _BACKSTOP_FLOOR
