"""Behavioral tests for harness.agent (and the __main__ entrypoint that drives it).

Every test here builds a real deepagents-compiled graph via `build_agent` — nothing about
deepagents itself is mocked, only the model (`harness.agent.build_chat_model`, patched per
the module that imports it, never a network call) and, for the `__main__` tests, config
loading. Tool calls stay confined to filesystem/todo tools; nothing here drives
`fetch_pages`/`search_web`, which would touch a real browser or a real SearXNG instance.
"""

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Interrupt

import harness.__main__ as main_module
from harness.agent import build_agent
from harness.config import AgentSettings
from harness.report import (
    _CUT_SHORT_HEADING,
    _ERROR_TEXT,
    _NO_ANSWER_TEXT,
    _ROUND_CAP_TEXT,
    _WALL_CLOCK_TEXT,
)
from harness.sources import SourceRegistry
from tests.conftest import (
    drain_stdout,
    patch_model,
    patch_run,
    verify_reply,
    write_source_capture,
)


def _tools_by_name(graph):
    return graph.nodes["tools"].bound.tools_by_name


def _filesystem_backend(graph):
    """Recover the `FilesystemBackend` a compiled graph was built with.

    deepagents does not expose middleware instances on the compiled graph. Every
    filesystem tool's function closes over the owning `FilesystemMiddleware` (see the
    Phase 3 plan's settled finding 3, `filesystem.py:1713`), so `read_file`'s closure is
    the stable, documented way in to recover it for the `SandboxBackendProtocol` check.
    """
    read_tool = _tools_by_name(graph)["read_file"]
    for cell in read_tool.func.__closure__ or ():
        candidate = cell.cell_contents
        backend = getattr(candidate, "backend", None)
        if backend is not None:
            return backend
    raise AssertionError("could not recover the FilesystemMiddleware's backend from read_file")


