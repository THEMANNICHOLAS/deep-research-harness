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
from harness.activity import ActivitySink, DisplayError, brief_summary
from harness.agent import (
    _retry_on_non_search_abort,
    build_agent,
    build_researcher_graph,
    subagent_failure_text,
)
from harness.config import HarnessConfig
from harness.display import (
    Activity,
    Alert,
    ReaderItem,
    ReadersUpdated,
    Renderer,
    RoundsUpdated,
    RunFinished,
    SourcesUpdated,
    StageTracker,
    TodoItem,
    TodosUpdated,
    ToolCall,
)
from harness.paragraphs import split_paragraphs
from harness.report import CutShortReason, RunOutcome, partition_sources, write_report
from harness.runlog import RunLog
from harness.sources import SourceRegistry
from harness.tools.dispatch import DISPATCH_RESEARCHER_TOOL_NAME
from harness.verify import VerificationResult, verify_paragraphs

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from harness.browser import BrowserSession

_EMPTY_USAGE: UsageMetadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

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


SessionEvent = ResearcherReturn | UserMessage


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
    """One activity line describing a researcher-dispatch proposal.

    The label, not the objective: the label is authored to be a 2-5 word roster entry, whereas
    the objective is the model's full delegation brief and painted a paragraph-sized blob that
    pushed the frame past the terminal height (PR #25 review). `brief_summary` still bounds it,
    since the label is model-supplied and nothing enforces its length.
    """
    args = call.get("args") or {}
    return f"{DISPATCH_RESEARCHER_TOOL_NAME}: {brief_summary(str(args.get('label', '')))}"


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
        answer_interrupt: Callable[[Interrupt], Awaitable[list[dict[str, Any]]]],
        started_at: datetime,
    ) -> None:
        self._config = config
        self._registry = registry
        self._run_log = run_log
        self._renderer = renderer
        self._tracker = tracker
        self._question = question
        self._sink = activity.or_default(sink)
        self._browser = browser
        self._answer_interrupt = answer_interrupt
        self._started_at = started_at

        self.events: asyncio.Queue[SessionEvent] = asyncio.Queue()
        self.running: dict[str, asyncio.Task[None]] = {}
        self.answer: str | None = None
        self.agent: Runnable
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
        self._last_todos: list[dict[str, Any]] | None = None
        self._alerts_emitted = 0
        self._last_source_count = 0
        self._tool_calls_emitted = 0
        self._last_readers: tuple[Any, ...] | None = None
        self._last_in_flight: int | None = None

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
        self._emit_readers()
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

    def _emit_readers(self) -> None:
        readers = self._sink.readers()
        if readers == self._last_readers:
            return
        self._renderer.emit(
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
        self._last_readers = readers

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
        """
        max_rounds = self._config.agent.max_rounds
        for message in node_update.get("messages") or []:
            if isinstance(message, ToolMessage):
                self._awaiting_tool_ids.discard(message.tool_call_id)
                continue
            if not isinstance(message, AIMessage):
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

        calls = _dispatch_tool_calls(node_update)
        if calls:
            self._tracker.advance("researching")
            for call in calls:
                self._renderer.emit(Activity(_describe_tool_call(call)))

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
                self._tracker.advance("clarifying")
                # `interrupts[0]`, not all of them: the lead is a single agent node, so at most
                # one is ever pending, and `Command(resume=...)` delivers ONE value — fanning
                # several into one decisions list would mis-pair them.
                decisions = await self._answer_interrupt(interrupts[0])
                stream_input = Command(resume={"decisions": decisions})
                continue
            if self._cap_hit or self._overrun or self._margin_hit:
                await self._synthesis_pass()
            return

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

    def _drain_events(self) -> list[SessionEvent]:
        """Everything queued right now, oldest first — never blocking."""
        batch: list[SessionEvent] = []
        while True:
            try:
                batch.append(self.events.get_nowait())
            except asyncio.QueueEmpty:
                return batch

    async def run(self) -> RunOutcome | None:
        """Drive the session to a written report, or to a failed run.

        Returns the `RunOutcome` that was written, or `None` when no report was written at all
        (D3/R5: a hard error, a user abort, an answer-less clock, or a lead that never called
        `submit_report`). `__main__` maps that to the exit code; the browser and the renderer
        are its to close, after this returns.
        """
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
                    if not self.running and self.events.empty():
                        if nudged:
                            self.cut_short = "error"
                            self.cut_short_detail = (
                                "the lead ended its turn without calling submit_report"
                            )
                            break
                        nudged = True
                        next_input = {"messages": [HumanMessage(content=_SUBMIT_NOW)]}
                        continue
                    batch: list[SessionEvent] = [await self.events.get()]
                    batch.extend(self._drain_events())
                    if self._fatal is not None:
                        raise self._fatal
                    next_input = {"messages": [HumanMessage(content=self._batch_message(batch))]}
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
            await self._cancel_running()

        return await self._finish()

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
        usable, unusable = partition_sources(self._config, self._registry)
        self._renderer.emit(
            RunFinished(
                stage_timings=self._tracker.timings(),
                usable_sources=len(usable),
                unusable_sources=len(unusable),
                cut_short=self.cut_short,
                verification_failures=len(verification.check_failures) if verification else 0,
                incidents=len(self._run_log.incidents()),
                report_path=path,
            )
        )
        return outcome if should_write_report else None


def _recursion_backstop(config: HarnessConfig) -> int:
    """The runaway superstep backstop, shared by the lead and every researcher invocation.

    Sized ~5x anything the counted round cap could legitimately need, so it only trips if the
    counting fails or a graph loops without producing model turns.
    """
    return config.agent.max_rounds * _BACKSTOP_SUPERSTEPS_PER_ROUND + _BACKSTOP_FLOOR
