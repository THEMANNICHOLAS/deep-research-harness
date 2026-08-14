"""CLI entrypoint: `python -m harness "<question>"`.

Loads config, preflights the `head` role before anything is spent (R6), builds the agent,
drives it while echoing todo-list progress (R10), and writes the report. The report path is
the final line of stdout — frozen, because R1 depends on it. Nothing may print after it.

The agent may ask clarifying questions via `ask_user` before researching (R2, D5): the run
interrupts, `main` prints each question, reads an answer, and resumes the same thread with
it as the tool's result.

Two ceilings bound the run (R7): a round cap carried as `recursion_limit` on the run config
(`AgentSettings.max_rounds`, in LangGraph supersteps — see the comment at `run_config`), and
a wall clock (`AgentSettings.wall_clock_seconds`) armed at the first `search_web`/
`fetch_pages` call and running continuously after that, including through a later
clarification wait. Hitting either bound, or any other mid-run failure, still writes a
report disclosing what happened (`RunOutcome.cut_short`).
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
from harness.paragraphs import split_paragraphs
from harness.report import CutShortReason, RunOutcome, format_todos, write_report
from harness.sources import SourceRegistry
from harness.verify import VerificationResult, verify_paragraphs

_EMPTY_USAGE: UsageMetadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

# What the model is told when the developer answers a clarifying question with nothing.
_NO_ANSWER_GIVEN = "(The developer gave no answer to this question.)"

# R2's pre-research window: the wall clock arms the first time one of these is proposed.
# Neither search nor fetch exports a tool-name constant, so these are the names from their
# own `@tool(...)` decorators.
_RESEARCH_TOOLS = frozenset({"search_web", "fetch_pages"})


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


def _proposes_research_tool_call(node_update: dict[str, Any]) -> bool:
    """Whether one node update carries an `AIMessage` proposing a research tool call.

    Arms the wall clock exactly once, at the first such call seen in the stream — see
    `_RESEARCH_TOOLS`.
    """
    for message in node_update.get("messages") or []:
        if isinstance(message, AIMessage):
            if any(call["name"] in _RESEARCH_TOOLS for call in message.tool_calls):
                return True
    return False


async def _answer_questions(interrupt: Interrupt) -> list[dict[str, Any]]:
    """Print each pending `ask_user` question and collect one answer per action request.

    One decision per request, in the same order — the middleware raises `ValueError` on a
    count mismatch.
    """
    decisions: list[dict[str, Any]] = []
    for request in interrupt.value["action_requests"]:
        args = request.get("args", {})
        question = args.get("question") or request.get("description") or str(args)
        print(question)
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

    try:
        await preflight(config, "head")
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Stamped before the agent can write anything: a cut-short report uses it to tell THIS
    # run's workspace notes from a previous run's leftovers (`report._notes_section`), and it
    # names this run's `<workspace_dir>/<run_id>/` directory, which keeps a previous run's
    # captures out of this run's verification.
    started_at = datetime.now()

    registry = SourceRegistry(run_id=started_at.strftime("%Y-%m-%d-%H%M%S"))
    agent = build_agent(config, registry)

    # One stable thread for the whole run: the checkpointer requires an id, and the
    # interrupt/resume loop below must keep resuming the SAME thread.
    thread_id = str(uuid4())
    run_config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        # `recursion_limit` is a `RunnableConfig` key, not a `create_deep_agent` kwarg, and
        # counts LangGraph supersteps, not research rounds: a round is a model call plus a
        # tool execution, plus the final tool-free answer turn — hence `* 2 + 1`.
        #
        # Deliberately approximate, and conservative: the compiled graph adds a fixed ~7-9
        # superstep middleware overhead, so the cap buys somewhat FEWER rounds than its name
        # suggests (default 20 → limit 41 → roughly 16). A tighter fit would hard-code one
        # deepagents version's node layout.
        #
        # It is also a PER-PASS bound. This config is reused on every `Command(resume=...)`
        # and langgraph recomputes `stop = resumed_step + recursion_limit + 1` per `astream`,
        # so each clarification resume grants a fresh allowance. Accepted: the wall clock is
        # the run-level bound once research starts, and the report says "per pass" rather
        # than implying a run total.
        "recursion_limit": config.agent.max_rounds * 2 + 1,
    }
    stream_input: Any = {"messages": [HumanMessage(content=args.question)]}

    last_todos: list[dict[str, Any]] | None = None
    final_state: dict[str, Any] | None = None
    clock_armed = False
    cut_short: CutShortReason | None = None
    cut_short_detail: str | None = None

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
                async for mode, chunk in agent.astream(
                    stream_input, config=run_config, stream_mode=["updates", "values"]
                ):
                    if mode == "updates":
                        for node_update in chunk.values():
                            # An interrupt arrives as `{"__interrupt__": (Interrupt(...),)}`,
                            # whose value is a tuple, not a dict — `.get` raises on it.
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
                        # Assigned HERE, inside the iteration: every cut-short path leaves
                        # this loop by exception, so an assignment after the `async for` never
                        # runs and the report would lose both the answer and the token usage
                        # on exactly the runs that need disclosing.
                        final_state = chunk

                interrupts = (pass_state or {}).get("__interrupt__")
                if not interrupts:
                    break
                # `interrupts[0]`, not all of them: the lead is a single agent node, so at
                # most one is ever pending, and `Command(resume=...)` delivers ONE value —
                # fanning several into one decisions list would mis-pair them.
                stream_input = Command(resume={"decisions": await _answer_questions(interrupts[0])})
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
        cut_short = "round_cap"
    except Exception as exc:  # noqa: BLE001 — never `BaseException`; Ctrl-C must still work
        cut_short = "error"
        cut_short_detail = f"{type(exc).__name__}: {exc}"

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
        print(
            f"verifying {len(paragraphs)} paragraph(s) against their cited sources...",
            file=sys.stderr,
        )
        try:
            verification = await verify_paragraphs(paragraphs, config, registry)
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
    )
    path = write_report(outcome, config)
    if cut_short == "error":
        # To stderr and before the path, so the path stays the LAST line of stdout.
        print(f"error: {cut_short_detail}", file=sys.stderr)
    print(path)
    return 1 if cut_short == "error" else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