async def test_build_agent_drives_research_using_the_configured_model(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    model = scripted_model(
        [
            AIMessage(
                content="final answer from the configured fake",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )
        ]
    )
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    # Behavioral, not structural: if build_agent ignored build_chat_model's return value
    # (or hardcoded a different model), this scripted content would never appear.
    assert result["messages"][-1].content == "final answer from the configured fake"


async def test_build_agent_delivers_the_rendered_prompt_and_the_question_to_the_model(
    make_config, monkeypatch, scripted_model
):
    """Regression guard for risk !#3 (a stale JSON tool-call instruction in the prompt).

    Nothing in the rest of the suite asserted anything about what actually reaches the
    model — `ScriptedChatModel._received_messages` (3F fix pass, Minor finding) records
    the real request, so this checks it directly instead of trusting the wiring.
    """
    config = make_config()
    model = scripted_model([AIMessage(content="done")])
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())
    await graph.ainvoke(
        {"messages": [HumanMessage(content="What is the boiling point of gallium?")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    assert model._received_messages, "the model was never called"
    first_request = model._received_messages[0]

    system_messages = [m for m in first_request if isinstance(m, SystemMessage)]
    assert system_messages, "no system message reached the model"
    # Distinctive text from harness/prompts/orchestrator.md, not from any fallback or
    # default LangChain/deepagents prompt — fails if build_agent stops rendering it.
    assert "lead researcher in a cited-sources research harness" in str(system_messages[0].content)

    human_messages = [m for m in first_request if isinstance(m, HumanMessage)]
    assert any("What is the boiling point of gallium?" in str(m.content) for m in human_messages), (
        "the research question never arrived as a human message"
    )


async def test_build_agent_exposes_the_harness_tools(make_config, monkeypatch, scripted_model):
    config = make_config()
    model = scripted_model([AIMessage(content="done")])
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())

    assert {"fetch_pages", "search_web"} <= _tools_by_name(graph).keys()


async def test_build_agent_disables_the_general_purpose_subagent(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    model = scripted_model([AIMessage(content="done")])
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())

    # Asserted on the outcome (the tool disappearing), never on the derived profile-key
    # string, so this fails loudly if deepagents changes its key derivation instead of
    # silently passing on a profile that no longer matched anything.
    assert "task" not in _tools_by_name(graph)


async def test_build_agent_includes_todo_list_middleware(make_config, monkeypatch, scripted_model):
    config = make_config()
    model = scripted_model([AIMessage(content="done")])
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())

    assert "write_todos" in _tools_by_name(graph)


async def test_execute_is_excluded_from_the_models_tool_schema(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    model = scripted_model([AIMessage(content="done")])
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())
    await graph.ainvoke(
        {"messages": [HumanMessage(content="hi")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    assert model._bound_tool_names, "bind_tools was never called — nothing was asserted"
    for offered in model._bound_tool_names:
        assert "execute" not in offered


def test_filesystem_backend_is_never_a_sandbox(make_config, monkeypatch, scripted_model):
    config = make_config()
    model = scripted_model([AIMessage(content="done")])
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())
    backend = _filesystem_backend(graph)

    assert not isinstance(backend, SandboxBackendProtocol)


async def test_writes_through_the_agent_land_under_the_workspace_dir(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": "notes.md", "content": "hello"},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())
    await graph.ainvoke(
        {"messages": [HumanMessage(content="write something")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    written = config.agent.workspace_dir / "notes.md"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "hello"


async def test_writes_cannot_escape_the_workspace_dir(make_config, monkeypatch, scripted_model):
    config = make_config()
    model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": "../escape.md", "content": "nope"},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="try to escape")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    escaped = config.agent.workspace_dir.parent / "escape.md"
    assert not escaped.exists()
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert any(getattr(m, "status", None) == "error" for m in tool_messages), tool_messages


async def test_compression_offloads_evicted_history_and_preserves_todos_state(
    make_config, monkeypatch, scripted_model
):
    """Cross the summarizer's trigger with real volume, then check real evidence.

    Two things verified empirically against the installed `deepagents`/`langchain` before
    writing this (Blocker 2 of the 3F fix pass):

    - `keep=("messages", 20)` vetoes summarization independently of `trigger` —
      `_find_safe_cutoff` returns 0 (no-op) whenever `len(messages) <= keep`. 14 tool
      rounds (28 messages) plus the plan round (2) plus the human message (1) is
      comfortably over 20, so this padding also crosses the message-count floor, not
      only the token trigger.
    - The deepagents `SummarizationMiddleware` (Blocker 1's fix) implements
      `wrap_model_call`/`awrap_model_call`, NOT the legacy `before_model`. It does NOT
      shrink the graph's own `state["messages"]` — it offloads the evicted messages to
      `backend` and rewrites only the NEXT model *request* to `[summary, *preserved]`.
      So the post-run message list is never shorter than what was scripted, and no
      summary message is ever added to `state["messages"]`; the state-shrinkage/summary-
      in-state assertions the old version of this test made cannot succeed under this
      middleware and were not testing anything real.

    The real, checkable evidence is therefore: (1) a later model *request* carries the
    middleware's own summarization `HumanMessage` (`additional_kwargs["lc_source"] ==
    "summarization"`) — produced by the middleware, not by anything the scripted model
    said, so this cannot pass by accident; and (2) the `[S1]` marker attached to an early
    finding, which that request no longer carries, is still recoverable from the
    backend's offload file under `<workspace_dir>/conversation_history/` — the strongest
    form of the D7 attribution-survival assertion per the fix plan.
    """
    config = make_config()
    padding = "x" * 60_000
    plan_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Investigate the topic", "status": "pending"}]},
                "id": "call_plan",
            }
        ],
    )
    tool_rounds = []
    for i in range(14):
        content = f"Finding: melting point noted [S1].\n{padding}" if i == 0 else padding
        tool_rounds.append(
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": f"note-{i}.md", "content": content},
                        "id": f"call_{i}",
                    }
                ],
            )
        )
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([plan_call, *tool_rounds, final])
    patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this at length")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    # Evidence 1: compression actually fired. Independent of anything the scripted model
    # emitted — the summarization HumanMessage is minted by the middleware itself.
    summarized_requests = [
        batch
        for batch in model._received_messages
        if any(m.additional_kwargs.get("lc_source") == "summarization" for m in batch)
    ]
    assert summarized_requests, (
        "no model request carried a summarization message — compression never fired, "
        "so this test proved nothing"
    )

    # Evidence 2: the evicted [S1] finding survives via the backend's offload file, even
    # though later requests to the model no longer carry it directly.
    history_dir = config.agent.workspace_dir / "conversation_history"
    offloaded_files = list(history_dir.glob("*.md"))
    assert offloaded_files, "compression fired but wrote no offload file under the backend"
    offloaded_text = "\n".join(f.read_text(encoding="utf-8") for f in offloaded_files)
    assert "[S1]" in offloaded_text

    # Finding 7: todos live in graph state, not the message list, so they cannot be
    # dropped by message compression — assert that rather than merely hoping it holds.
    assert result["todos"] == [{"content": "Investigate the topic", "status": "pending"}]


async def test_todo_updates_surface_at_the_terminal(
    make_config, monkeypatch, scripted_model, capsys
):
    config = make_config()
    ping = AIMessage(content="pong")  # consumed by preflight, never enters graph state
    plan_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Search for the answer", "status": "pending"}]},
                "id": "call_1",
            }
        ],
    )
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, plan_call, final])
    patch_run(monkeypatch, config, model)

    await main_module.main(["What is the capital of France?"])

    out = capsys.readouterr().out
    assert "Search for the answer" in out


