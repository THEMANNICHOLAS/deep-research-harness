"""CLI entrypoint: `python -m harness "<question>"`.

Loads config, preflights the `head` role's reachability before anything is spent (R6),
builds the agent, drives it while echoing todo-list progress to the terminal (R10), and
writes the finished report. Prints the report's path as the final line of stdout — frozen,
because R1 depends on it. Nothing may print after it.

Before researching begins, the agent may ask one or more clarifying questions via
`ask_user` (Phase 4, R2, D5): the run interrupts, `main` prints each question, reads an
answer from the terminal, and resumes the same thread with the answer as the tool's
result. See the loop in `main` for the resume protocol.

The run is bounded by two ceilings (Phase 5, R7): a round cap, carried as `recursion_limit`
on the run config (`AgentSettings.max_rounds`, mapped to LangGraph supersteps — see the
comment at `run_config`), and a wall clock (`AgentSettings.wall_clock_seconds`) armed at the
first `search_web`/`fetch_pages` call and running continuously after that, including
through any later clarification wait. Hitting either bound, or any other mid-run failure,
still writes a report disclosing what happened (`RunOutcome.cut_short`) rather than losing
the run — see `## Reconciliations` 2026-08-10 — Phase 5 in
`docs/plans/PLAN-research-loop.md` for the reasoning behind both bounds' exact placement.
"""

import argparse
import asyncio
import sys
import threading
from datetime import datetime
from functools import reduce
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata, add_usage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.types import Command, Interrupt

from harness.agent import build_agent
from harness.config import ConfigError, load_config
from harness.models import ModelError, preflight
from harness.report import CutShortReason, RunOutcome, format_todos, write_report
from harness.sources import SourceRegistry
from harness.verify import VerificationResult, extract_claims, verify_claims

_EMPTY_USAGE: UsageMetadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

# What the model is told when the developer answers a clarifying question with nothing.
_NO_ANSWER_GIVEN = "(The developer gave no answer to this question.)"

# The observable form of R2's "pre-research window" (`## Reconciliations` 2026-08-10 —
# Phase 5): the wall clock arms the first time one of these is proposed. Neither
# `harness/tools/search.py` nor `harness/tools/fetch.py` exports a tool-name constant (only
# `harness/tools/ask_user.py` does), so these are the tool names themselves, matching the
# `@tool(...)` decorators in those two modules.
_RESEARCH_TOOLS = frozenset({"search_web", "fetch_pages"})


def _sum_usage(messages: list[BaseMessage]) -> UsageMetadata:
    """Sum `usage_metadata` across every `AIMessage` in the final state (finding 8)."""
    usages = [
        message.usage_metadata
        for message in messages
        if isinstance(message, AIMessage) and message.usage_metadata
    ]
    total = reduce(add_usage, usages, None)
    return total if total is not None else _EMPTY_USAGE


def _final_answer(messages: list[BaseMessage]) -> str:
    """The last `AIMessage` carrying real prose, or `""` if the run never produced one.

    NOT `messages[-1].content`: every cut-short path lands here too, and there the last
    message is usually a `ToolMessage` or a content-less tool-call `AIMessage` — which
    would put raw tool output ("Updated todo list to [...]") under `## Answer` in front of
    a non-technical reader (3F Major). `report.py` renders the empty case explicitly.
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = str(message.content).strip()
            if content:
                return content
    return ""


async def _read_answer(prompt: str = "> ") -> str:
    """Read one clarification answer from the terminal without blocking the event loop.

    A daemon thread feeding an `asyncio.Future`, not `asyncio.to_thread` (Phase 5
    Discovery, 2026-08-10): `asyncio.to_thread` runs on the default executor, whose worker
    threads are NOT daemons — probed before any code was written, `asyncio.wait_for`
    around it fires its timeout on schedule, but `asyncio.run()` then blocks at interpreter
    shutdown joining that non-daemon worker (the probe returned at 30s against a 1s
    timeout). With a real, still-blocked `input()` that join is unbounded, so the run would
    print its cut-short report and then hang at an already-dead `> ` prompt. The same probe
    on a daemon thread feeding a future returned at 1.0s, which is what this does.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def _worker() -> None:
        answer = input(prompt)

        def _resolve() -> None:
            # The wall clock may have already cancelled/timed out this future by the time
            # the terminal read returns — setting a result on a done future raises
            # `InvalidStateError`.
            if not future.done():
                future.set_result(answer)

        try:
            loop.call_soon_threadsafe(_resolve)
        except RuntimeError:
            # The event loop is already closed (interpreter shutdown) — nothing left to
            # resolve, and this must not raise out of a daemon thread.
            pass

    threading.Thread(target=_worker, daemon=True).start()
    return await future


