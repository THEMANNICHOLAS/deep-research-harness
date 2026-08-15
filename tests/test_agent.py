"""Behavioral tests for harness.agent (and the __main__ entrypoint that drives it).

Every test builds a real deepagents-compiled graph via `build_agent`: nothing about deepagents is
mocked, only the model and, for the `__main__` tests, config loading. Tool calls stay confined to
filesystem/todo tools — driving `fetch_pages`/`search_web` would touch a real browser or SearXNG.
"""

import asyncio
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Interrupt

import harness.__main__ as main_module
from harness.agent import build_agent
from harness.config import AgentSettings, HarnessConfig, run_workspace_dir
from harness.display import PlainRenderer
from harness.report import (
    _CUT_SHORT_HEADING,
    _NO_ANSWER_TEXT,
    _ROUND_CAP_TEXT,
    _WALL_CLOCK_TEXT,
    VERDICT_LABEL,
)
from harness.sources import SourceRegistry
from tests.conftest import (
    ScriptedChatModel,
    _FakeMarkdown,
    _FakeResult,
    drain_stdout,
    install_search_transport,
    patch_model,
    patch_run,
    verify_reply,
    write_source_capture,
)


@pytest.fixture
def noop_agent(make_config, monkeypatch, scripted_model):
    """A real compiled graph over a model scripted to answer once, plus that model.

    Six tests below need nothing but a graph over a model that does nothing. The model comes back
    with it because two of them assert on what reached it, which `patch_model` otherwise hides.
    """
    model = scripted_model([AIMessage(content="done")])
    patch_model(monkeypatch, model)
    return model, build_agent(make_config(), SourceRegistry())


def _tools_by_name(graph):
    return graph.nodes["tools"].bound.tools_by_name


def _declared_subagent_names(graph) -> set[str]:
    """Recover the declared subagent names backing the `task` tool.

    Mirrors `_filesystem_backend`: deepagents does not expose the subagent-name list on the
    compiled graph, but the `task` tool's coroutine closes over a `{name: runnable}` dict, so
    this walks its closure to find it.
    """
    task_tool = _tools_by_name(graph)["task"]
    for cell in task_tool.coroutine.__closure__ or ():
        candidate = cell.cell_contents
        if (
            isinstance(candidate, dict)
            and candidate
            and all(isinstance(k, str) for k in candidate)
            and all(hasattr(v, "invoke") for v in candidate.values())
        ):
            return set(candidate)
    raise AssertionError("could not recover declared subagent names from the task tool")