async def test_run_outcome_records_token_usage_summed_with_reasoning_split(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    ping = AIMessage(content="pong")
    usage_a = {
        "input_tokens": 40,
        "output_tokens": 10,
        "total_tokens": 50,
        "output_token_details": {"reasoning": 4},
    }
    usage_b = {
        "input_tokens": 20,
        "output_tokens": 20,
        "total_tokens": 40,
        "output_token_details": {"reasoning": 18},
    }
    round_one = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Plan the research", "status": "pending"}]},
                "id": "call_1",
            }
        ],
        usage_metadata=usage_a,
    )
    final = AIMessage(content="Final answer [S1].", usage_metadata=usage_b)
    model = scripted_model([ping, round_one, final])
    patch_run(monkeypatch, config, model)

    captured = {}
    real_write_report = main_module.write_report

    def _spy(outcome, cfg):
        captured["outcome"] = outcome
        return real_write_report(outcome, cfg)

    monkeypatch.setattr(main_module, "write_report", _spy)

    await main_module.main(["question"])

    usage = captured["outcome"].usage
    # Summed across BOTH AIMessages in the final state, not just the last one — a stub
    # that read only the final message's usage_metadata would report 40/20/60 here, not
    # the true 60/30/90.
    assert usage["input_tokens"] == 60
    assert usage["output_tokens"] == 30
    assert usage["total_tokens"] == 90
    assert usage["output_token_details"]["reasoning"] == 22


async def test_main_prints_the_report_path_as_the_final_line_of_stdout(
    make_config, monkeypatch, scripted_model, capsys
):
    config = make_config()
    ping = AIMessage(content="pong")
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, final])
    patch_run(monkeypatch, config, model)

    await main_module.main(["a question with no tool calls"])

    _, lines = drain_stdout(capsys)
    assert lines, "main() printed nothing"
    printed_path = lines[-1].strip()
    assert printed_path.endswith(".md")
    assert Path(printed_path).exists()
    assert Path(printed_path).parent == config.agent.reports_dir


# --- Phase 5: round cap, wall clock, and cut-short reporting ---------------------------


def _install_slow_search(monkeypatch, delay_seconds: float) -> None:
    """Route `harness.tools.search`'s `httpx.AsyncClient` through a transport that sleeps.

    Same faking technique as `tests/test_search.py`'s `_install` — monkeypatch the class
    the module imports — not a new one; reproduced here (rather than imported) because
    that helper is private to its own test module.

    CALL THIS AFTER `scripted_model(...)`, never before. `harness.tools.search` does a
    plain `import httpx`, so this replaces the process-global `httpx.AsyncClient`, and
    `openai`'s client constructor rejects anything that is not an instance of whatever
    `httpx.AsyncClient` is bound to at that moment — including
    `langchain_openai`'s `_AsyncHttpxClientWrapper`, which subclasses the ORIGINAL class
    captured at import time. Building the model first means that check has already run.
    """
    real = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay_seconds)
        return httpx.Response(200, json={"query": "x", "results": []})

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("harness.tools.search.httpx.AsyncClient", factory)