def _proposes_research_tool_call(node_update: dict[str, Any]) -> bool:
    """Whether one node update carries an `AIMessage` proposing a research tool call.

    Used to arm the wall clock exactly once, at the first `search_web`/`fetch_pages` call
    observed in the stream — see `_RESEARCH_TOOLS` and `## Reconciliations` 2026-08-10 —
    Phase 5.
    """
    for message in node_update.get("messages") or []:
        if isinstance(message, AIMessage):
            if any(call["name"] in _RESEARCH_TOOLS for call in message.tool_calls):
                return True
    return False


async def _answer_questions(interrupt: Interrupt) -> list[dict[str, Any]]:
    """Print each pending `ask_user` question and collect one answer per action request.

    One decision per action request, in the same order — the middleware raises
    `ValueError` if the count returned does not match the count requested.
    """
    decisions: list[dict[str, Any]] = []
    for request in interrupt.value["action_requests"]:
        args = request.get("args", {})
        question = args.get("question") or request.get("description") or str(args)
        print(question)
        answer = await _read_answer()
        # Best-effort + disclose: a bare Enter must not reach the model as a silent empty
        # tool result, which reads as "answered with nothing said". Say so explicitly
        # instead, so the model knows the ambiguity is still open (3F Minor).
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

    try:
        await preflight(config, "head")
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Stamped before the agent can write anything, so a cut-short report can tell THIS
    # run's workspace notes from a previous run's leftovers — see `report._notes_section`.
    # The same stamp also names this run's `sources/<run_id>/` directory (`SourceRegistry`
    # below), which is what keeps a previous run's captures out of this run's verification
    # (see the plan's `## Reconciliations` 2026-08-12 — Phase 6).
    started_at = datetime.now()

    registry = SourceRegistry(run_id=started_at.strftime("%Y-%m-%d-%H%M%S"))
    agent = build_agent(config, registry)

    # A stable thread_id for the whole run — the checkpointer requires one, and the
    # interrupt/resume loop below must keep resuming the SAME thread.
    thread_id = str(uuid4())
    run_config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        # `recursion_limit` is a `RunnableConfig` key, not a `create_deep_agent` kwarg
        # (`## Reconciliations` 2026-08-10 — Phase 5), and counts LangGraph supersteps, not
        # research rounds: a round is a model call plus a tool execution (2 supersteps),
        # plus the final tool-free answer turn — hence `* 2 + 1`.
        #
        # That arithmetic is deliberately approximate, and the direction is conservative.
        # Measured against the installed deepagents (`## Discoveries` 2026-08-12 — Phase 5):
        # the compiled graph carries a fixed ~7-9 superstep middleware overhead on top of
        # the ~2 per round, so the cap buys somewhat FEWER rounds than its name suggests
        # (the default 20 → limit 41 → roughly 16). Bounding a run early is the job; a
        # tighter fit would hard-code one deepagents version's node layout.
        "recursion_limit": config.agent.max_rounds * 2 + 1,
    }
    stream_input: Any = {"messages": [HumanMessage(content=args.question)]}

    last_todos: list[dict[str, Any]] | None = None
    final_state: dict[str, Any] | None = None
    clock_armed = False
    cut_short: CutShortReason | None = None
    cut_short_detail: str | None = None

    try:
        # `asyncio.timeout(None)` starts disarmed — no clarifying wait before the first
        # research tool call is ever bounded (`## Reconciliations` 2026-08-10 — Phase 5).
        # This shape, not `asyncio.wait_for` around a pre-sized coroutine, because the
        # deadline isn't known until mid-stream, and it has to span both the `astream`
        # iteration and the `_read_answer` await inside the same loop.
        async with asyncio.timeout(None) as clock:
            while True:
                # Scoped to THIS pass, deliberately: interrupt detection below must never
                # read a previous pass's `__interrupt__`, or a resumed pass that emits no
                # `values` chunk would re-ask the same question forever (3F Minor).
                # `final_state` still keeps the newest state we actually saw, so the report
                # is assembled from real data.
                pass_state: dict[str, Any] | None = None
                async for mode, chunk in agent.astream(
                    stream_input, config=run_config, stream_mode=["updates", "values"]
                ):
                    if mode == "updates":
                        for node_update in chunk.values():
                            # An interrupt arrives as `{"__interrupt__": (Interrupt(...),)}`,
                            # whose value is a tuple, not a dict — `.get` on it raises
                            # AttributeError. This guard is a latent bug in the original
                            # loop that only becomes reachable now that interrupts exist.
                            if not node_update or not isinstance(node_update, dict):
                                continue
                            todos = node_update.get("todos")
                            if todos is not None and todos != last_todos:
                                print(format_todos(todos))
                                last_todos = todos
                            if not clock_armed and _proposes_research_tool_call(node_update):
                                clock.reschedule(
                                    asyncio.get_running_loop().time()
                                    + config.agent.wall_clock_seconds
                                )
                                clock_armed = True
                    else:  # mode == "values"
                        pass_state = chunk
                        # Updated HERE, inside the iteration, not after the `async for`
                        # completes. Every cut-short path leaves this loop by exception, so
                        # an assignment placed after it never runs — and the report would
                        # then be built from `final_state is None`, losing both the answer
                        # and the token usage on exactly the runs that need disclosing.
                        final_state = chunk

                interrupts = (pass_state or {}).get("__interrupt__")
                if not interrupts:
                    break
                # `interrupts[0]`, not a flattening of all of them: the lead is a single
                # agent node, so at most one interrupt is ever pending, and
                # `Command(resume=...)` delivers ONE value — fanning several pending
                # interrupts into one decisions list would mis-pair them.
                stream_input = Command(resume={"decisions": await _answer_questions(interrupts[0])})
    except TimeoutError as exc:
        # `clock.expired()`, not a bare `except TimeoutError`: a `TimeoutError` raised by
        # something INSIDE the run (an `asyncio.wait_for` in a tool, say) would otherwise
        # be reported to the reader as "the wall clock stopped this run", which is simply
        # untrue and hides the real failure (3F Minor).
        if clock.expired():
            cut_short = "wall_clock"
        else:
            cut_short = "error"
            cut_short_detail = f"{type(exc).__name__}: {exc}"
    except GraphRecursionError:  # must precede `Exception` — it subclasses RuntimeError
        cut_short = "round_cap"
    except Exception as exc:  # noqa: BLE001 — never `BaseException`; Ctrl-C must still work
        cut_short = "error"
        cut_short_detail = f"{type(exc).__name__}: {exc}"

    messages: list[BaseMessage] = final_state["messages"] if final_state else []
    usage = _sum_usage(messages)

    answer = _final_answer(messages)
    verification = None
    if cut_short == "error":
        # A model-outage death means the head model is near-certainly still unreachable —
        # running one verification call per claim, each carrying Phase 1's bounded
        # backoff, would burn minutes on calls that are near-certain to fail before the
        # report is even written. Skipping is disclosed, never silent (3F fix pass,
        # Minor finding) — `## Gaps and disclosures` states it via `check_failures`.
        verification = VerificationResult(
            check_failures=[
                "verification skipped: the run ended in an error, so claims were not checked"
            ]
        )
    elif answer:
        claims = extract_claims(answer)
        # Computed once and reused below — `verify_claims` no longer recomputes it (3F fix
        # pass, simplification). Worded as "claim(s)", not a call count: the actual number
        # of model calls is per (claim x cited source), which this count does not claim
        # to be.
        print(f"verifying {len(claims)} claim(s) against their cited sources...", file=sys.stderr)
        try:
            verification = await verify_claims(answer, config, registry, claims=claims)
        except Exception as exc:  # noqa: BLE001
            # Best-effort + disclose: a pass that fails wholesale is reported IN the
            # report, never silently dropped. Per-claim failures are handled inside the
            # pass itself.
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
        verification=verification,
    )
    path = write_report(outcome, config)
    if cut_short == "error":
        # Printed to stderr, and before the path, so the path stays the LAST line of
        # stdout regardless of what happened.
        print(f"error: {cut_short_detail}", file=sys.stderr)
    print(path)
    return 1 if cut_short == "error" else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