def _filesystem_backend(graph):
    """Recover the `FilesystemBackend` a compiled graph was built with.

    deepagents does not expose middleware instances on the compiled graph, but every filesystem
    tool's function closes over the owning `FilesystemMiddleware`, so `read_file`'s closure is the
    way in for the `SandboxBackendProtocol` check.
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

    # Behavioral, not structural: had `build_agent` ignored `build_chat_model`'s return value,
    # this scripted content would never appear.
    assert result["messages"][-1].content == "final answer from the configured fake"


async def test_build_agent_delivers_the_rendered_prompt_and_the_question_to_the_model(
    noop_agent,
):
    """Guards against a stale prompt reaching the model: `_received_messages` records the real
    request, so this checks it directly instead of trusting the wiring.
    """
    model, graph = noop_agent

    await graph.ainvoke(
        {"messages": [HumanMessage(content="What is the boiling point of gallium?")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    assert model._received_messages, "the model was never called"
    first_request = model._received_messages[0]

    system_messages = [m for m in first_request if isinstance(m, SystemMessage)]
    assert system_messages, "no system message reached the model"
    # Distinctive text from `harness/prompts/orchestrator.md`, not from any default
    # LangChain/deepagents prompt, so this fails if `build_agent` stops rendering it.
    assert "lead researcher in a cited-sources research harness" in str(system_messages[0].content)

    human_messages = [m for m in first_request if isinstance(m, HumanMessage)]
    assert any("What is the boiling point of gallium?" in str(m.content) for m in human_messages), (
        "the research question never arrived as a human message"
    )


async def test_build_agent_exposes_the_harness_tools(noop_agent):
    _, graph = noop_agent

    # `fetch_pages` moved to the reader subagent (Phase 1): the lead delegates through `task`
    # rather than fetching directly.
    assert {"search_web", "task"} <= _tools_by_name(graph).keys()
    assert "fetch_pages" not in _tools_by_name(graph)


async def test_build_agent_disables_the_general_purpose_subagent(noop_agent):
    _, graph = noop_agent

    # `task` now exists (backing the declared `reader` subagent), so the general-purpose
    # disable is evidenced by the declared-agent list, not by the tool's absence.
    assert _declared_subagent_names(graph) == {"reader"}


async def test_build_agent_lead_excludes_fetch_and_gains_task(
    make_config, patch_models_by_role, scripted_model
):
    """R1 is structural: the lead's tool set physically excludes `fetch_pages` and gains `task`,
    the delegation mechanism to the reader subagent (D1).
    """
    head_model = scripted_model([AIMessage(content="done")])
    reader_model = scripted_model([AIMessage(content="reader done")])
    patch_models_by_role({"head": head_model, "subagent": reader_model})

    graph = build_agent(make_config(), SourceRegistry())

    assert "task" in _tools_by_name(graph)
    assert "fetch_pages" not in _tools_by_name(graph)

    await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    assert head_model._bound_tool_names, "bind_tools was never called on the head model"
    for offered in head_model._bound_tool_names:
        assert "task" in offered
        assert "search_web" in offered
        assert "ask_user" in offered
        assert "fetch_pages" not in offered


def test_reader_spec_contract(make_config, scripted_model):
    """The declared reader `SubAgent` spec (D1) carries the subagent-role model, the rendered
    reader.md prompt, and the run's own `fetch_pages` instance — never a second one.
    """
    from harness.agent import _reader_spec
    from harness.prompts import render
    from harness.tools import build_tools

    config = make_config()
    reader_model = scripted_model([AIMessage(content="done")])
    reader_tools = build_tools(config, SourceRegistry()).reader

    spec = _reader_spec(config, reader_model, reader_tools)

    assert spec["name"] == "reader"
    assert spec["model"] is reader_model
    assert spec["system_prompt"] == render(
        "reader",
        current_date=date.today().isoformat(),
        max_urls_per_call=config.fetch.max_urls_per_call,
    )
    assert spec["tools"] is reader_tools
    # The reader has no checkpointer forwarded and cannot interrupt — inheriting the lead's
    # `ask_user` interrupt entry would register an interrupt that can never fire.
    assert "interrupt_on" not in spec


async def test_reader_model_profile_excludes_execute(make_config, patch_models_by_role):
    """Risk #1: deepagents resolves a HarnessProfile per SUBAGENT MODEL key — if the reader's
    key is never registered, the no-shell invariant silently breaks for the reader.
    """
    from deepagents._models import get_model_identifier, get_model_provider
    from deepagents.profiles.harness.harness_profiles import _get_harness_profile
    from pydantic import SecretStr

    from tests.conftest import ScriptedChatModel

    head_model = ScriptedChatModel(
        model="head-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([AIMessage(content="done")])
    reader_model = ScriptedChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([AIMessage(content="reader done")])
    patch_models_by_role({"head": head_model, "subagent": reader_model})

    build_agent(make_config(), SourceRegistry())

    provider = get_model_provider(reader_model)
    identifier = get_model_identifier(reader_model)
    profile = _get_harness_profile(f"{provider}:{identifier}")

    assert profile is not None
    assert "execute" in profile.excluded_tools


# --- Phase 2: failure path — task-tool retry/error middleware, fetch_raw fallback (D2) --


class _RaisingChatModel(ScriptedChatModel):
    """Raises on every call, recording each invocation — drives the task-tool retry path."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._received_messages.append(list(messages))
        self._call_count += 1
        raise RuntimeError("boom")


def _task_call(description: str, call_id: str = "call_task") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": description, "subagent_type": "reader"},
                "id": call_id,
            }
        ],
    )