def test_final_answer_skips_trailing_tool_output():
    """On every cut-short path the message list usually ends in tool traffic, not prose.
    Taking `messages[-1].content` verbatim would publish deepagents' internal tool output
    as the run's ANSWER to a non-technical reader (3F Major), so the last AIMessage that
    actually said something is what counts.
    """
    messages = [
        AIMessage(content="Acme is cheapest at $4.20/unit [S1]."),
        AIMessage(
            content="",
            tool_calls=[{"name": "write_todos", "args": {"todos": []}, "id": "c1"}],
        ),
        ToolMessage(content="Updated todo list to [...]", tool_call_id="c1"),
    ]

    assert main_module._final_answer(messages) == "Acme is cheapest at $4.20/unit [S1]."


def test_final_answer_is_empty_when_the_run_never_spoke():
    """A run cut short before any prose has no answer at all — `report.py` renders the
    empty case explicitly rather than showing an empty section.
    """
    messages = [
        AIMessage(
            content="",
            tool_calls=[{"name": "search_web", "args": {"query": "x"}, "id": "c1"}],
        ),
        ToolMessage(content="Some search results.", tool_call_id="c1"),
    ]

    assert main_module._final_answer(messages) == ""


def test_message_text_reads_block_style_content():
    """`AIMessage.content` is `str | list`, and `str(content)` on the list shape is a repr.

    A provider or model swap that returns content blocks would otherwise put
    `[{'type': 'text', 'text': '...'}]` under `## Answer` for a non-technical reader
    (PR #4 review, Minor). Non-text blocks (a `thinking` block, say) are dropped rather
    than rendered.
    """
    message = AIMessage(
        content=[
            {"type": "thinking", "thinking": "internal scratch that must not be published"},
            {"type": "text", "text": "Acme quoted $4.20/unit [S1]."},
        ]
    )

    assert main_module._message_text(message) == "Acme quoted $4.20/unit [S1]."
    assert main_module._final_answer([message]) == "Acme quoted $4.20/unit [S1]."


def test_message_text_still_reads_the_plain_string_shape():
    assert main_module._message_text(AIMessage(content="  Plain prose.  ")) == "Plain prose."


async def test_read_answer_resolves_instead_of_hanging_when_stdin_is_closed(monkeypatch):
    """A dead stdin must not strand the run on a future nothing will ever complete.

    `input()` raising `EOFError` used to kill the worker thread BEFORE `_resolve` was
    scheduled, so `await future` never returned (PR #4 review, Major). The wall clock is
    still disarmed for a pre-research question, so nothing else would ever rescue it.
    This test hangs forever against the old code rather than failing, which is why it
    carries its own timeout.
    """

    def fake_input(prompt: str = "") -> str:
        raise EOFError("EOF when reading a line")

    monkeypatch.setattr("builtins.input", fake_input)

    answer = await asyncio.wait_for(main_module._read_answer(), timeout=5)

    assert answer == ""


async def test_read_answer_resolves_when_stdin_raises_oserror(monkeypatch):
    """Same guard, for a detached-stdin `OSError` rather than a clean EOF."""

    def fake_input(prompt: str = "") -> str:
        raise OSError("Bad file descriptor")

    monkeypatch.setattr("builtins.input", fake_input)

    assert await asyncio.wait_for(main_module._read_answer(), timeout=5) == ""


async def test_the_clarification_prompt_never_reaches_stdout(monkeypatch, capsys):
    """The report path is the final line of STDOUT — frozen, because R1 depends on it.

    `input(prompt)` writes the prompt with no trailing newline, so the path printed at the
    end of `main` landed on the same line as a pending `> ` (PR #4 review, Major). The
    prompt belongs on stderr with the rest of the terminal chatter.
    """
    monkeypatch.setattr("builtins.input", lambda: "the metal")

    answer = await main_module._read_answer("> ")

    captured = capsys.readouterr()
    assert answer == "the metal"
    assert captured.out == "", f"the prompt reached stdout: {captured.out!r}"
    assert "> " in captured.err


async def test_read_answer_runs_on_a_daemon_thread(monkeypatch):
    """A non-daemon worker is joined at interpreter shutdown, so the process hangs after
    the wall clock has already fired and written its report. (Measured: a probe using
    `asyncio.to_thread` under a 1s timeout returned at 30s; the daemon-thread version
    returned at 1.0s.) This is the only cheap in-process assertion of that property.
    """
    recorded: dict[str, bool] = {}

    def fake_input(prompt: str = "") -> str:
        recorded["daemon"] = threading.current_thread().daemon
        return "answer"

    monkeypatch.setattr("builtins.input", fake_input)

    await main_module._read_answer()

    assert recorded.get("daemon") is True


