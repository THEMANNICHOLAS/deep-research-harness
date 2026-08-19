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
from contextlib import aclosing
from datetime import datetime
from functools import reduce
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata, add_usage

from harness.config import ConfigError, load_config
from harness.display import (
    Activity,
    Alert,
    Question,
    Renderer,
    RunFinished,
    StageTracker,
    TodoItem,
    TodosUpdated,
    build_renderer,
)
from harness.paragraphs import split_paragraphs
from harness.report import CutShortReason, RunOutcome, partition_sources, write_report
from harness.runlog import RunLog
from harness.sources import SourceRegistry
from harness.tools.search import SearchPreflightError, preflight_search
from harness.verify import VerificationResult, verify_paragraphs

if TYPE_CHECKING:
    # Annotation-only: the runtime imports of langgraph and the agent/model stack are
    # deferred into `main` — they cost several seconds, and `--help` or a config error
    # should not pay them (see the deferred-import block there).
    from collections.abc import AsyncGenerator

    from langchain_core.runnables import RunnableConfig
    from langgraph.types import Interrupt

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


async def _read_answer(prompt: str = "> ") -> str:
    """Read one clarification answer from the terminal without blocking the event loop.

    A daemon thread feeding an `asyncio.Future`, not `asyncio.to_thread`: the default
    executor's workers are NOT daemons, so the timeout fires on schedule but `asyncio.run()`
    then blocks at interpreter shutdown joining a worker still parked in `input()` — the run
    would print its cut-short report and hang at an already-dead `> ` prompt.

    The prompt goes to STDERR, not through `input`'s own argument, which writes it with no
    trailing newline and so put a pending `> ` on the same stdout line as the report path,
    breaking the frozen "path is the final line of stdout" contract.
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
            # `_answer_questions` turns "" into `_NO_ANSWER_GIVEN`, so the model is told the
            # question went unanswered and the run proceeds to a report like any other.
            answer = ""

        def _resolve() -> None:
            # The wall clock may already have cancelled this future — setting a result on a
            # done future raises `InvalidStateError`.
            if not future.done():
                future.set_result(answer)

        try:
            loop.call_soon_threadsafe(_resolve)
        except RuntimeError:
            # The loop is already closed — nothing left to resolve, and this must not raise
            # out of a daemon thread.
            pass

    threading.Thread(target=_worker, daemon=True).start()
    return await future


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
    """One activity line describing a researcher-dispatch proposal."""
    args = call.get("args") or {}
    return f'task(researcher): "{args.get("description", "")}"'


async def _answer_questions(interrupt: Interrupt, renderer: Renderer) -> list[dict[str, Any]]:
    """Render each pending `ask_user` question and collect one answer per action request.

    One decision per request, in the same order — the middleware raises `ValueError` on a
    count mismatch.
    """
    decisions: list[dict[str, Any]] = []
    for request in interrupt.value["action_requests"]:
        args = request.get("args", {})
        question = args.get("question") or request.get("description") or str(args)
        renderer.emit(Question(question))
        with renderer.suspend():
            answer = await _read_answer()
        # Best-effort + disclose: a bare Enter must not reach the model as an empty tool
        # result, which reads as "answered with nothing said" and hides the open ambiguity.
        decisions.append({"type": "respond", "message": answer.strip() or _NO_ANSWER_GIVEN})
    return decisions


async def main(argv: list[str] | None = None) -> int:
    """Run one research question end to end. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m harness", description="Answer one research question with cited sources."
    )
    parser.add_argument("question", help="The research question to answer.")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

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
    run_log = RunLog()
    # Same close/print/exit-1 shape as the preflight loop: `build_agent` resolves every
    # role through `build_chat_model`, so a missing or TODO role raises `ModelError` here —
    # unhandled it would escape as a traceback under the alternate screen (PR #18 review).
    try:
        agent = build_agent(config, registry, run_log)
    except ModelError as exc:
        renderer.close()
        print(f"error: {exc}", file=sys.stderr)
        return 1

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
    stream_input: Any = {"messages": [HumanMessage(content=args.question)]}

    last_todos: list[dict[str, Any]] | None = None
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
                                    renderer.emit(
                                        TodosUpdated(
                                            tuple(
                                                TodoItem(
                                                    content=todo["content"], status=todo["status"]
                                                )
                                                for todo in todos
                                            )
                                        )
                                    )
                                    last_todos = todos
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
                        resume={"decisions": await _answer_questions(interrupts[0], renderer)}
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
        question=args.question,
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
            )
        )
    finally:
        renderer.close()
    # Error prints belong AFTER close(): under `Live(screen=True)` anything written to the
    # terminal — stderr included, it shares the device — while the Live runs lands on the
    # alternate screen and is discarded with it. Down here the normal buffer is restored, so
    # the detail survives the run (Phase 4's "the no-report error message becomes visible").
    if path is not None:
        print(path)
    elif cut_short == "wall_clock":
        # The wall clock fired before a final answer existed (risk #2's blank/whitespace-answer
        # case lands here too, via `has_answer`).
        print("error: the wall clock expired before a final answer existed", file=sys.stderr)
    else:
        print(f"error: {cut_short_detail}", file=sys.stderr)
    return 0 if should_write_report else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
