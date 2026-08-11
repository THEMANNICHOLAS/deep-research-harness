"""CLI entrypoint: `python -m harness "<question>"`.

Loads config, preflights the `head` role's reachability before anything is spent (R6),
builds the agent, drives it while echoing todo-list progress to the terminal (R10), and
writes the finished report. Prints the report's path as the final line of stdout — frozen,
because R1 depends on it. Nothing may print after it.

Before researching begins, the agent may ask one or more clarifying questions via
`ask_user` (Phase 4, R2, D5): the run interrupts, `main` prints each question, reads an
answer from the terminal, and resumes the same thread with the answer as the tool's
result. See the loop in `main` for the resume protocol.
"""

import argparse
import asyncio
import sys
from functools import reduce
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata, add_usage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, Interrupt

from harness.agent import build_agent
from harness.config import ConfigError, load_config
from harness.models import ModelError, preflight
from harness.report import RunOutcome, write_report
from harness.sources import SourceRegistry

_EMPTY_USAGE: UsageMetadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

# What the model is told when the developer answers a clarifying question with nothing.
_NO_ANSWER_GIVEN = "(The developer gave no answer to this question.)"


def _sum_usage(messages: list[BaseMessage]) -> UsageMetadata:
    """Sum `usage_metadata` across every `AIMessage` in the final state (finding 8)."""
    usages = [
        message.usage_metadata
        for message in messages
        if isinstance(message, AIMessage) and message.usage_metadata
    ]
    total = reduce(add_usage, usages, None)
    return total if total is not None else _EMPTY_USAGE


def _format_todos(todos: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{todo['status']}] {todo['content']}" for todo in todos)


async def _read_answer(prompt: str = "> ") -> str:
    """Read one clarification answer from the terminal without blocking the event loop.

    `asyncio.to_thread`, not a bare `input()`: Phase 5 has to run a wall clock ACROSS this
    wait (D5, R7), and a synchronous `input()` in the event loop would stop any asyncio
    timeout from ever firing.
    """
    return await asyncio.to_thread(input, prompt)


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

    registry = SourceRegistry()
    agent = build_agent(config, registry)

    # A stable thread_id for the whole run — the checkpointer requires one, and the
    # interrupt/resume loop below must keep resuming the SAME thread.
    thread_id = str(uuid4())
    run_config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    stream_input: Any = {"messages": [HumanMessage(content=args.question)]}

    last_todos: list[dict[str, Any]] | None = None
    final_state: dict[str, Any] | None = None
    while True:
        # Scoped to THIS pass, deliberately: interrupt detection below must never read a
        # previous pass's `__interrupt__`, or a resumed pass that emits no `values` chunk
        # would re-ask the same question forever (3F Minor). `final_state` still keeps the
        # newest state we actually saw, so the report is assembled from real data.
        pass_state: dict[str, Any] | None = None
        async for mode, chunk in agent.astream(
            stream_input, config=run_config, stream_mode=["updates", "values"]
        ):
            if mode == "updates":
                for node_update in chunk.values():
                    # An interrupt arrives as `{"__interrupt__": (Interrupt(...),)}`, whose
                    # value is a tuple, not a dict — `.get` on it raises AttributeError.
                    # This guard is a latent bug in the original loop that only becomes
                    # reachable now that interrupts exist.
                    if not node_update or not isinstance(node_update, dict):
                        continue
                    todos = node_update.get("todos")
                    if todos is not None and todos != last_todos:
                        print(_format_todos(todos))
                        last_todos = todos
            else:  # mode == "values"
                pass_state = chunk

        if pass_state is not None:
            final_state = pass_state

        interrupts = (pass_state or {}).get("__interrupt__")
        if not interrupts:
            break
        # `interrupts[0]`, not a flattening of all of them: the lead is a single agent
        # node, so at most one interrupt is ever pending, and `Command(resume=...)`
        # delivers ONE value — fanning several pending interrupts into one decisions list
        # would mis-pair them.
        stream_input = Command(resume={"decisions": await _answer_questions(interrupts[0])})

    messages: list[BaseMessage] = final_state["messages"] if final_state else []
    answer = str(messages[-1].content) if messages else ""
    usage = _sum_usage(messages)

    outcome = RunOutcome(question=args.question, answer=answer, registry=registry, usage=usage)
    path = write_report(outcome, config)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