async def test_read_answer_returns_what_was_typed(monkeypatch):
    """Regression: swapping `input()` for a thread-based read must not mangle the answer,
    and the normalization `_answer_questions` applies (Phase 4) must still see it whole.
    """
    monkeypatch.setattr("builtins.input", lambda prompt="": "  Yes, region EU-West  ")
    interrupt = Interrupt(value={"action_requests": [{"args": {"question": "Which region?"}}]})

    decisions = await main_module._answer_questions(interrupt)

    assert decisions == [{"type": "respond", "message": "Yes, region EU-West"}]


async def test_main_cuts_the_run_short_at_the_round_cap(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """`max_rounds=1` with a model that never stops proposing tool calls forces
    `recursion_limit` to end the run instead of the graph terminating on its own — proving
    the cap, not an exhausted script, is what ends it. `ScriptedChatModel` raises
    `IndexError` once its script runs out (see tests/conftest.py), so far more responses
    are scripted than the cap could ever consume.
    """
    agent = AgentSettings(
        max_rounds=1, workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
    )
    config = make_config(agent=agent)
    ping = AIMessage(content="pong")
    keep_going = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Keep researching", "status": "in_progress"}]},
                "id": "call",
            }
        ],
    )
    model = scripted_model([ping, *([keep_going] * 20)])
    patch_run(monkeypatch, config, model)

    exit_code = await main_module.main(["question that never settles"])

    out, lines = drain_stdout(capsys)
    assert exit_code == 0
    assert lines, "main() printed no report path"
    report_path = Path(lines[-1].strip())
    assert report_path.exists()
    # Proves the cap ended the run rather than the 20-item script running out: a run that
    # actually consumed the whole script would have driven far more model calls.
    assert len(model._received_messages) < 5
    body = report_path.read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    # Names the ROUND CAP specifically, not just "something cut this short": without this,
    # swapping the `GraphRecursionError` and `TimeoutError` labels in __main__'s except
    # clauses would keep every cut-short test green.
    assert _ROUND_CAP_TEXT in body
    assert _WALL_CLOCK_TEXT not in body


@pytest.mark.parametrize(("max_rounds", "expect_cut_short"), [(3, True), (4, False)])
async def test_max_rounds_scales_the_recursion_limit(
    make_config, monkeypatch, scripted_model, tmp_path, capsys, max_rounds, expect_cut_short
):
    """Pins the `max_rounds * 2 + 1` mapping at its measured boundary, which the round-cap
    test above cannot — that one passes under ANY mapping small enough to trip.

    A run doing exactly one tool round is cut short at `max_rounds=3` (limit 7) and
    completes at `max_rounds=4` (limit 9). Measured against the installed deepagents, not
    derived: the graph carries a fixed ~7-9 superstep middleware overhead on top of the
    ~2 supersteps a round actually costs, so the mapping's arithmetic is looser than its
    name (see `## Discoveries` 2026-08-11 — Phase 5). Passing `max_rounds` straight
    through as `recursion_limit` would cut BOTH of these short, so this pair is what
    distinguishes the two mappings.
    """
    agent = AgentSettings(
        max_rounds=max_rounds,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)
    ping = AIMessage(content="pong")
    one_round = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Look it up", "status": "completed"}]},
                "id": "call_1",
            }
        ],
    )
    final = AIMessage(
        content="Answered after exactly one tool round.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, one_round, final])
    patch_run(monkeypatch, config, model)

    exit_code = await main_module.main(["a question needing one round"])

    out, lines = drain_stdout(capsys)
    assert exit_code == 0
    body = Path(lines[-1].strip()).read_text(encoding="utf-8")
    assert (_CUT_SHORT_HEADING in body) is expect_cut_short
    if not expect_cut_short:
        assert "Answered after exactly one tool round." in body


async def test_a_cut_short_report_carries_the_todos_seen_during_the_run(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """`todos=last_todos or []` is the only route from the streamed todo state into the
    report (D9's "name the planned steps not yet done"). Without this, passing `[]` there
    would stay green — `test_report.py` only proves the RENDER, never the wiring.
    """
    # max_rounds=2 (limit 5), measured: far enough for the write_todos round to land in
    # the stream, still short of the ~9 a full run needs — so the report is cut short AND
    # has todos to name. At max_rounds=1 the run dies before any todo update is emitted.
    agent = AgentSettings(
        max_rounds=2, workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
    )
    config = make_config(agent=agent)
    ping = AIMessage(content="pong")
    keep_going = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Chase the pricing page", "status": "pending"}]},
                "id": "call",
            }
        ],
    )
    model = scripted_model([ping, *([keep_going] * 20)])
    patch_run(monkeypatch, config, model)

    await main_module.main(["question that never settles"])

    out, lines = drain_stdout(capsys)
    body = Path(lines[-1].strip()).read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    assert "Chase the pricing page" in body
    # The last message on a cut-short run is the write_todos ToolMessage. Taking
    # `messages[-1].content` verbatim would publish deepagents' "Updated todo list to
    # [...]" as the run's ANSWER to a non-technical reader (3F Major).
    assert _NO_ANSWER_TEXT in body
    assert "Updated todo list" not in body