async def test_a_reader_crash_becomes_an_error_task_message_after_one_retry(
    make_config, patch_models_by_role, scripted_model
):
    """D2: the task tool is wrapped by ToolRetryMiddleware(max_retries=1) inner and
    ToolErrorMiddleware outer, so a reader crash surfaces as an error ToolMessage — never a
    raised exception out of `ainvoke` — after exactly one retry (two reader model calls total).
    """
    from pydantic import SecretStr

    head_model = scripted_model(
        [_task_call("Fetch and digest https://a.test"), AIMessage(content="done")]
    )
    reader_model = _RaisingChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    )
    patch_models_by_role({"head": head_model, "subagent": reader_model})

    graph = build_agent(make_config(), SourceRegistry())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    task_messages = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task"
    ]
    assert len(task_messages) == 1
    assert task_messages[0].status == "error"
    assert str(task_messages[0].content).startswith("READER FAILED")
    assert reader_model._call_count == 2  # initial attempt + exactly one retry


async def test_a_reader_ending_with_no_final_text_returns_an_empty_task_message(
    make_config, patch_models_by_role, scripted_model
):
    """deepagents' documented empty-ToolMessage behavior: a subagent ending on an AIMessage with
    no text produces a task ToolMessage with empty content. THIS emptiness is the documented
    failure signal Phase 3's prompt teaches the lead to treat as a failed digest.
    """
    head_model = scripted_model(
        [_task_call("Fetch and digest https://a.test"), AIMessage(content="done")]
    )
    reader_model = scripted_model([AIMessage(content="")])
    patch_models_by_role({"head": head_model, "subagent": reader_model})

    graph = build_agent(make_config(), SourceRegistry())
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    task_messages = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task"
    ]
    assert len(task_messages) == 1
    assert task_messages[0].content == ""


def _reader_fetch_call(url: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "fetch_pages", "args": {"urls": [url]}, "id": "call_fetch"}],
    )


async def test_a_reader_crash_after_a_successful_fetch_leaves_the_source_unread(
    make_config, patch_models_by_role, scripted_model, install_crawler
):
    """R5: "digested" is marked at the delegation boundary, not at fetch time. The reader
    fetches a page successfully, then crashes (its script runs out, raising `IndexError` —
    the crash-after-partial-fetch case); no digest ever reaches the lead, so the source must
    stay "unread". A fetch-time mark here would make the report disclose a digest that never
    existed.
    """
    head_model = scripted_model(
        [_task_call("Fetch and digest https://a.test"), AIMessage(content="done")]
    )
    # One scripted reply only: the fetch call. The reader's next model call — and the whole
    # retry attempt — exhausts the script and raises.
    reader_model = scripted_model([_reader_fetch_call("https://a.test")])
    patch_models_by_role({"head": head_model, "subagent": reader_model})
    install_crawler(
        [
            _FakeResult(
                "https://a.test",
                markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body"),
            )
        ]
    )

    registry = SourceRegistry()
    graph = build_agent(make_config(), registry)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    task_messages = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task"
    ]
    assert len(task_messages) == 1
    assert task_messages[0].status == "error"
    # The fetch itself succeeded and registered the source...
    source = registry.get("S1")
    assert source is not None
    # ...but with no digest delivered, it is disclosed as unread, never as digested.
    assert source.read_mode == "unread"


async def test_an_empty_digest_leaves_the_fetched_source_unread(
    make_config, patch_models_by_role, scripted_model, install_crawler
):
    """The empty-digest half of the same boundary: the reader fetches successfully but ends
    with no final text — the documented failure signal the prompt tells the lead to treat as
    a failed delegation — so the source stays "unread" until a `fetch_raw` recovery marks it.
    """
    head_model = scripted_model(
        [_task_call("Fetch and digest https://a.test"), AIMessage(content="done")]
    )
    reader_model = scripted_model([_reader_fetch_call("https://a.test"), AIMessage(content="")])
    patch_models_by_role({"head": head_model, "subagent": reader_model})
    install_crawler(
        [
            _FakeResult(
                "https://a.test",
                markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body"),
            )
        ]
    )

    registry = SourceRegistry()
    graph = build_agent(make_config(), registry)
    await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    source = registry.get("S1")
    assert source is not None
    assert source.read_mode == "unread"


