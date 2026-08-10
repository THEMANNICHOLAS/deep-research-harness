"""CLI entrypoint: `python -m harness "<question>"`.

Loads config, preflights the `head` role's reachability before anything is spent (R6),
builds the agent, drives it while echoing todo-list progress to the terminal (R10), and
writes the finished report. Prints the report's path as the final line of stdout — frozen,
because R1 depends on it. Nothing may print after it.
"""

import argparse
import asyncio
import sys
from functools import reduce
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata, add_usage

from harness.agent import build_agent
from harness.config import ConfigError, load_config
from harness.models import ModelError, preflight
from harness.report import RunOutcome, write_report
from harness.sources import SourceRegistry

_EMPTY_USAGE: UsageMetadata = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


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

    last_todos: list[dict[str, Any]] | None = None
    final_state: dict[str, Any] | None = None
    async for mode, chunk in agent.astream(
        {"messages": [HumanMessage(content=args.question)]},
        stream_mode=["updates", "values"],
    ):
        if mode == "updates":
            for node_update in chunk.values():
                if not node_update:
                    continue
                todos = node_update.get("todos")
                if todos is not None and todos != last_todos:
                    print(_format_todos(todos))
                    last_todos = todos
        else:  # mode == "values"
            final_state = chunk

    messages: list[BaseMessage] = final_state["messages"] if final_state else []
    answer = str(messages[-1].content) if messages else ""
    usage = _sum_usage(messages)

    outcome = RunOutcome(question=args.question, answer=answer, registry=registry, usage=usage)
    path = write_report(outcome, config)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