async def test_main_cuts_the_run_short_when_the_wall_clock_expires(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """A `final` response is scripted AFTER the slow search — reachable only if nothing
    cuts the run short — so an unimplemented clock would let this run complete normally
    (no exception, no cut-short disclosure) rather than coincidentally raising once the
    3-item script exhausts. The elapsed-time assertion is what actually pins the timeout:
    without it, a broad "catch anything, call it wall_clock" shortcut that let the full
    3-second sleep run to completion could slip past a body-content check alone.
    """
    agent = AgentSettings(
        wall_clock_seconds=1,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)

    ping = AIMessage(content="pong")
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, search_call, final])
    patch_run(monkeypatch, config, model)
    # After the model is built — see `_install_slow_search`'s docstring.
    _install_slow_search(monkeypatch, delay_seconds=3)

    started = time.monotonic()
    exit_code = await main_module.main(["a question that starts researching"])
    elapsed = time.monotonic() - started

    out, lines = drain_stdout(capsys)
    assert exit_code == 0
    assert lines, "main() printed no report path"
    report_path = Path(lines[-1].strip())
    body = report_path.read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    # Names the WALL CLOCK specifically — see the round-cap test for why the generic
    # heading assertion alone is not enough.
    assert _WALL_CLOCK_TEXT in body
    assert _ROUND_CAP_TEXT not in body
    # Well under the full 3s sleep: proves the run was actually cut off near the 1s bound,
    # not merely completed and then happened to be mislabeled.
    assert elapsed < 2.5, f"run took {elapsed}s — the wall clock did not actually fire early"


