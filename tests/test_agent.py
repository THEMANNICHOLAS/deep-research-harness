"""Behavioral tests for harness.agent (and the __main__ entrypoint that drives it).

Every test here builds a real deepagents-compiled graph via `build_agent` — nothing about
deepagents itself is mocked, only the model (`harness.agent.build_chat_model`, patched per
the module that imports it, never a network call) and, for the `__main__` tests, config
loading. Tool calls stay confined to filesystem/todo tools; nothing here drives
`fetch_pages`/`search_web`, which would touch a real browser or a real SearXNG instance.
"""

from pathlib import Path

from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import harness.__main__ as main_module
from harness.agent import build_agent
from harness.sources import SourceRegistry


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


def _patch_model(monkeypatch, model):
    monkeypatch.setattr("harness.agent.build_chat_model", lambda config, role: model)


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
    _patch_model(monkeypatch, model)

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
    _patch_model(monkeypatch, model)

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
    _patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())

    assert {"fetch_pages", "search_web"} <= _tools_by_name(graph).keys()


async def test_build_agent_disables_the_general_purpose_subagent(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    model = scripted_model([AIMessage(content="done")])
    _patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())

    # Asserted on the outcome (the tool disappearing), never on the derived profile-key
    # string, so this fails loudly if deepagents changes its key derivation instead of
    # silently passing on a profile that no longer matched anything.
    assert "task" not in _tools_by_name(graph)


async def test_build_agent_includes_todo_list_middleware(make_config, monkeypatch, scripted_model):
    config = make_config()
    model = scripted_model([AIMessage(content="done")])
    _patch_model(monkeypatch, model)

    graph = build_agent(config, SourceRegistry())

    assert "write_todos" in _tools_by_name(graph)


async def test_execute_is_excluded_from_the_models_tool_schema(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
    model = scripted_model([AIMessage(content="done")])
    _patch_model(monkeypatch, model)

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
    _patch_model(monkeypatch, model)

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
    _patch_model(monkeypatch, model)

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
    _patch_model(monkeypatch, model)

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
    _patch_model(monkeypatch, model)

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
    monkeypatch.setattr("harness.models.build_chat_model", lambda cfg, role: model)
    _patch_model(monkeypatch, model)
    monkeypatch.setattr(main_module, "load_config", lambda: config)

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
    monkeypatch.setattr("harness.models.build_chat_model", lambda cfg, role: model)
    _patch_model(monkeypatch, model)
    monkeypatch.setattr(main_module, "load_config", lambda: config)

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
    monkeypatch.setattr("harness.models.build_chat_model", lambda cfg, role: model)
    _patch_model(monkeypatch, model)
    monkeypatch.setattr(main_module, "load_config", lambda: config)

    await main_module.main(["a question with no tool calls"])

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "main() printed nothing"
    printed_path = lines[-1].strip()
    assert printed_path.endswith(".md")
    assert Path(printed_path).exists()
    assert Path(printed_path).parent == config.agent.reports_dir