async def test_fetch_raw_is_offered_to_the_lead_but_not_the_reader(
    noop_agent, make_config, scripted_model
):
    _, graph = noop_agent
    assert "fetch_raw" in _tools_by_name(graph)

    from harness.agent import _reader_spec
    from harness.tools import build_tools

    config = make_config()
    reader_tools = build_tools(config, SourceRegistry()).reader
    spec = _reader_spec(config, scripted_model([AIMessage(content="done")]), reader_tools)
    assert "fetch_raw" not in [t.name for t in spec["tools"]]


async def test_build_agent_includes_todo_list_middleware(noop_agent):
    _, graph = noop_agent

    assert "write_todos" in _tools_by_name(graph)


async def test_execute_is_excluded_from_the_models_tool_schema(noop_agent):
    model, graph = noop_agent

    await graph.ainvoke(
        {"messages": [HumanMessage(content="hi")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    assert model._bound_tool_names, "bind_tools was never called — nothing was asserted"
    for offered in model._bound_tool_names:
        assert "execute" not in offered


def test_filesystem_backend_is_never_a_sandbox(noop_agent):
    _, graph = noop_agent
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

    registry = SourceRegistry()
    graph = build_agent(config, registry)
    await graph.ainvoke(
        {"messages": [HumanMessage(content="write something")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    # Under THIS run's subdirectory, not the shared root: that is what keeps a concurrent run
    # from reading these notes as its own.
    written = run_workspace_dir(config, registry.run_id) / "notes.md"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "hello"
    assert not (config.agent.workspace_dir / "notes.md").exists()


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

    registry = SourceRegistry()
    graph = build_agent(config, registry)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="try to escape")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    # One level up is the shared workspace, where a sibling run's notes live.
    escaped = run_workspace_dir(config, registry.run_id).parent / "escape.md"
    assert not escaped.exists()
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert any(getattr(m, "status", None) == "error" for m in tool_messages), tool_messages


async def test_compression_offloads_evicted_history_and_preserves_todos_state(
    make_config, monkeypatch, scripted_model
):
    """Cross the summarizer's trigger with real volume, then check real evidence.

    Two properties of the installed middleware shape this test:

    - `keep=("messages", 20)` vetoes summarization independently of `trigger`, since
      `_find_safe_cutoff` is a no-op whenever `len(messages) <= keep`. 14 tool rounds plus the
      plan round plus the human message clears 20, so the padding crosses both floors.
    - The middleware wraps the model call rather than mutating state: it offloads evicted
      messages to `backend` and rewrites only the NEXT model *request*. The post-run message
      list is therefore never shorter than what was scripted, and no summary message appears in
      `state["messages"]` — assertions about either would prove nothing.

    So the checkable evidence is (1) a later model request carrying the middleware's own
    summarization `HumanMessage`, which the scripted model cannot have produced, and (2) the
    `[S1]` marker from an early finding still recoverable from the offload file under
    `conversation_history/` — D7's attribution survival.
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

    registry = SourceRegistry()
    graph = build_agent(config, registry)
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
    history_dir = run_workspace_dir(config, registry.run_id) / "conversation_history"
    offloaded_files = list(history_dir.glob("*.md"))
    assert offloaded_files, "compression fired but wrote no offload file under the backend"
    offloaded_text = "\n".join(f.read_text(encoding="utf-8") for f in offloaded_files)
    assert "[S1]" in offloaded_text

    # Todos live in graph state, not the message list, so compression cannot drop them.
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
    # Summed across BOTH AIMessages: reading only the final message's `usage_metadata` would
    # report 40/20/60 rather than the true 60/30/90.
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


async def test_main_exits_nonzero_and_writes_no_report_when_searxng_is_unreachable(
    make_config, monkeypatch, scripted_model, capsys
):
    """R1: SearXNG down at startup fails fast, before any run or report."""
    config = make_config()
    model = scripted_model([AIMessage(content="unused — the search preflight aborts first")])
    patch_run(monkeypatch, config, model, skip_preflight=True, run_search_preflight=True)

    def handler(request):
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)

    exit_code = await main_module.main(["a question"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err
    assert "SearXNG" in captured.err
    assert "container" in captured.err.lower() or "docker" in captured.err.lower()
    assert not config.agent.reports_dir.exists() or not any(config.agent.reports_dir.iterdir())


# --- Phase 5: round cap, wall clock, and cut-short reporting ---------------------------


async def _run_main(
    argv: list[str], config: HarnessConfig, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[Path], str, str]:
    """Run `main()`; return (exit code, reports-dir listing, stdout, stderr).

    Shared by the D2 gating tests below, each of which cares about the same four facts:
    whether a report landed on disk, and what reached each stream.
    """
    exit_code = await main_module.main(argv)
    captured = capsys.readouterr()
    files = list(config.agent.reports_dir.iterdir()) if config.agent.reports_dir.exists() else []
    return exit_code, files, captured.out, captured.err


def _install_slow_search(monkeypatch, delay_seconds: float) -> None:
    """Stall `search_web` so a bounds test can expire mid-call (ordering caveat: see helper)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay_seconds)
        return httpx.Response(200, json={"query": "x", "results": []})

    install_search_transport(monkeypatch, handler)


def test_final_answer_skips_trailing_tool_output():
    """On a cut-short path the message list usually ends in tool traffic, so
    `messages[-1].content` would publish internal tool output as the run's ANSWER. The last
    `AIMessage` that actually said something is what counts.
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
    """A run cut short before any prose has no answer at all, and `report.py` renders that case
    explicitly rather than showing an empty section.
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
    """`AIMessage.content` is `str | list`, and `str(content)` on the list shape is a repr, so a
    provider returning content blocks would put `[{'type': 'text', ...}]` under `## Answer`.
    Non-text blocks (a `thinking` block, say) are dropped rather than rendered.
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
    """A dead stdin must not strand the run on a future nothing will complete.

    `input()` raising `EOFError` killed the worker thread BEFORE `_resolve` was scheduled, so
    `await future` never returned, and the wall clock is disarmed for a pre-research question.
    Against the old code this hangs rather than failing, hence its own timeout.
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
    """The report path is the final line of STDOUT, frozen because R1 depends on it. `input(prompt)`
    writes with no trailing newline, so the path landed on the same line as a pending `> `; the
    prompt belongs on stderr with the rest of the terminal chatter.
    """
    monkeypatch.setattr("builtins.input", lambda: "the metal")

    answer = await main_module._read_answer("> ")

    captured = capsys.readouterr()
    assert answer == "the metal"
    assert captured.out == "", f"the prompt reached stdout: {captured.out!r}"
    assert "> " in captured.err


async def test_read_answer_runs_on_a_daemon_thread(monkeypatch):
    """A non-daemon worker is joined at interpreter shutdown, so the process hangs after the wall
    clock has already fired and written its report. This is the cheapest in-process assertion of
    that property.
    """
    recorded: dict[str, bool] = {}

    def fake_input(prompt: str = "") -> str:
        recorded["daemon"] = threading.current_thread().daemon
        return "answer"

    monkeypatch.setattr("builtins.input", fake_input)

    await main_module._read_answer()

    assert recorded.get("daemon") is True


async def test_read_answer_returns_what_was_typed(monkeypatch):
    """The thread-based read must not mangle the answer, and `_answer_questions`' normalization
    must still see it whole.
    """
    monkeypatch.setattr("builtins.input", lambda prompt="": "  Yes, region EU-West  ")
    interrupt = Interrupt(value={"action_requests": [{"args": {"question": "Which region?"}}]})

    decisions = await main_module._answer_questions(interrupt, PlainRenderer())

    assert decisions == [{"type": "respond", "message": "Yes, region EU-West"}]


async def test_main_cuts_the_run_short_at_the_round_cap(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """`max_rounds=1` with a model that never stops proposing tool calls forces `recursion_limit`
    to end the run rather than the graph terminating on its own. `ScriptedChatModel` raises
    `IndexError` when its script runs out, so far more responses are scripted than the cap can
    consume — proving the cap, not an exhausted script, is what ended it.
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
    # A run that consumed the whole 20-item script would have driven far more model calls.
    assert len(model._received_messages) < 5
    body = report_path.read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    # Names the ROUND CAP specifically: without this, swapping the `GraphRecursionError` and
    # `TimeoutError` labels in `__main__`'s except clauses would keep every cut-short test green.
    assert _ROUND_CAP_TEXT in body
    assert _WALL_CLOCK_TEXT not in body


@pytest.mark.parametrize(("max_rounds", "expect_cut_short"), [(3, True), (4, False)])
async def test_max_rounds_scales_the_recursion_limit(
    make_config, monkeypatch, scripted_model, tmp_path, capsys, max_rounds, expect_cut_short
):
    """Pins the `max_rounds * 2 + 1` mapping at its measured boundary, which the round-cap test
    above cannot — that one passes under ANY mapping small enough to trip.

    A run doing exactly one tool round is cut short at `max_rounds=3` (limit 7) and completes at
    `max_rounds=4` (limit 9). Measured, not derived: the graph adds a fixed ~7-9 superstep
    middleware overhead on top of the ~2 a round costs. Passing `max_rounds` straight through as
    `recursion_limit` would cut BOTH short, so this pair is what separates the two mappings.
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
    """`todos=last_todos or []` is the only route from the streamed todo state into the report
    (D9). Passing `[]` there would stay green otherwise, since `test_report.py` proves the RENDER
    and never the wiring.
    """
    # max_rounds=2 (limit 5), measured: far enough for the write_todos round to reach the stream,
    # short of the ~9 a full run needs, so the report is cut short AND has todos to name.
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
    # The last message here is the write_todos ToolMessage, so `messages[-1].content` would
    # publish "Updated todo list to [...]" as the run's ANSWER.
    assert _NO_ANSWER_TEXT in body
    assert "Updated todo list" not in body


async def test_main_writes_no_report_when_the_wall_clock_expires_with_no_answer(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """D2: the wall clock expires while the only model turn is a bare tool-call proposal (no
    prose), so `_final_answer` sees nothing — no report, exit 1, stderr names the wall clock.

    The elapsed-time assertion pins the timeout itself: without it, a broad "catch anything,
    call it wall_clock" shortcut that ran the full sleep would still pass.
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
    model = scripted_model([ping, search_call])
    patch_run(monkeypatch, config, model)
    # After the model is built — see `_install_slow_search`'s docstring.
    _install_slow_search(monkeypatch, delay_seconds=3)

    started = time.monotonic()
    exit_code, files, out, err = await _run_main(
        ["a question that starts researching"], config, capsys
    )
    elapsed = time.monotonic() - started

    assert exit_code == 1
    assert files == [], f"a report was written despite no final answer: {files}"
    assert any("wall clock" in line for line in err.splitlines()), err
    # Post-run summary still reaches the normal terminal (Phase 1's post-run summary path).
    assert "summary:" in out
    assert elapsed < 2.5, f"run took {elapsed}s — the wall clock did not actually fire early"


async def test_main_writes_a_cut_short_report_when_the_wall_clock_expires_with_an_answer(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """D2's other wall-clock branch: the same expiry, but the interrupted turn already carried
    prose alongside its tool call, so a final answer exists — the report is still written,
    disclosing the cut-short, and the exit code stays 0.
    """
    agent = AgentSettings(
        wall_clock_seconds=1,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)

    ping = AIMessage(content="pong")
    partial_then_search = AIMessage(
        content="Partial finding: Acme quoted $4.20/unit.",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    model = scripted_model([ping, partial_then_search])
    patch_run(monkeypatch, config, model)
    # After the model is built — see `_install_slow_search`'s docstring.
    _install_slow_search(monkeypatch, delay_seconds=3)

    started = time.monotonic()
    exit_code, files, out, err = await _run_main(
        ["a question that starts researching"], config, capsys
    )
    elapsed = time.monotonic() - started

    assert exit_code == 0
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    # Names the WALL CLOCK specifically — see the round-cap test for why the heading is not enough.
    assert _WALL_CLOCK_TEXT in body
    assert _ROUND_CAP_TEXT not in body
    assert "Partial finding: Acme quoted $4.20/unit." in body
    # Well under the full 3s sleep: cut off near the 1s bound rather than completed and mislabeled.
    assert elapsed < 2.5, f"run took {elapsed}s — the wall clock did not actually fire early"


async def test_a_pre_research_clarification_does_not_start_the_wall_clock(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """The clock arms at the first `search_web`/`fetch_pages` call, not at process start, so a
    pre-research `ask_user` wait of any length must not trip it — the wait (2s) is longer than the
    configured clock (1s) and the run must still finish clean. Paired with the mid-run test below;
    neither alone pins where the clock starts.
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


async def test_a_mid_run_clarification_with_no_answer_is_bounded_by_the_wall_clock(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """Pairs with the pre-research test above: once research has begun the clock runs and is not
    paused for an interrupt, so an unanswered mid-run ask still ends the run at the bound.

    D2: nothing in this scripted run ever produces prose (`search_call` and `ask` are both
    content-less tool-call proposals), so this is the wall-clock NO-answer case — no report,
    exit 1 — not the disclosed-report case this test asserted before Phase 4's gate existed.
    The elapsed-time assertion pins the timeout to the wait itself.
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
    model = scripted_model([ping, search_call, ask])
    patch_run(monkeypatch, config, model)
    # After the model is built — see `_install_slow_search`'s docstring.
    _install_slow_search(monkeypatch, delay_seconds=0.1)

    async def _slow_answer(prompt: str = "> ") -> str:
        await asyncio.sleep(3)
        return "Narrower."

    monkeypatch.setattr(main_module, "_read_answer", _slow_answer)

    started = time.monotonic()
    exit_code, files, out, err = await _run_main(["Research widgets"], config, capsys)
    elapsed = time.monotonic() - started

    assert exit_code == 1
    assert files == [], f"a report was written despite no final answer: {files}"
    assert any("wall clock" in line for line in err.splitlines()), err
    # Well under the full 3s wait: proves the wait was actually cut off near the 1s
    # remaining on the clock, not merely completed and then mislabeled.
    assert elapsed < 2.5, f"run took {elapsed}s — the wall clock did not actually fire early"


async def test_main_writes_no_report_when_the_run_dies_mid_flight(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """D2: a hard error (the script runs out right after one real round, so `ScriptedChatModel`
    raises `IndexError`, standing in for a genuine mid-run failure) writes NO report — only the
    stderr error and exit 1. `main` must still never let a traceback escape.
    """
    config = make_config(
        agent=AgentSettings(workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports")
    )
    ping = AIMessage(content="pong")
    plan_call = AIMessage(
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

    exit_code, files, out, err = await _run_main(
        ["question that never gets an answer"], config, capsys
    )

    assert exit_code == 1
    assert any(line.startswith("error:") for line in err.splitlines()), err
    assert "Traceback" not in err
    assert "IndexError" in err
    assert files == [], f"a report was written for a hard error: {files}"
    # Post-run summary still reaches the normal terminal (Phase 1's post-run summary path).
    assert "summary:" in out


async def test_main_exits_cleanly_on_keyboard_interrupt_mid_stream(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """D2: Ctrl+C maps onto the existing hard-error path — no report, exit 1, and the renderer
    still closes cleanly (no exception escapes `main()`).

    Raises the interrupt directly from `main()`'s own `async for` over `agent.astream(...)`
    (a monkeypatched stream, per the plan's alternative to scripting a model side-effect) rather
    than from inside a model/tool call: a KeyboardInterrupt raised THERE was observed to surface
    at this boundary as `asyncio.CancelledError` instead — see the Phase 4 handoff note — which
    is neither `Exception` nor `KeyboardInterrupt` and escapes `main()` uncaught. That gap is out
    of THIS test's scope (it pins `main()`'s own except clause, not langgraph's internals).
    """
    config = make_config(
        agent=AgentSettings(workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports")
    )
    ping = AIMessage(content="pong")
    model = scripted_model([ping])
    patch_run(monkeypatch, config, model)

    real_build_agent = main_module.build_agent

    def _build_agent_that_interrupts(config: HarnessConfig, registry: Any) -> Any:
        real_agent = real_build_agent(config, registry)

        class _InterruptingAgent:
            def astream(self, *args: Any, **kwargs: Any) -> Any:
                async def _gen() -> Any:
                    async for mode, chunk in real_agent.astream(*args, **kwargs):
                        yield mode, chunk
                        raise KeyboardInterrupt("test abort")

                return _gen()

        return _InterruptingAgent()

    monkeypatch.setattr(main_module, "build_agent", _build_agent_that_interrupts)

    exit_code, files, out, err = await _run_main(["a question"], config, capsys)

    assert exit_code == 1
    assert files == [], f"a report was written for a Ctrl+C abort: {files}"
    assert any(line.startswith("error:") for line in err.splitlines()), err
    assert "ctrl" in err.lower() or "abort" in err.lower()
    assert "Traceback" not in err
    assert "summary:" in out


async def test_main_aborts_and_writes_no_report_when_searxng_fails_repeatedly_mid_run(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """Phase 3's deferred full-`main()` criterion (D3): three consecutive connection failures
    during `search_web` trip `SearchUnavailableError`, which the loop's generic exception
    handler treats like any other hard error — no report, exit 1.
    """
    config = make_config(
        agent=AgentSettings(workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports")
    )
    ping = AIMessage(content="pong")

    def _search_call(call_id: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": call_id}],
        )

    # make_config's default max_consecutive_failures is 3 — three scripted search rounds are
    # exactly enough for the third tool execution to trip the abort before a fourth model call.
    model = scripted_model([ping, _search_call("c1"), _search_call("c2"), _search_call("c3")])
    patch_run(monkeypatch, config, model)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    install_search_transport(monkeypatch, handler)  # after scripted_model — see its docstring

    exit_code, files, out, err = await _run_main(
        ["a question that starts researching"], config, capsys
    )

    assert exit_code == 1
    assert files == [], f"a report was written despite a SearXNG abort: {files}"
    assert any(line.startswith("error:") for line in err.splitlines()), err
    assert "SearXNG" in err
    assert "summary:" in out


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
    """A round-capped run whose partial answer cites a real, captured source — the cut-short x
    verification seam. Every other cut-short test scripts an answer with no `[Sn]` marker, so
    verification only ever takes its trivial no-call path, and reaching the model-call branch
    needs `patch_run`'s patch of `harness.verify`'s own `build_chat_model` binding.

    The verification model is scripted SEPARATELY from the run model: how many rounds the cap
    allows is a langgraph detail, so a shared script would make the verify reply's index depend
    on it.
    """
    agent = AgentSettings(
        max_rounds=2, workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
    )
    config = make_config(agent=agent)

    # `main` builds the registry itself, so a pre-populated one is injected in its place, standing
    # in for the `fetch_pages` calls a real run would have made before the cap.
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

    verify_model = scripted_model([verify_reply("not_supported", "The capture reads $5.10.")])
    monkeypatch.setattr("harness.verify.build_chat_model", lambda cfg, role: verify_model)

    await main_module.main(["what does Acme charge?"])

    _, lines = drain_stdout(capsys)
    body = Path(lines[-1].strip()).read_text(encoding="utf-8")

    assert _CUT_SHORT_HEADING in body, "this run was supposed to hit the round cap"
    # The check actually ran on the cut-short path — not skipped, not defaulted.
    assert verify_model._call_count == 1
    assert f"{VERDICT_LABEL} not supported - The capture reads $5.10." in body
    # And R1's citation resolution still happened on the same partial answer.
    assert "https://example.test/pricing" in body


async def test_a_clarifying_question_can_arrive_without_a_question_argument(
    make_config, monkeypatch, capsys
):
    """Exercises the `description` and `str(args)` fallbacks of `args["question"] or description or
    str(args)`: nothing guarantees deepagents keeps putting the prompt under `args`, and those
    fallbacks are all that stand between a schema change and an empty prompt at the terminal.
    Driven through `_answer_questions` directly, since no real model can be scripted into that
    shape.
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

    decisions = await main_module._answer_questions(interrupt, PlainRenderer())

    out, _ = drain_stdout(capsys)
    asked = [line for line in out.splitlines() if line.strip()]
    metal_line = next(line for line in asked if line == "Metal or album?")
    isotope_line = next(line for line in asked if "isotope" in line)
    assert asked.index(metal_line) < asked.index(isotope_line), (
        "the description fallback never fired, or fired out of order"
    )
    assert [d["message"] for d in decisions] == ["answered", "answered"]