async def test_a_pre_research_clarification_does_not_start_the_wall_clock(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """The clock arms at the first `search_web`/`fetch_pages` call, not at process start
    (`## Reconciliations` 2026-08-10 — Phase 5). A pre-research `ask_user` wait of any
    length must not trip it — proven by making the wait (2s) longer than the configured
    wall clock (1s) and asserting the run still finishes clean. Paired with the mid-run
    test below; both must exist or neither pins where the clock starts.
    """
    agent = AgentSettings(
        wall_clock_seconds=1,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)
    ping = AIMessage(content="pong")
    ask = AIMessage(
        content="",
        tool_calls=[{"name": "ask_user", "args": {"question": "Which scope?"}, "id": "call_1"}],
    )
    final = AIMessage(
        content="Final answer, no research needed.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, ask, final])
    patch_run(monkeypatch, config, model)

    async def _slow_answer(prompt: str = "> ") -> str:
        await asyncio.sleep(2)
        return "Whole company."

    monkeypatch.setattr(main_module, "_read_answer", _slow_answer)

    exit_code = await main_module.main(["Should we expand?"])

    out, lines = drain_stdout(capsys)
    assert exit_code == 0
    assert lines, "main() printed no report path"
    report_path = Path(lines[-1].strip())
    body = report_path.read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING not in body


async def test_a_mid_run_clarification_is_bounded_by_the_wall_clock(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """Pairs with the pre-research test above: once research has begun the clock is
    running and is not paused for an interrupt, so an unanswered mid-run ask still ends
    the run at the bound (`## Reconciliations` 2026-08-10 — Phase 5, consequence 3).

    A `final` response is scripted for AFTER the resume — reachable only if the wait is
    never cut short — so an unimplemented clock would let this run complete normally
    rather than coincidentally raising once the 4-item script exhausts (same reasoning as
    the wall-clock-expiry test above). The elapsed-time assertion is what actually pins
    the timeout to the wait itself.
    """
    agent = AgentSettings(
        wall_clock_seconds=1,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)

    ping = AIMessage(content="pong")
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    ask = AIMessage(
        content="",
        tool_calls=[
            {"name": "ask_user", "args": {"question": "Narrower scope?"}, "id": "call_ask"}
        ],
    )
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, search_call, ask, final])
    patch_run(monkeypatch, config, model)
    # After the model is built — see `_install_slow_search`'s docstring.
    _install_slow_search(monkeypatch, delay_seconds=0.1)

    async def _slow_answer(prompt: str = "> ") -> str:
        await asyncio.sleep(3)
        return "Narrower."

    monkeypatch.setattr(main_module, "_read_answer", _slow_answer)

    started = time.monotonic()
    exit_code = await main_module.main(["Research widgets"])
    elapsed = time.monotonic() - started

    out, lines = drain_stdout(capsys)
    assert exit_code == 0
    assert lines, "main() printed no report path"
    report_path = Path(lines[-1].strip())
    body = report_path.read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    assert _WALL_CLOCK_TEXT in body
    # Well under the full 3s wait: proves the wait was actually cut off near the 1s
    # remaining on the clock, not merely completed and then mislabeled.
    assert elapsed < 2.5, f"run took {elapsed}s — the wall clock did not actually fire early"


async def test_main_writes_a_cut_short_report_when_the_run_dies_mid_flight(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """The model's script runs out (`ScriptedChatModel` raises `IndexError` — see
    tests/conftest.py) right after one real round, standing in for a genuine mid-run
    failure. `main` must turn ANY such exception into a written report and exit 1, never
    let a traceback escape.
    """
    config = make_config(
        agent=AgentSettings(workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports")
    )
    ping = AIMessage(content="pong")
    plan_call = AIMessage(
        # Carries prose AND a tool call: the last message in the final state is the
        # write_todos ToolMessage, so this is the only place `_final_answer` has to walk
        # BACK past tool traffic to find what the model actually said. Taking
        # `messages[-1].content` would publish "Updated todo list to [...]" as the answer.
        content="Partial finding: Acme quoted $4.20/unit.",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Investigate", "status": "in_progress"}]},
                "id": "call_1",
            }
        ],
        usage_metadata={"input_tokens": 41, "output_tokens": 7, "total_tokens": 48},
    )
    model = scripted_model([ping, plan_call])  # no third response — the run dies here
    patch_run(monkeypatch, config, model)

    exit_code = await main_module.main(["question that never gets an answer"])

    out, err = capsys.readouterr()
    assert exit_code == 1
    assert any(line.startswith("error:") for line in err.splitlines()), err
    assert "Traceback" not in err
    lines = [line for line in out.splitlines() if line.strip()]  # `out` already drained above
    assert lines, "no report path was printed even though the run died mid-flight"
    report_path = Path(lines[-1].strip())
    assert report_path.exists()
    body = report_path.read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    assert _ERROR_TEXT in body
    assert "IndexError" in body
    # Pins the `_final_answer` WIRING, not just the helper: what the model actually said
    # survives, and the trailing tool output does not become the answer (3F Major).
    assert "Partial finding: Acme quoted $4.20/unit." in body
    assert "Updated todo list" not in body
    # The token cost of a run that died still has to be recorded (R7's baseline). Both this
    # and the answer above come from the run state, which is only captured DURING the
    # stream — a cut-short run leaves the loop by exception, so anything gathered after it
    # is never gathered at all.
    assert "- Total tokens: 48" in body


async def test_a_run_inside_both_bounds_reports_no_cut_short(
    make_config, monkeypatch, scripted_model, capsys
):
    config = make_config()
    ping = AIMessage(content="pong")
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ping, final])
    patch_run(monkeypatch, config, model)

    exit_code = await main_module.main(["a simple question"])

    out, lines = drain_stdout(capsys)
    assert exit_code == 0
    assert lines, "main() printed no report path"
    report_path = Path(lines[-1].strip())
    body = report_path.read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING not in body


# --- PR #4 review: the Phase 5 x Phase 6 seam ------------------------------------------


async def test_a_cut_short_run_still_checks_a_claim_against_its_captured_source(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """A round-capped run whose partial answer cites a real, captured source.

    This is the seam nothing covered (PR #4 review, Major). Every existing cut-short test
    scripts an answer with no `[Sn]` marker, or no answer at all, so `verify_claims` only
    ever took its trivial "no marker → no model call" path; the suite asserted cut-short
    headings and bound text and *looked* like it covered the interaction. It could not
    cover it, because `harness.verify` holds its own `build_chat_model` binding that no
    test patched — reaching the model-call branch would have dialed the fake endpoint for
    real. `patch_run` now patches that binding too, which is what makes this possible.

    The verification model is scripted SEPARATELY from the run model: how many rounds the
    cap allows is a langgraph implementation detail, so a shared script would make the
    verify reply's index depend on it.
    """
    agent = AgentSettings(
        max_rounds=2, workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
    )
    config = make_config(agent=agent)

    # `main` builds the registry itself, so a pre-populated one is injected in its place —
    # standing in for the `fetch_pages` calls a real run would have made before the cap.
    registry = SourceRegistry(run_id="2020-01-01-000000")
    source_id = registry.add("https://example.test/pricing", title="Pricing")
    write_source_capture(config, registry, source_id, "Acme lists $5.10 per unit.")
    monkeypatch.setattr(main_module, "SourceRegistry", lambda run_id: registry)

    ping = AIMessage(content="pong")
    partial = AIMessage(
        content=f"Acme quoted $4.20 per unit [{source_id}].",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Confirm the quote", "status": "pending"}]},
                "id": "call_1",
            }
        ],
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    keep_going = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_todos",
                "args": {"todos": [{"content": "Confirm the quote", "status": "pending"}]},
                "id": "call_n",
            }
        ],
    )
    model = scripted_model([ping, partial, *([keep_going] * 20)])
    patch_run(monkeypatch, config, model)

    verify_model = scripted_model([verify_reply("unsupported", "The capture reads $5.10.")])
    monkeypatch.setattr("harness.verify.build_chat_model", lambda cfg, role: verify_model)

    await main_module.main(["what does Acme charge?"])

    _, lines = drain_stdout(capsys)
    body = Path(lines[-1].strip()).read_text(encoding="utf-8")

    assert _CUT_SHORT_HEADING in body, "this run was supposed to hit the round cap"
    # The check actually ran on the cut-short path — not skipped, not defaulted.
    assert verify_model._call_count == 1
    assert f"**[unsupported — {source_id}]**" in body
    # And R1's citation resolution still happened on the same partial answer.
    assert "https://example.test/pricing" in body


async def test_a_clarifying_question_can_arrive_without_a_question_argument(
    make_config, monkeypatch, capsys
):
    """`args["question"] or description or str(args)` — only the first branch was exercised.

    Nothing guarantees deepagents keeps putting the prompt under `args`, and the fallbacks
    are all that stand between a schema change and an empty prompt at the terminal
    (PR #4 review, nit). Driven through `_answer_questions` directly, since an action
    request that omits `args["question"]` is a shape no real model can be scripted into.
    """

    async def _record(prompt: str = "> ") -> str:
        return "answered"

    monkeypatch.setattr(main_module, "_read_answer", _record)

    interrupt = Interrupt(
        value={
            "action_requests": [
                {"name": "ask_user", "args": {}, "description": "Metal or album?"},
                {"name": "ask_user", "args": {"topic": "isotope"}},
            ]
        }
    )

    decisions = await main_module._answer_questions(interrupt)

    out, _ = drain_stdout(capsys)
    asked = [line for line in out.splitlines() if line.strip()]
    assert asked[0] == "Metal or album?", "the description fallback never fired"
    assert "isotope" in asked[1], "the str(args) last resort never fired"
    assert [d["message"] for d in decisions] == ["answered", "answered"]
