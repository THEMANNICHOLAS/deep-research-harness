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
import openai
import pytest
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Interrupt
from pydantic import PrivateAttr

import harness.__main__ as main_module
from harness.activity import ActivitySink, active_reader
from harness.agent import (
    ResearchDeadline,
    _research_reserve,
    _retry_on_non_search_abort,
    _summarize_tool_args,
    _summarize_tool_result,
    build_agent,
)
from harness.config import AgentSettings, HarnessConfig, run_workspace_dir
from harness.display import PlainRenderer, StageTracker
from harness.report import (
    _CUT_SHORT_HEADING,
    _NO_ANSWER_TEXT,
    _ROUND_CAP_TEXT,
    _SYNTHESIS_MARGIN_TEXT,
    _WALL_CLOCK_TEXT,
)
from harness.runlog import RunLog
from harness.sources import SourceRegistry
from tests.conftest import (
    ConcurrencyTrackingModel,
    ScriptedChatModel,
    _FakeMarkdown,
    _FakeResult,
    approve_all,
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


def _declared_subagents(graph) -> dict[str, Any]:
    """Recover the `{name: runnable}` dict backing the `task` tool's declared subagents.

    Mirrors `_filesystem_backend`: deepagents does not expose the subagent-name list on the
    compiled graph, but the `task` tool's coroutine closes over this dict, so this walks its
    closure to find it. Returns the runnables (not just the names) so a caller can apply the
    same walk to a NESTED tier's own `task` tool (Step 3's researcher -> reader nesting).
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
            return candidate
    raise AssertionError("could not recover declared subagents from the task tool")


def _declared_subagent_names(graph) -> set[str]:
    """The declared subagent names backing the `task` tool (see `_declared_subagents`)."""
    return set(_declared_subagents(graph))


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

    # `search_web`/`fetch_pages` both moved off the lead (Step 3): it delegates through `task`
    # to the researcher tier rather than researching directly.
    assert "task" in _tools_by_name(graph)
    assert "search_web" not in _tools_by_name(graph)
    assert "fetch_pages" not in _tools_by_name(graph)


async def test_build_agent_disables_the_general_purpose_subagent(noop_agent):  # R4
    _, graph = noop_agent

    # `task` now exists (backing the declared `researcher` subagent), so the general-purpose
    # disable is evidenced by the declared-agent list, not by the tool's absence.
    assert _declared_subagent_names(graph) == {"researcher"}


async def test_the_researchers_own_task_tool_declares_only_the_reader(noop_agent):  # R4
    """TEST-FIRST item 3 (lead tool surface): the 3-tier hierarchy is nested, not flattened —
    the lead's own declared subagent is exactly `{"researcher"}` (previous test), and the
    researcher's OWN `task` tool, one level deeper, declares exactly `{"reader"}`.

    R4 regression (PLAN-prompt-injection-defense.md Phase 5): pins the containment structure
    a fenced/sanitized run still relies on — the researcher can dispatch only to the reader,
    never fan out to another tier of its own choosing.
    """
    _, graph = noop_agent

    researcher_runnable = _declared_subagents(graph)["researcher"]

    assert _declared_subagent_names(researcher_runnable) == {"reader"}


def test_lead_interrupt_on_contains_exactly_ask_user():  # R4
    """R4 regression (Phase 5, D7): the lead's whole interrupt surface is `ask_user` — pinning
    `_INTERRUPT_ON`'s key set directly, since that dict is what `build_agent` passes through to
    `interrupt_on` unmodified.
    """
    from harness.agent import _INTERRUPT_ON
    from harness.tools.ask_user import ASK_USER_TOOL_NAME

    assert set(_INTERRUPT_ON) == {ASK_USER_TOOL_NAME}


async def test_the_nested_readers_tool_surface_excludes_a_filesystem_workspace(noop_agent):
    """R6 (Phase 4 trim): the reader carries only what it needs to read and answer in prose —
    no write tools. Inverts the prior contract (the reader used to get a scratch workspace via
    `FilesystemMiddleware`, restored by hand since nesting through a hand-built
    `SubAgentMiddleware` gets none of `create_deep_agent`'s auto-injected base stack for free);
    `reader.md`'s scratch-workspace promise is removed in the same phase (Step 4).
    """
    _, graph = noop_agent

    researcher_runnable = _declared_subagents(graph)["researcher"]
    reader_runnable = _declared_subagents(researcher_runnable)["reader"]
    reader_tool_names = set(_tools_by_name(reader_runnable))

    assert reader_tool_names.isdisjoint(
        {"write_file", "edit_file", "ls", "glob", "grep", "read_file", "delete", "execute"}
    )
    assert "fetch_pages" in reader_tool_names


async def test_build_agent_lead_excludes_search_and_fetch_and_gains_task(
    make_config, patch_models_by_role, scripted_model
):
    """R1 is structural: the lead's tool set physically excludes `search_web`/`fetch_pages` and
    gains `task`, the delegation mechanism to the researcher subagent (Step 3).
    """
    head_model = scripted_model([AIMessage(content="done")])
    researcher_model = scripted_model([AIMessage(content="researcher done")])
    reader_model = scripted_model([AIMessage(content="reader done")])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    graph = build_agent(make_config(), SourceRegistry())

    assert "task" in _tools_by_name(graph)
    assert "search_web" not in _tools_by_name(graph)
    assert "fetch_pages" not in _tools_by_name(graph)

    await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    assert head_model._bound_tool_names, "bind_tools was never called on the head model"
    for offered in head_model._bound_tool_names:
        assert "task" in offered
        assert "ask_user" in offered
        assert "search_web" not in offered
        assert "fetch_pages" not in offered


def test_reader_spec_contract(make_config, scripted_model, tmp_path):
    """The declared reader `SubAgent` spec (D1) carries the subagent-role model, the rendered
    reader.md prompt, and the run's own `fetch_pages` instance — never a second one.
    """
    from deepagents import FilesystemMiddleware
    from deepagents.backends.filesystem import FilesystemBackend
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from deepagents.middleware.summarization import (
        SummarizationMiddleware as DeepagentsSummarizationMiddleware,
    )

    from harness.agent import _reader_spec, _ToolActivityMiddleware
    from harness.prompts import render
    from harness.tools import build_tools

    config = make_config()
    reader_model = scripted_model([AIMessage(content="done")])
    reader_tools = build_tools(config, SourceRegistry()).reader
    backend = FilesystemBackend(root_dir=tmp_path / "workspace")

    spec = _reader_spec(config, reader_model, reader_tools, backend, ActivitySink())

    assert spec["name"] == "reader"
    assert spec["model"] is reader_model
    assert spec["system_prompt"] == render(
        "reader",
        current_date=date.today().isoformat(),
        max_urls_per_call=config.fetch.max_urls_per_call,
    )
    assert spec["tools"] is reader_tools
    # R6 (Phase 4 trim): `FilesystemMiddleware` — and the scratch workspace it backed — is
    # gone. The remaining entries are asserted positively, not just the removal, because a
    # manually-nested subagent gets NONE of `create_deep_agent`'s auto-injected base stack for
    # free (architecture.md) and every surviving entry matters: the summarizer still offloads
    # evicted history to the SAME backend independent of `FilesystemMiddleware` (it holds its
    # own reference, PR #18 review), and the tool-call patcher matches the pre-trim stack.
    assert not any(isinstance(m, FilesystemMiddleware) for m in spec["middleware"])
    summarization_middlewares = [
        m for m in spec["middleware"] if isinstance(m, DeepagentsSummarizationMiddleware)
    ]
    assert len(summarization_middlewares) == 1
    summarizer = summarization_middlewares[0]
    # A private attribute, deliberately: deepagents exposes no public accessor for the backend
    # a summarizer offloads to, and the claim pinned here — the reader's summarizer writes
    # evicted history to THIS run's backend, independent of the dropped `FilesystemMiddleware`
    # — has no other observable surface. The `hasattr` guard exists so a deepagents bump that
    # renames the field fails saying it is an UPSTREAM rename, rather than looking like a
    # `_reader_spec` regression and costing a debugging detour on every bump.
    assert hasattr(summarizer, "_backend"), (
        "deepagents renamed SummarizationMiddleware._backend: an upstream rename, not a "
        "_reader_spec regression. Find the new accessor and update this assertion."
    )
    assert summarizer._backend is backend
    assert any(isinstance(m, PatchToolCallsMiddleware) for m in spec["middleware"])
    # Phase 6 (D-C): the reader tier is the ONLY place `fetch_pages` is observable, so its
    # activity middleware is part of this spec's contract, not an incidental extra.
    assert any(isinstance(m, _ToolActivityMiddleware) for m in spec["middleware"])
    # The reader has no checkpointer forwarded and cannot interrupt — inheriting the lead's
    # `ask_user` interrupt entry would register an interrupt that can never fire.
    # R4 regression (Phase 5): `interrupt_on` stays confined to the lead alone.
    assert "interrupt_on" not in spec


def test_deepagents_summarization_never_overrides_message_mutating_hooks(scripted_model, tmp_path):
    """Pins the invariant `_ReaderDispatchCapMiddleware`'s docstring depends on (review fix
    F2): the cap counts distinct dispatch ids out of `state["messages"]`, which only stays
    correct as long as deepagents' summarizer never rewrites that list. If a future deepagents
    bump ever swaps in LangChain-style mutating summarization (a `before_model`/`after_model`
    override issuing `RemoveMessage`), old dispatch ids would fall out of history and the cap
    would silently reset mid-attempt -- this must fail loudly here instead.
    """
    from deepagents.backends.filesystem import FilesystemBackend
    from deepagents.middleware.summarization import create_summarization_middleware
    from langchain.agents.middleware import AgentMiddleware

    backend = FilesystemBackend(root_dir=tmp_path / "workspace")
    reader_model = scripted_model([AIMessage(content="done")])

    middleware = create_summarization_middleware(reader_model, backend)

    # `AgentMiddleware` defines all four as concrete (no-op) methods, not abstract ones, so an
    # unoverridden hook on `middleware`'s class is literally the SAME function object as the
    # base class's -- `is`, not just an equality/behavior check. A middleware that overrode any
    # of these to mutate `state["messages"]` would define its own function here and fail this.
    for hook in ("before_model", "abefore_model", "after_model", "aafter_model"):
        assert getattr(type(middleware), hook) is getattr(AgentMiddleware, hook), (
            f"{hook} is overridden on {type(middleware).__name__} -- deepagents' summarizer "
            "may now mutate state['messages'], which would silently uncap "
            "_ReaderDispatchCapMiddleware"
        )


def test_researcher_spec_has_no_interrupt_on(make_config, scripted_model, tmp_path):
    """Regression pin (D6; deferred from Step 3, developer-approved to land in Step 4): the
    researcher `SubAgent` spec omits `interrupt_on`, the same as the reader above — a nested
    researcher has no checkpointer forwarded and cannot interrupt either.

    R4 regression (Phase 5): `interrupt_on` stays confined to the lead alone.
    """
    from deepagents.backends.filesystem import FilesystemBackend

    from harness.agent import _reader_spec, _researcher_spec, _ToolActivityMiddleware
    from harness.tools import build_tools

    config = make_config()
    reader_model = scripted_model([AIMessage(content="done")])
    researcher_model = scripted_model([AIMessage(content="done")])
    tool_sets = build_tools(config, SourceRegistry())
    backend = FilesystemBackend(root_dir=tmp_path / "workspace")
    sink = ActivitySink()
    reader_spec = _reader_spec(config, reader_model, tool_sets.reader, backend, sink)

    spec = _researcher_spec(
        config,
        researcher_model,
        tool_sets.researcher,
        reader_spec,
        backend,
        SourceRegistry(),
        RunLog(),
        sink,
    )

    assert "interrupt_on" not in spec
    # Phase 6 (D-C): registered LAST, so `ToolRetryMiddleware` (outside it) re-invokes the
    # inner handler with the same tool-call id and a retried dispatch is observable as one.
    assert isinstance(spec["middleware"][-1], _ToolActivityMiddleware)


async def test_build_agent_resolves_each_role_from_its_own_key(
    make_config, monkeypatch, scripted_model
):
    """Frozen role keys (Step 2): `build_agent` must resolve the reader `SubAgent` spec's model
    from the `reader` key and request the `researcher` role, not the retired `subagent` key.
    """
    import harness.agent as agent_module

    head_model = scripted_model([AIMessage(content="done")])
    # A real `ScriptedChatModel`, not a bare `object()`: Step 3 wires the researcher into
    # `create_deep_agent`'s own `subagents=` list, which resolves its model via
    # `resolve_model` — a placeholder that is not a `BaseChatModel` fails there now.
    researcher_model = scripted_model([AIMessage(content="researcher done")])
    reader_model = scripted_model([AIMessage(content="reader done")])
    models_by_role = {
        "head": head_model,
        "researcher": researcher_model,
        "reader": reader_model,
        "verifier": object(),
    }
    requested_roles: list[str] = []

    def _by_role(cfg: Any, role: str) -> Any:
        requested_roles.append(role)
        return models_by_role[role]

    monkeypatch.setattr("harness.models.build_chat_model", _by_role)

    captured: dict[str, Any] = {}
    original_reader_spec = agent_module._reader_spec

    def _spy_reader_spec(
        config: Any, reader_model_arg: Any, reader_tools: Any, backend: Any, sink: Any
    ) -> Any:
        captured["reader_model"] = reader_model_arg
        return original_reader_spec(config, reader_model_arg, reader_tools, backend, sink)

    monkeypatch.setattr(agent_module, "_reader_spec", _spy_reader_spec)

    build_agent(make_config(), SourceRegistry())

    assert "researcher" in requested_roles
    assert captured["reader_model"] is reader_model


def test_build_agent_raises_model_error_naming_the_missing_role(make_config):
    """Loud rename (Step 2): a config carrying only the retired `head`/`subagent` roles must
    fail loud with `ModelError` naming the first new role `build_agent` needs, rather than
    silently succeeding against the stale `subagent` key. No `patch_models_by_role` here — the
    REAL `harness.models.build_chat_model` must be the one raising, and it does so before any
    network call for an undeclared role.
    """
    from harness.config import RoleConfig
    from harness.models import ModelError

    config = make_config()
    broken = HarnessConfig(
        providers=config.providers,
        roles={
            "head": config.roles["head"],
            "subagent": RoleConfig(provider="opencode", model="test-model"),
        },
        fetch=config.fetch,
        search=config.search,
        agent=config.agent,
    )

    with pytest.raises(ModelError) as excinfo:
        build_agent(broken, SourceRegistry())

    assert "researcher" in str(excinfo.value)


def test_orchestrator_prompt_names_the_answer_structure_contract():
    """Phase 1 Step 3: the rendered orchestrator prompt must tell the model to write headings
    starting at `## ` (never `# `), lead with a direct answer, and never write its own
    meta/coverage/disclosure sections — the harness demotes/owns all of that.
    """
    from harness.prompts import render

    prompt = render(
        "orchestrator", current_date=date.today().isoformat(), max_concurrent_researchers=4
    )

    assert "start at `## `" in prompt
    assert "never `# `" in prompt
    assert "no meta" in prompt


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
    # A real `ScriptedChatModel`, not a bare `object()`: Step 3 wires the researcher into
    # `create_deep_agent`'s own `subagents=` list, which resolves its model via
    # `resolve_model` — a placeholder that is not a `BaseChatModel` fails there now.
    researcher_model = ScriptedChatModel(
        model="researcher-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([AIMessage(content="researcher done")])
    reader_model = ScriptedChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([AIMessage(content="reader done")])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    build_agent(make_config(), SourceRegistry())

    provider = get_model_provider(reader_model)
    identifier = get_model_identifier(reader_model)
    profile = _get_harness_profile(f"{provider}:{identifier}")

    assert profile is not None
    assert "execute" in profile.excluded_tools


# --- Phase 2 / Step 3: failure path — task-tool retry/error middleware, fetch_raw fallback --


class _RaisingChatModel(ScriptedChatModel):
    """Raises on every call, recording each invocation — drives the task-tool retry path.

    `_failure` is what it raises: the default `RuntimeError` is the generic subagent crash, and
    the D6 tests swap in a deterministic `openai.BadRequestError` or the builtin `TimeoutError`
    to prove those are converted but NOT replayed.
    """

    _failure: Exception = PrivateAttr(default_factory=lambda: RuntimeError("boom"))

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._received_messages.append(list(messages))
        self._call_count += 1
        raise self._failure


def _bad_request_error(message: str = "context length exceeded") -> openai.BadRequestError:
    """An `openai.BadRequestError` built the way the SDK builds one — a 4xx error carries the
    httpx response (and its request) it was raised from."""
    request = httpx.Request("POST", "https://example.test/v1")
    return openai.BadRequestError(message, response=httpx.Response(400, request=request), body=None)


def _raising_researcher(failure: Exception) -> _RaisingChatModel:
    """A researcher-role model that raises `failure` on every call."""
    from pydantic import SecretStr

    model = _RaisingChatModel(
        model="researcher-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    )
    model._failure = failure
    return model


def _task_call(
    description: str, call_id: str = "call_task", subagent_type: str = "researcher"
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": description, "subagent_type": subagent_type},
                "id": call_id,
            }
        ],
    )


async def _run_with_a_failing_researcher(
    make_config, patch_models_by_role, scripted_model, failure: Exception, run_log=None
):
    """Arrange/act shared by the researcher-failure tests (issue #43 #8: the third verbatim
    copy got factored out): a lead that dispatches one researcher whose model raises
    `failure`, run to the lead's next scripted turn. Returns `(result, researcher_model)`
    — what each test asserts about them (retry count, message status, incidents) is the
    part that differs. `run_log` passes through for the tests that assert on incidents.
    """
    head_model = scripted_model(
        [_task_call("Investigate an angle"), AIMessage(content="done despite the failed angle")]
    )
    researcher_model = _raising_researcher(failure)
    reader_model = scripted_model([AIMessage(content="unused")])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    graph = build_agent(make_config(), SourceRegistry(), run_log)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )
    return result, researcher_model


def _reader_fetch_call(url: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "fetch_pages", "args": {"urls": [url]}, "id": "call_fetch"}],
    )


@pytest.fixture
def build_researcher(make_config, tmp_path):
    """Factory: compile a standalone researcher subagent runnable, the same way `build_agent`
    nests it under the lead (Step 3), but invocable directly so a test can drive the
    researcher's OWN task-to-reader dispatch (retry/error middleware, digest marking) without
    also scripting a full lead turn around it.
    """
    from deepagents.backends.filesystem import FilesystemBackend
    from deepagents.middleware.subagents import create_sub_agent

    from harness.agent import _reader_spec, _researcher_spec
    from harness.tools import build_tools

    def _build(
        researcher_model: Any,
        reader_model: Any,
        registry: SourceRegistry | None = None,
        run_log: RunLog | None = None,
        sink: ActivitySink | None = None,
        agent: AgentSettings | None = None,
        max_reader_dispatches: int | None = None,
    ):
        # `agent` lets a caller override `AgentSettings` wholesale. `max_reader_dispatches`
        # (F3 review fix) is the narrower knob a cap test actually needs: it sets the cap
        # WITHOUT reverting `workspace_dir`/`reports_dir` to their real HOME-relative defaults
        # the way handing a bare `AgentSettings(max_reader_dispatches=...)` as `agent` did --
        # that leaked run directories into the developer's real `~/deep-research/workspace/`,
        # since `build_fetch_tool` eagerly `mkdir`s under it. Mirrors `make_config`'s own
        # `tmp_path`-scoped defaults exactly.
        if max_reader_dispatches is not None:
            if agent is not None:
                raise ValueError("pass either `agent` or `max_reader_dispatches`, not both")
            agent = AgentSettings(
                workspace_dir=tmp_path / "workspace",
                reports_dir=tmp_path / "reports",
                max_reader_dispatches=max_reader_dispatches,
            )
        config = make_config(agent=agent)
        registry = registry if registry is not None else SourceRegistry()
        backend = FilesystemBackend(root_dir=tmp_path / "workspace")
        tool_sets = build_tools(config, registry)
        # `sink` defaults internally rather than on the parameter itself (Contracts:
        # `_reader_spec`/`_researcher_spec` take it keyword-or-positional with NO default,
        # so no call site silently forgets one) -- callers that don't care about tool
        # activity, i.e. every pre-Phase-6 test using this fixture, need not pass one. The
        # SAME instance goes to both specs: reader-tier attribution (D-D) needs the reader's
        # own middleware updating the very `ReaderState` the researcher's middleware created.
        shared_sink = sink if sink is not None else ActivitySink()
        reader_spec = _reader_spec(config, reader_model, tool_sets.reader, backend, shared_sink)
        researcher_spec = _researcher_spec(
            config,
            researcher_model,
            tool_sets.researcher,
            reader_spec,
            backend,
            registry,
            run_log if run_log is not None else RunLog(),
            shared_sink,
        )
        return create_sub_agent(researcher_spec), registry

    return _build


async def test_a_reader_crash_becomes_an_error_task_message_after_one_retry(
    build_researcher, scripted_model
):
    """D2 (relocated to the researcher tier, Step 3 — the mechanism, not the semantics, moved):
    the researcher's own `task` tool (dispatching to the reader) is wrapped by
    ToolRetryMiddleware(max_retries=1) inner and ToolErrorMiddleware outer, so a reader crash
    surfaces to the researcher as an error ToolMessage — never a raised exception out of
    `ainvoke` — after exactly one retry (two reader model calls total).
    """
    from pydantic import SecretStr

    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="done"),
        ]
    )
    reader_model = _RaisingChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    )
    researcher, _ = build_researcher(researcher_model, reader_model)

    result = await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    task_messages = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task"
    ]
    assert len(task_messages) == 1
    assert task_messages[0].status == "error"
    assert str(task_messages[0].content).startswith("READER FAILED")
    assert reader_model._call_count == 2  # initial attempt + exactly one retry


def _multi_task_call(
    descriptions: list[str], call_ids: list[str], subagent_type: str = "reader"
) -> AIMessage:
    """Like `_task_call`, but emits several `task` calls in ONE AIMessage — the shape
    `_ReaderDispatchCapMiddleware` must refuse only the surplus of, not the whole batch (see
    the plan's "position, not a count" rationale), and the shape the lead's own fan-out cap
    (`subagent_type="researcher"`) sees as concurrent dispatches.
    """
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": description, "subagent_type": subagent_type},
                "id": call_id,
            }
            for description, call_id in zip(descriptions, call_ids, strict=True)
        ],
    )


async def test_a_dispatch_past_the_cap_is_refused_and_spawns_no_reader(
    build_researcher, scripted_model
):
    """R5 (D6, amended per the parent plan's 2026-08-21 Reconciliations entry):
    `[agent].max_reader_dispatches` is harness-enforced, not merely advised in prompt prose.
    A low cap (2, rather than the default of 6) makes the boundary explicit: the researcher
    emits cap+1 reader dispatches in one AIMessage, and only the surplus is refused.
    """
    cap = 2
    call_ids = [f"call_{i}" for i in range(cap + 1)]
    researcher_model = scripted_model(
        [
            _multi_task_call([f"Fetch and digest facet {i}" for i in range(cap + 1)], call_ids),
            AIMessage(content="done"),
        ]
    )
    reader_model = scripted_model([AIMessage(content=f"digest {i}") for i in range(cap)])
    researcher, _ = build_researcher(researcher_model, reader_model, max_reader_dispatches=cap)

    result = await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    task_messages_by_id = {
        m.tool_call_id: m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.name == "task"
    }
    assert set(task_messages_by_id) == set(call_ids)
    for call_id in call_ids[:cap]:
        assert "budget exhausted" not in str(task_messages_by_id[call_id].content).lower()
    refused = task_messages_by_id[call_ids[cap]]
    assert "budget exhausted" in str(refused.content).lower()
    assert refused.status != "error"  # a budget verdict, not a failure
    # The surplus dispatch never reached the reader subgraph at all.
    assert reader_model._call_count == cap


async def test_a_refused_dispatch_records_a_run_log_incident(build_researcher, scripted_model):
    """Best-effort + disclose: a refused dispatch thins THIS angle's coverage during research,
    and the refusal ToolMessage is seen only by the model — which may summarize it away or
    simply not mention it. The report's `## Gaps and disclosures` is built structurally from
    `RunLog` incidents alone, so without one the loss is invisible in the written artifact.
    Unlike the round cap and wall clock, this bound has no `CutShortReason` to disclose it.
    """
    cap = 1
    call_ids = ["call_0", "call_1"]
    researcher_model = scripted_model(
        [
            _multi_task_call(["Fetch and digest facet 0", "Fetch and digest facet 1"], call_ids),
            AIMessage(content="done"),
        ]
    )
    reader_model = scripted_model([AIMessage(content="digest 0")])
    run_log = RunLog()
    researcher, _ = build_researcher(
        researcher_model, reader_model, run_log=run_log, max_reader_dispatches=cap
    )

    await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    incidents = [i for i in run_log.incidents() if i.kind == "reader_budget_exhausted"]
    assert len(incidents) == 1
    # The configured cap belongs in the detail: an operator reading the report needs to know
    # which knob to raise, not merely that something was refused.
    assert str(cap) in incidents[0].detail


async def test_dispatches_within_the_budget_record_no_incident(build_researcher, scripted_model):
    """The incident marks a REFUSAL, not a dispatch. A researcher that stays inside its budget
    has thinned nothing, so it must not add noise to `## Gaps and disclosures`.
    """
    cap = 2
    call_ids = ["call_0", "call_1"]
    researcher_model = scripted_model(
        [
            _multi_task_call(["Fetch and digest facet 0", "Fetch and digest facet 1"], call_ids),
            AIMessage(content="done"),
        ]
    )
    reader_model = scripted_model([AIMessage(content=f"digest {i}") for i in range(cap)])
    run_log = RunLog()
    researcher, _ = build_researcher(
        researcher_model, reader_model, run_log=run_log, max_reader_dispatches=cap
    )

    await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    assert [i for i in run_log.incidents() if i.kind == "reader_budget_exhausted"] == []


async def test_a_second_researcher_attempt_gets_a_fresh_budget(build_researcher, scripted_model):
    """The count derives from the researcher's OWN message history (Reconciliations,
    2026-08-21), so a fresh `ainvoke` — a new subgraph with a new message history — gets a
    fresh budget: the shared middleware instance holds no cross-attempt tally.
    """
    cap = 2
    attempt_a_ids = ["call_a0", "call_a1"]
    attempt_b_ids = ["call_b0", "call_b1"]
    researcher_model = scripted_model(
        [
            _multi_task_call(["Fetch and digest a0", "Fetch and digest a1"], attempt_a_ids),
            AIMessage(content="done"),
            _multi_task_call(["Fetch and digest b0", "Fetch and digest b1"], attempt_b_ids),
            AIMessage(content="done"),
        ]
    )
    reader_model = scripted_model([AIMessage(content=f"digest {i}") for i in range(4)])
    researcher, _ = build_researcher(researcher_model, reader_model, max_reader_dispatches=cap)

    result_a = await researcher.ainvoke({"messages": [HumanMessage(content="research angle a")]})
    result_b = await researcher.ainvoke({"messages": [HumanMessage(content="research angle b")]})

    for result in (result_a, result_b):
        task_messages = [
            m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task"
        ]
        assert len(task_messages) == cap
        for message in task_messages:
            assert "budget exhausted" not in str(message.content).lower()
    assert reader_model._call_count == 4  # every dispatch in BOTH attempts reached the reader


async def test_a_tool_retry_reinvocation_is_not_double_counted(build_researcher, scripted_model):
    """`ToolRetryMiddleware(max_retries=1)` (`_task_dispatch_guard`) re-invokes the SAME
    `tool_call["id"]` on a reader crash; since the cap counts distinct ids already present in
    the researcher's message history, that re-invocation costs no extra budget. With the cap at
    N and N dispatches (each crashing and retrying, mirroring
    `test_a_reader_crash_becomes_an_error_task_message_after_one_retry`), none is refused.
    """
    from pydantic import SecretStr

    cap = 2
    call_ids = [f"call_{i}" for i in range(cap)]
    researcher_model = scripted_model(
        [
            _multi_task_call([f"Fetch and digest facet {i}" for i in range(cap)], call_ids),
            AIMessage(content="done"),
        ]
    )
    reader_model = _RaisingChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    )
    researcher, _ = build_researcher(researcher_model, reader_model, max_reader_dispatches=cap)

    result = await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    task_messages_by_id = {
        m.tool_call_id: m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.name == "task"
    }
    assert set(task_messages_by_id) == set(call_ids)
    for call_id in call_ids:
        message = task_messages_by_id[call_id]
        assert "budget exhausted" not in str(message.content).lower()
        assert message.status == "error"
        assert str(message.content).startswith("READER FAILED")
    # initial attempt + exactly one retry, for EACH of the `cap` dispatches -- the retry
    # consumed no extra cap budget, only extra reader-model calls.
    assert reader_model._call_count == cap * 2


async def test_a_swallowed_subagent_crash_records_a_run_log_incident(
    build_researcher, scripted_model
):
    """Best-effort + disclose (PR #18 review): converting a crashed dispatch into a
    `... FAILED` ToolMessage keeps the run alive, but only the MODEL saw that message — the
    cause must also reach the shared `RunLog`, which the terminal echoes live and the report
    renders under `## Gaps and disclosures`. Without this a run whose every dispatch died
    reported `incidents=0` and never named why its sources were missing.
    """
    from pydantic import SecretStr

    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="done"),
        ]
    )
    reader_model = _RaisingChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    )
    run_log = RunLog()
    researcher, _ = build_researcher(researcher_model, reader_model, run_log=run_log)

    await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    incidents = run_log.incidents()
    assert len(incidents) == 1
    assert incidents[0].kind == "subagent_failed"
    assert "reader" in incidents[0].detail


async def test_a_reader_ending_with_no_final_text_returns_an_empty_task_message(
    build_researcher, scripted_model
):
    """deepagents' documented empty-ToolMessage behavior: a subagent ending on an AIMessage with
    no text produces a task ToolMessage with empty content. THIS emptiness is the documented
    failure signal the researcher's prompt (Step 3) teaches it to treat as a failed digest.
    """
    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="done"),
        ]
    )
    reader_model = scripted_model([AIMessage(content="")])
    researcher, _ = build_researcher(researcher_model, reader_model)

    result = await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    task_messages = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task"
    ]
    assert len(task_messages) == 1
    assert task_messages[0].content == ""


async def test_a_reader_crash_after_a_successful_fetch_leaves_the_source_unread(
    build_researcher, scripted_model, install_crawler
):
    """R5: "digested" is marked at the delegation boundary, not at fetch time. The reader
    fetches a page successfully, then crashes (its script runs out, raising `IndexError` —
    the crash-after-partial-fetch case); no digest ever reaches the researcher, so the source
    must stay "unread". A fetch-time mark here would make the report disclose a digest that
    never existed.
    """
    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="done"),
        ]
    )
    # One scripted reply only: the fetch call. The reader's next model call — and the whole
    # retry attempt — exhausts the script and raises.
    reader_model = scripted_model([_reader_fetch_call("https://a.test")])
    install_crawler(
        [
            _FakeResult(
                "https://a.test",
                markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body"),
            )
        ]
    )

    # Phase 4 strict provenance (R2): this scenario never calls `search_web`, so the reader's
    # directly-fetched URL must arrive pre-approved.
    registry = SourceRegistry()
    approve_all(registry, ["https://a.test"])
    researcher, registry = build_researcher(researcher_model, reader_model, registry=registry)
    result = await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

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
    build_researcher, scripted_model, install_crawler
):
    """The empty-digest half of the same boundary: the reader fetches successfully but ends
    with no final text — the documented failure signal the prompt tells the researcher to
    treat as a failed delegation — so the source stays "unread" until a `fetch_raw` recovery
    marks it.
    """
    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="done"),
        ]
    )
    reader_model = scripted_model([_reader_fetch_call("https://a.test"), AIMessage(content="")])
    install_crawler(
        [
            _FakeResult(
                "https://a.test",
                markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body"),
            )
        ]
    )

    # Phase 4 strict provenance (R2): this scenario never calls `search_web`, so the reader's
    # directly-fetched URL must arrive pre-approved.
    registry = SourceRegistry()
    approve_all(registry, ["https://a.test"])
    researcher, registry = build_researcher(researcher_model, reader_model, registry=registry)
    await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    source = registry.get("S1")
    assert source is not None
    assert source.read_mode == "unread"


# --- Phase 6: `_ToolActivityMiddleware` / `ActivitySink` (reader/researcher tiers) --------


class _FailOnceChatModel(ScriptedChatModel):
    """Raises on its very first call only, then defers to the real scripted script.

    Drives the "crash then succeed on retry" path -- as opposed to `_RaisingChatModel`
    above, which fails every call. Does not touch `_call_count` on the failing branch, so
    the eventual successful call still reads `self._script[0]`.
    """

    _failed_once: bool = PrivateAttr(default=False)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if not self._failed_once:
            self._failed_once = True
            self._received_messages.append(list(messages))
            raise RuntimeError("boom")
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


async def test_two_parallel_reader_dispatches_get_distinct_ids_and_terminal_transitions(
    build_researcher, scripted_model
):
    """N parallel reader dispatches produce N distinct reader ids, each with a start and a
    terminal transition. Verified BOTH ways (plan `## Discoveries`, Phase 5): against an
    implementation whose reader id is a constant `"reader/1"`, or keyed off the tool name,
    `len(ids) == 2` fails -- so this is a real test of the risk, not a vacuous one.
    """
    from pydantic import SecretStr

    researcher_model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"description": "Angle A", "subagent_type": "reader"},
                        "id": "call_reader_a",
                    },
                    {
                        "name": "task",
                        "args": {"description": "Angle B", "subagent_type": "reader"},
                        "id": "call_reader_b",
                    },
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    reader_model = ConcurrencyTrackingModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([AIMessage(content="Reader A report."), AIMessage(content="Reader B report.")])
    # A real, non-zero yield -- see `ConcurrencyTrackingModel`'s own docstring on why proving
    # CONCURRENCY (as opposed to disproving it) needs one.
    reader_model._sleep_seconds = 0.05

    sink = ActivitySink()
    researcher, _ = build_researcher(researcher_model, reader_model, sink=sink)

    await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    # Empirically confirmed (this phase's implementation notes): two `task(reader)` calls in
    # one researcher turn genuinely run concurrently under this fixture.
    assert reader_model._peak_in_flight > 1

    readers = sink.readers()
    assert len(readers) == 2
    ids = {r.id for r in readers}
    assert len(ids) == 2  # distinct, not both "reader/1" or both keyed off the tool name
    assert all(r.done for r in readers)

    records = sink.records()
    for call_id in ("call_reader_a", "call_reader_b"):
        matching = [r for r in records if r.call_id == call_id]
        assert len(matching) == 2  # one start, one finish
        assert any(r.result_summary is None for r in matching)
        assert any(r.result_summary is not None for r in matching)


async def test_a_failed_reader_is_a_failed_row_not_a_stuck_live_one(
    build_researcher, scripted_model
):
    """The reader-crash path (existing test above) re-run with a sink attached: the error
    `ToolMessage` and the `RunLog` `subagent_failed` incident are UNCHANGED, and the reader's
    own row is `done` with a failed status rather than left `running...` forever -- risk #3's
    "no change to failure semantics" pin.
    """
    from pydantic import SecretStr

    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="done"),
        ]
    )
    reader_model = _RaisingChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    )
    run_log = RunLog()
    sink = ActivitySink()
    researcher, _ = build_researcher(researcher_model, reader_model, run_log=run_log, sink=sink)

    result = await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    # Unchanged from `test_a_reader_crash_becomes_an_error_task_message_after_one_retry`.
    task_messages = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task"
    ]
    assert len(task_messages) == 1
    assert task_messages[0].status == "error"
    assert str(task_messages[0].content).startswith("READER FAILED")
    # Unchanged from `test_a_swallowed_subagent_crash_records_a_run_log_incident`.
    incidents = run_log.incidents()
    assert len(incidents) == 1
    assert incidents[0].kind == "subagent_failed"

    readers = sink.readers()
    assert len(readers) == 1
    assert readers[0].done is True
    assert "failed" in readers[0].status_text


async def test_a_retried_reader_dispatch_is_flagged_in_the_sink_records(
    build_researcher, scripted_model
):
    """A crash-then-succeed script: the researcher's `task` dispatch to the reader fails
    once and succeeds on retry (`ToolRetryMiddleware` re-invoking with the SAME `call_id`),
    so the sink shows two records for that one call id -- `retry=False` then `retry=True`.
    """
    from pydantic import SecretStr

    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="done"),
        ]
    )
    reader_model = _FailOnceChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([AIMessage(content="Recovered digest.")])
    sink = ActivitySink()
    researcher, _ = build_researcher(researcher_model, reader_model, sink=sink)

    await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    records = [r for r in sink.records() if r.call_id == "call_task"]
    assert len(records) == 4  # start+finish for the failed attempt, start+finish for the retry
    assert [r.retry for r in records] == [False, False, True, True]


async def test_a_reader_tier_tool_call_attributes_to_its_reader(
    build_researcher, scripted_model, install_crawler
):
    """A reader-tier `fetch_pages` call, while live, names itself on its OWN reader's row
    (D-D's `ContextVar` attribution) -- not some other reader's, and not left generic.
    """
    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="done"),
        ]
    )
    reader_model = scripted_model(
        [_reader_fetch_call("https://a.test"), AIMessage(content="Digest of the page.")]
    )
    install_crawler(
        [
            _FakeResult(
                "https://a.test",
                markdown=_FakeMarkdown(raw_markdown="A body", fit_markdown="A body"),
            )
        ]
    )
    registry = SourceRegistry()
    approve_all(registry, ["https://a.test"])

    sink = ActivitySink()
    # `note_reader_tool` is the exact moment the reader's row gets attributed -- capture a
    # snapshot right then, since by the time `ainvoke` returns the reader is `done` and its
    # status text has moved on to a "done"/"failed" shape, not the live "fetch_pages" one.
    live_snapshots: list[tuple] = []
    original_note = sink.note_reader_tool

    def _capturing_note(reader_id, tool):
        original_note(reader_id, tool)
        live_snapshots.append(sink.readers())

    sink.note_reader_tool = _capturing_note

    researcher, _ = build_researcher(researcher_model, reader_model, registry=registry, sink=sink)
    await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    assert live_snapshots, "note_reader_tool was never called -- attribution never fired"
    live_readers = live_snapshots[0]
    assert len(live_readers) == 1
    assert live_readers[0].done is False
    assert "fetch_pages" in live_readers[0].status_text


async def test_two_concurrent_readers_attribute_their_own_tool_calls_without_cross_talk(
    build_researcher, scripted_model, install_crawler
):
    """Fix-pass item 6: risk #3's own question is whether the reader scope can leak ACROSS
    concurrent dispatches -- only a single-reader attribution test existed before. Two
    `task(reader)` dispatches run concurrently, each making its own distinct reader-tier
    `fetch_pages` call to a distinct URL; `active_reader()` is captured at the moment each
    call starts, so each URL must land on its OWN, DIFFERENT reader id -- never the other's,
    and never `None`. Verified BOTH ways, per this file's report: swapping the ContextVar for
    a plain shared attribute makes this fail before it is trusted green.
    """
    from pydantic import SecretStr

    researcher_model = scripted_model(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"description": "Angle A", "subagent_type": "reader"},
                        "id": "call_reader_a",
                    },
                    {
                        "name": "task",
                        "args": {"description": "Angle B", "subagent_type": "reader"},
                        "id": "call_reader_b",
                    },
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    reader_model = ConcurrencyTrackingModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "fetch_pages",
                        "args": {"urls": ["https://a.test"]},
                        "id": "call_fetch_a",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "fetch_pages",
                        "args": {"urls": ["https://b.test"]},
                        "id": "call_fetch_b",
                    }
                ],
            ),
            AIMessage(content="Digest one."),
            AIMessage(content="Digest two."),
        ]
    )
    # A real, non-zero yield, same reasoning as the parallel-ids test above.
    reader_model._sleep_seconds = 0.05
    install_crawler(
        [
            _FakeResult(
                "https://a.test", markdown=_FakeMarkdown(raw_markdown="A", fit_markdown="A")
            ),
            _FakeResult(
                "https://b.test", markdown=_FakeMarkdown(raw_markdown="B", fit_markdown="B")
            ),
        ]
    )
    registry = SourceRegistry()
    approve_all(registry, ["https://a.test", "https://b.test"])

    sink = ActivitySink()
    attributions: list[tuple[str, str | None]] = []
    original_start_call = sink.start_call

    def _capturing_start_call(call_id, tool, arg_summary):
        if tool == "fetch_pages":
            attributions.append((arg_summary, active_reader()))
        return original_start_call(call_id, tool, arg_summary)

    sink.start_call = _capturing_start_call

    researcher, _ = build_researcher(researcher_model, reader_model, registry=registry, sink=sink)
    await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    assert reader_model._peak_in_flight > 1  # genuine concurrency, not a serial fallback
    assert len(attributions) == 2
    by_url = dict(attributions)
    assert set(by_url) == {"https://a.test", "https://b.test"}
    reader_ids = set(by_url.values())
    assert None not in reader_ids
    assert len(reader_ids) == 2  # each call attributed to its OWN reader, never the other's


async def test_a_researcher_crash_becomes_an_error_task_message_after_one_retry(
    make_config, patch_models_by_role, scripted_model
):
    """TEST-FIRST item 2: the lead's OWN `task` tool (dispatching to the researcher) carries
    the same ToolRetryMiddleware(max_retries=1)/ToolErrorMiddleware pair, now guarding a
    researcher crash — the lead receives a `RESEARCHER FAILED (...)` error ToolMessage after
    exactly one retry, and the run continues on the lead's next scripted turn.
    """
    result, researcher_model = await _run_with_a_failing_researcher(
        make_config, patch_models_by_role, scripted_model, RuntimeError("boom")
    )

    task_messages = _lead_task_messages(result)
    assert list(task_messages) == ["call_task"]
    assert task_messages["call_task"].status == "error"
    assert str(task_messages["call_task"].content).startswith("RESEARCHER FAILED")
    assert researcher_model._call_count == 2  # initial attempt + exactly one retry
    # The run continued past the failed dispatch onto the lead's next scripted turn.
    assert result["messages"][-1].content == "done despite the failed angle"


def test_retry_predicate_replays_only_transient_failures():
    """D6/R6: replaying a whole researcher is only worth paying for when the failure might come
    out differently. `openai.BadRequestError` (context length, malformed request) is
    deterministic and the builtin `TimeoutError` covers a timeout raised inside a dispatch
    (a model call's own timeout), so neither is replayed. `openai.APITimeoutError` is
    deliberately still retryable — it subclasses `APIConnectionError` and a re-request may
    well succeed.
    """
    request = httpx.Request("POST", "https://example.test/v1")

    assert _retry_on_non_search_abort(_bad_request_error()) is False
    assert _retry_on_non_search_abort(TimeoutError()) is False
    assert _retry_on_non_search_abort(openai.APIConnectionError(request=request)) is True
    assert _retry_on_non_search_abort(openai.APITimeoutError(request=request)) is True
    assert _retry_on_non_search_abort(RuntimeError("boom")) is True


def test_the_transient_timeout_types_this_stack_raises_are_still_retried():
    """Issue #43 #9's low-confidence note, resolved: since Python 3.10 `socket.timeout` IS the
    builtin `TimeoutError` (non-retryable here), so a bare one could hide a genuinely
    transient OS-level network failure. It cannot reach this predicate from the network —
    verified against the dependency tree, every stack raises its own timeout type, none
    subclassing the builtin: httpx's `TimeoutException` family (the transport under both
    direct fetches and the openai SDK) and the SDK's `APITimeoutError`. Those must stay
    retryable — this pin is what breaks if a future dependency leaks a bare builtin
    `TimeoutError` from its network path instead of wrapping it.
    """
    assert _retry_on_non_search_abort(httpx.ConnectTimeout("timed out")) is True
    assert _retry_on_non_search_abort(httpx.ReadTimeout("timed out")) is True


async def test_a_researcher_bad_request_is_not_replayed(
    make_config, patch_models_by_role, scripted_model
):
    """D6: the non-retryable tuple is a SUPERSET of the pass-through one, not the same tuple —
    a deterministic `BadRequestError` is still converted to a soft `RESEARCHER FAILED (...)`
    ToolMessage and the run continues, it is just never replayed through a second whole
    researcher that would fail identically.
    """
    result, researcher_model = await _run_with_a_failing_researcher(
        make_config, patch_models_by_role, scripted_model, _bad_request_error()
    )

    task_messages = _lead_task_messages(result)
    assert list(task_messages) == ["call_task"]
    assert task_messages["call_task"].status == "error"
    assert str(task_messages["call_task"].content).startswith("RESEARCHER FAILED")
    assert "BadRequestError" in str(task_messages["call_task"].content)
    assert researcher_model._call_count == 1  # converted, but NOT retried
    assert result["messages"][-1].content == "done despite the failed angle"


async def test_lead_to_researcher_to_reader_digest_reaches_the_lead(
    make_config, patch_models_by_role, scripted_model, install_crawler
):
    """TEST-FIRST item 1: the full 3-tier chain, scripted end to end — the lead dispatches a
    researcher, the researcher dispatches a reader, the reader's digest reaches the researcher,
    the researcher's own report reaches the lead, and the digested source is marked `digested`
    (R7's mechanism moved, not broken, by nesting it one level deeper).
    """
    head_model = scripted_model(
        [
            _task_call("Investigate the widget defect angle"),
            AIMessage(content="Final answer citing the researcher's finding [S1]."),
        ]
    )
    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="Researcher report: the page confirms the defect [S1]."),
        ]
    )
    reader_model = scripted_model(
        [
            _reader_fetch_call("https://a.test"),
            AIMessage(content="Digest: confirmed defect pattern [S1]."),
        ]
    )
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )
    install_crawler(
        [
            _FakeResult(
                "https://a.test",
                markdown=_FakeMarkdown(raw_markdown="Defect body", fit_markdown="Defect body"),
            )
        ]
    )

    # Phase 4 strict provenance (R2): this scenario never calls `search_web`, so the reader's
    # directly-fetched URL must arrive pre-approved.
    registry = SourceRegistry()
    approve_all(registry, ["https://a.test"])
    graph = build_agent(make_config(), registry)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    assert result["messages"][-1].content == "Final answer citing the researcher's finding [S1]."
    source = registry.get("S1")
    assert source is not None
    assert source.read_mode == "digested"


async def test_two_researchers_dispatched_in_one_turn_run_concurrently(
    make_config, patch_models_by_role
):
    """Acceptance criterion: two researchers dispatched by ONE lead turn (a single AIMessage
    carrying two `task` tool calls) actually run concurrently, not sequentially — peak in-flight
    researcher model calls > 1.
    """
    from pydantic import SecretStr

    head_model = ScriptedChatModel(
        model="head-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"description": "Angle A", "subagent_type": "researcher"},
                        "id": "call_a",
                    },
                    {
                        "name": "task",
                        "args": {"description": "Angle B", "subagent_type": "researcher"},
                        "id": "call_b",
                    },
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    researcher_model = ConcurrencyTrackingModel(
        model="researcher-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([AIMessage(content="Report A."), AIMessage(content="Report B.")])
    # A real, non-zero yield — see the class docstring on why proving CONCURRENCY needs it.
    researcher_model._sleep_seconds = 0.05
    reader_model = ScriptedChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([AIMessage(content="unused")])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    graph = build_agent(make_config(), SourceRegistry())
    await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    assert researcher_model._peak_in_flight > 1
    assert researcher_model._call_count == 2


def _lead_task_messages(result) -> dict[str, ToolMessage]:
    """The lead's `task` result ToolMessages, keyed by the `tool_call_id` they answer.

    Same shape the reader-cap tests index by: a refusal and a real digest are both ordinary
    ToolMessages, so only the id ties a verdict back to the dispatch that earned it.
    """
    return {
        m.tool_call_id: m
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.name == "task"
    }


def _researcher_models(sleep_seconds: float, replies: list[str]):
    """A `ConcurrencyTrackingModel` researcher plus an unused reader, the pair every lead-tier
    dispatch test below needs (`patch_models_by_role` wants all three roles).
    """
    from pydantic import SecretStr

    researcher_model = ConcurrencyTrackingModel(
        model="researcher-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    )
    researcher_model._sleep_seconds = sleep_seconds
    researcher_model.script([AIMessage(content=reply) for reply in replies])
    reader_model = ScriptedChatModel(
        model="reader-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([AIMessage(content="unused")])
    return researcher_model, reader_model


async def test_a_researcher_dispatch_past_the_deadline_is_refused_and_spawns_nothing(
    make_config, patch_models_by_role, scripted_model
):
    """R5/D1: once the synthesis reserve is reached, a NEW researcher dispatch never starts.
    The lead gets a plain (non-error) ToolMessage telling it to answer now, and the run log
    carries `research_deadline_reached` so the report can disclose the thinned coverage.
    """
    head_model = scripted_model(
        [_task_call("Investigate an angle"), AIMessage(content="answer from what I have")]
    )
    researcher_model, reader_model = _researcher_models(0.0, ["Report A."])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    deadline = ResearchDeadline()
    deadline.arm(asyncio.get_running_loop().time() - 1)  # already past
    run_log = RunLog()
    graph = build_agent(make_config(), SourceRegistry(), run_log, deadline=deadline)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    refused = _lead_task_messages(result)["call_task"]
    assert "time budget" in str(refused.content).lower()
    assert refused.status != "error"  # a budget verdict, not a failure
    assert researcher_model._started_count == 0  # no subagent was spawned at all
    incidents = [i for i in run_log.incidents() if i.kind == "research_deadline_reached"]
    assert len(incidents) == 1


async def test_an_unarmed_deadline_never_cancels_a_researcher_dispatch(
    make_config, make_agent_settings, patch_models_by_role, scripted_model
):
    """`synthesis_margin_seconds = 0` disables the reserve (`_research_reserve` returns None),
    so the dispatch middleware never arms the deadline and must be a pure pass-through — a
    slow researcher runs to completion rather than being cut off by an implicit zero budget.
    The margin-0 config is explicit: `make_config()`'s default margin would self-arm.
    """
    head_model = scripted_model([_task_call("Investigate an angle"), AIMessage(content="done")])
    researcher_model, reader_model = _researcher_models(0.05, ["Report A."])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    deadline = ResearchDeadline()
    run_log = RunLog()
    config = make_config(agent=make_agent_settings())
    graph = build_agent(config, SourceRegistry(), run_log, deadline=deadline)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    settled = _lead_task_messages(result)["call_task"]
    assert "time budget" not in str(settled.content).lower()
    assert researcher_model._call_count == 1
    assert deadline.remaining() is None  # never armed: no reserve to enforce
    assert [i for i in run_log.incidents() if i.kind == "research_deadline_reached"] == []


async def test_the_middleware_arms_an_unarmed_deadline_at_the_first_dispatch(
    make_config, make_agent_settings, patch_models_by_role, scripted_model
):
    """Issue #43 #2: the deadline is armed by the middleware itself, at the first researcher
    dispatch it wraps — not by `__main__`'s stream consumer, which can lose the race against
    the tools superstep starting. After one dispatch has run, an unarmed deadline is armed
    `wall_clock - margin` out.
    """
    head_model = scripted_model([_task_call("Investigate an angle"), AIMessage(content="done")])
    researcher_model, reader_model = _researcher_models(0.0, ["Report A."])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    deadline = ResearchDeadline()
    config = make_config(
        agent=make_agent_settings(wall_clock_seconds=600, synthesis_margin_seconds=100)
    )
    graph = build_agent(config, SourceRegistry(), RunLog(), deadline=deadline)
    await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    remaining = deadline.remaining()
    assert remaining is not None
    # Armed at the first dispatch, ~500s out (wall 600 - margin 100), minus negligible run time.
    assert 450 < remaining <= 500


async def test_a_first_wave_dispatch_is_bounded_even_when_nothing_else_armed_the_deadline(
    make_config, make_agent_settings, patch_models_by_role, scripted_model
):
    """The heart of issue #43 #2: the same dispatch that arms the deadline already runs under
    its `wait_for`, so the first wave can never run unbounded — the state a stream consumer
    that lost the race against the tools superstep would otherwise leave the run in. The
    reserve (1s of a 2s wall clock) is shorter than the researcher's run, so the dispatch
    must be cancelled at the reserve and refused, never completed.
    """
    head_model = scripted_model(
        [_task_call("Investigate an angle"), AIMessage(content="answer from what I have")]
    )
    researcher_model, reader_model = _researcher_models(2.0, ["Report A."])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    deadline = ResearchDeadline()
    run_log = RunLog()
    config = make_config(
        agent=make_agent_settings(wall_clock_seconds=2, synthesis_margin_seconds=1)
    )
    graph = build_agent(config, SourceRegistry(), run_log, deadline=deadline)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    settled = _lead_task_messages(result)["call_task"]
    assert "time budget" in str(settled.content).lower()
    assert settled.status != "error"  # a budget verdict, not a failure
    assert researcher_model._started_count == 1  # spawned, then cancelled mid-run
    assert deadline.remaining() is not None and deadline.remaining() <= 0
    kinds = [i.kind for i in run_log.incidents()]
    assert "research_deadline_reached" in kinds


async def test_a_model_timeout_inside_an_unarmed_dispatch_is_not_reported_as_deadline_reached(
    make_config, patch_models_by_role, scripted_model
):
    """Phase 1 Discovery: `_ResearcherDispatchMiddleware`'s `except TimeoutError` is scoped to
    the `asyncio.wait_for` branch, so on an UNARMED run a `TimeoutError` raised by the model
    call itself is an ordinary subagent failure — `subagent_failed`, never
    `research_deadline_reached` on a run that has no deadline to reach. And per D6 it is not
    replayed.
    """
    run_log = RunLog()
    result, researcher_model = await _run_with_a_failing_researcher(
        make_config, patch_models_by_role, scripted_model, TimeoutError(), run_log=run_log
    )

    settled = _lead_task_messages(result)["call_task"]
    assert settled.status == "error"
    assert str(settled.content).startswith("RESEARCHER FAILED")
    assert researcher_model._call_count == 1  # TimeoutError is non-retryable (D6)
    kinds = [i.kind for i in run_log.incidents()]
    assert "subagent_failed" in kinds
    assert "research_deadline_reached" not in kinds
    assert result["messages"][-1].content == "done despite the failed angle"


async def test_a_cancelled_researcher_dispatch_is_refused_once_and_never_replayed(
    make_config, patch_models_by_role, scripted_model
):
    """The deadline middleware sits OUTSIDE `_task_dispatch_guard` (D1), so a dispatch it
    cancels is never handed to `ToolRetryMiddleware` — replaying a whole researcher after the
    time budget ran out is precisely the compounding overrun this exists to stop. The lead's
    next turn must still run, so the cancelled dispatch cannot leave the thread wedged.
    """
    head_model = scripted_model(
        [_task_call("Investigate an angle"), AIMessage(content="answer from what I have")]
    )
    researcher_model, reader_model = _researcher_models(5.0, ["Report A.", "Report A again."])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    deadline = ResearchDeadline()
    # A full second, not a few ms: compiling the graph and reaching the first dispatch costs
    # real time, and too tight a budget refuses the dispatch BEFORE it starts -- which is the
    # other test's case, not this one's.
    deadline.arm(asyncio.get_running_loop().time() + 1.0)
    run_log = RunLog()
    graph = build_agent(make_config(), SourceRegistry(), run_log, deadline=deadline)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    task_messages = [
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task"
    ]
    assert len(task_messages) == 1  # exactly one verdict for the one dispatch
    assert "time budget" in str(task_messages[0].content).lower()
    assert task_messages[0].status != "error"
    # STARTED, not completed: the cancelled call never reached `_call_count`. A retry would
    # have started the researcher a second time.
    assert researcher_model._started_count == 1
    assert [i.kind for i in run_log.incidents()] == ["research_deadline_reached"]
    assert result["messages"][-1].content == "answer from what I have"


async def test_the_surplus_concurrent_researcher_is_refused_and_a_later_wave_is_allowed(
    make_config, patch_models_by_role, scripted_model, make_agent_settings
):
    """D5: `[agent].max_concurrent_researchers` is a CONCURRENCY cap enforced in code, not a
    total for the run. Three dispatches in one AIMessage under a cap of 2 leave exactly one
    refused; once the first wave returns, a second wave of two runs normally.
    """
    cap = 2
    wave_a = ["call_a0", "call_a1", "call_a2"]
    wave_b = ["call_b0", "call_b1"]
    head_model = scripted_model(
        [
            _multi_task_call([f"Angle a{i}" for i in range(3)], wave_a, "researcher"),
            _multi_task_call([f"Angle b{i}" for i in range(2)], wave_b, "researcher"),
            AIMessage(content="done"),
        ]
    )
    researcher_model, reader_model = _researcher_models(0.05, [f"Report {i}." for i in range(4)])
    patch_models_by_role(
        {"head": head_model, "researcher": researcher_model, "reader": reader_model}
    )

    run_log = RunLog()
    config = make_config(agent=make_agent_settings(max_concurrent_researchers=cap))
    graph = build_agent(config, SourceRegistry(), run_log)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="research this")]},
        config={"configurable": {"thread_id": "test-thread"}},
    )

    by_id = _lead_task_messages(result)
    assert set(by_id) == set(wave_a) | set(wave_b)
    refused = [
        call_id
        for call_id, message in by_id.items()
        if "already in flight" in str(message.content).lower()
    ]
    assert refused == [wave_a[cap]]  # only the surplus of the FIRST wave
    assert by_id[wave_a[cap]].status != "error"  # not a failure `_task_dispatch_guard` retries
    assert researcher_model._peak_in_flight == cap
    assert researcher_model._call_count == 4  # 2 in the first wave + 2 in the second
    incidents = [i for i in run_log.incidents() if i.kind == "researcher_budget_exhausted"]
    assert len(incidents) == 1
    assert str(cap) in incidents[0].detail


async def test_fetch_raw_is_offered_to_the_researcher_but_not_the_lead_or_reader(
    noop_agent, make_config
):
    """`fetch_raw` moved off the lead onto the researcher (Step 3) — the digest-recovery loop
    belongs to whoever dispatches readers.
    """
    _, graph = noop_agent
    assert "fetch_raw" not in _tools_by_name(graph)

    from harness.tools import build_tools

    config = make_config()
    tool_sets = build_tools(config, SourceRegistry())
    assert "fetch_raw" in [t.name for t in tool_sets.researcher]
    assert "fetch_raw" not in [t.name for t in tool_sets.reader]


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
    model = scripted_model([plan_call, final])
    patch_run(monkeypatch, config, model)

    await main_module.main(["What is the capital of France?"])

    out = capsys.readouterr().out
    assert "Search for the answer" in out


async def test_run_outcome_records_token_usage_summed_with_reasoning_split(
    make_config, monkeypatch, scripted_model
):
    config = make_config()
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
    model = scripted_model([round_one, final])
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
    # Phase 1: `harness.browser.BrowserSession` is imported by value into `main_module` at
    # module import time (Step 5's cheap-import treatment, matching `preflight_search` —
    # conftest.py:331-334), so patching the class object itself (same object either way it's
    # accessed) reaches the instance `main()` builds without needing a `main_module` name.
    from harness.browser import BrowserSession

    closed: list[bool] = []
    original_close = BrowserSession.close

    async def _spy_close(self: BrowserSession) -> None:
        closed.append(True)
        await original_close(self)

    monkeypatch.setattr(BrowserSession, "close", _spy_close)

    config = make_config()
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([final])
    patch_run(monkeypatch, config, model)

    await main_module.main(["a question with no tool calls"])

    _, lines = drain_stdout(capsys)
    assert lines, "main() printed nothing"
    printed_path = lines[-1].strip()
    assert printed_path.endswith(".md")
    assert Path(printed_path).exists()
    assert Path(printed_path).parent == config.agent.reports_dir
    assert closed, "BrowserSession.close() was never reached on the normal exit path"


async def test_main_exits_nonzero_and_writes_no_report_when_browser_preflight_fails(
    make_config, monkeypatch, scripted_model, capsys
):
    """R1: a Chromium launch failure fails fast, before any run or report."""
    # Same value-import reasoning as the teardown test above.
    from harness.browser import BrowserPreflightError, BrowserSession

    config = make_config()
    model = scripted_model([AIMessage(content="unused — the browser preflight aborts first")])
    patch_run(monkeypatch, config, model, skip_preflight=True)

    # Patched AFTER `patch_run`: monkeypatch applies in call order, so this patch — which must
    # actually raise — wins over `patch_run`'s own default neutralization of `start`.
    async def _raise_preflight(self: BrowserSession) -> None:
        raise BrowserPreflightError("Chromium could not be launched: boom (try crawl4ai-setup)")

    monkeypatch.setattr(BrowserSession, "start", _raise_preflight)

    exit_code = await main_module.main(["a question"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err
    assert "chromium" in captured.err.lower()
    assert not config.agent.reports_dir.exists() or not any(config.agent.reports_dir.iterdir())


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


@pytest.mark.parametrize("missing_role", ["researcher", "reader", "verifier"])
async def test_main_exits_nonzero_when_a_preflighted_role_is_not_declared(
    make_config, monkeypatch, capsys, missing_role
):
    """Startup preflights every role the run will call (`_PREFLIGHT_ROLES` — all four since
    the PR #18 review); a config missing any of them fails the same clean ModelError path as
    a missing `head` would, never a traceback under the TUI or a mid-run surprise.

    Deliberately bypasses `patch_run`/`patch_model`: those fake `build_chat_model` to ignore
    `role` entirely, which would hide the very check under test here. Instead the REAL
    `preflight`/`build_chat_model` run, with only the `ChatOpenAI` transport faked (mirroring
    `tests/test_models.py`) so the earlier roles' real preflight pings never leave the process.
    """
    import harness.models as models_module

    config = make_config()
    broken = HarnessConfig(
        providers=config.providers,
        roles={name: role for name, role in config.roles.items() if name != missing_role},
        fetch=config.fetch,
        search=config.search,
        agent=config.agent,
    )
    monkeypatch.setattr(main_module, "load_config", lambda: broken)

    real_chat_openai = models_module.ChatOpenAI

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def _factory(**kwargs: Any) -> Any:
        kwargs["http_async_client"] = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        return real_chat_openai(**kwargs)

    monkeypatch.setattr(models_module, "ChatOpenAI", _factory)

    exit_code = await main_module.main(["a question"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err
    assert missing_role in captured.err


async def test_main_reports_a_model_error_from_build_agent_cleanly(
    make_config, monkeypatch, scripted_model, capsys
):
    """Defense in depth behind the preflight loop (PR #18 review): `build_agent` resolves
    every role through `build_chat_model`, and a `ModelError` it raises — a TODO placeholder,
    a role preflight missed — must land on the same close/print/exit-1 shape as a preflight
    failure, never escape `main()` as a traceback under the alternate screen.
    """
    from harness.models import ModelError

    config = make_config()
    model = scripted_model([AIMessage(content="unused — build_agent raises first")])
    patch_run(monkeypatch, config, model)

    def _broken_build_agent(*args: Any, **kwargs: Any) -> Any:
        raise ModelError("role 'reader' is not declared in [roles]")

    # At the source module: `main` imports `build_agent` at call time (heavy-import deferral).
    monkeypatch.setattr("harness.agent.build_agent", _broken_build_agent)

    exit_code = await main_module.main(["a question"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err
    assert "reader" in captured.err
    assert "Traceback" not in captured.err
    assert not config.agent.reports_dir.exists() or not any(config.agent.reports_dir.iterdir())


async def test_main_closes_the_browser_session_when_build_agent_raises_a_model_error(
    make_config, monkeypatch, scripted_model, capsys
):
    """The Phase 1 Discoveries teardown site: `build_agent`'s `ModelError` early exit
    (`__main__.py:805-810`) must close the session too, not only the final `finally` — mirrors
    `test_main_prints_the_report_path_as_the_final_line_of_stdout`'s close-spy shape above.
    """
    from harness.browser import BrowserSession
    from harness.models import ModelError

    closed: list[bool] = []
    original_close = BrowserSession.close

    async def _spy_close(self: BrowserSession) -> None:
        closed.append(True)
        await original_close(self)

    monkeypatch.setattr(BrowserSession, "close", _spy_close)

    config = make_config()
    model = scripted_model([AIMessage(content="unused — build_agent raises first")])
    patch_run(monkeypatch, config, model)

    def _broken_build_agent(*args: Any, **kwargs: Any) -> Any:
        raise ModelError("role 'reader' is not declared in [roles]")

    monkeypatch.setattr("harness.agent.build_agent", _broken_build_agent)

    exit_code = await main_module.main(["a question"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err
    assert closed, "BrowserSession.close() was never reached on the build_agent ModelError path"


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


def test_final_answer_skips_a_tool_calling_message_even_when_it_has_prose():
    """R5: the failed 1800s run published the lead's PLANNING PREAMBLE as its answer — an
    `AIMessage` that says "I will now search..." and carries `tool_calls` in the same message.
    Content alone cannot distinguish that from a real answer; the presence of `tool_calls` can.
    """
    preamble_only = [
        AIMessage(
            content="I will now search for the relevant sources and delegate a researcher.",
            tool_calls=[
                {"name": "task", "args": {"description": "Angle A"}, "id": "c1"},
            ],
        ),
        ToolMessage(content="Researcher report.", tool_call_id="c1"),
    ]

    assert main_module._final_answer(preamble_only) == ""

    with_a_real_answer = [
        AIMessage(content="Acme is cheapest at $4.20/unit [S1]."),
        AIMessage(
            content="Let me check one more angle before finishing.",
            tool_calls=[{"name": "task", "args": {"description": "Angle B"}, "id": "c2"}],
        ),
        ToolMessage(content="Researcher report.", tool_call_id="c2"),
    ]

    assert main_module._final_answer(with_a_real_answer) == "Acme is cheapest at $4.20/unit [S1]."


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

    answer = await asyncio.wait_for(main_module._read_answer(PlainRenderer()), timeout=5)

    assert answer == ""


async def test_read_answer_resolves_when_stdin_raises_oserror(monkeypatch):
    """Same guard, for a detached-stdin `OSError` rather than a clean EOF."""

    def fake_input(prompt: str = "") -> str:
        raise OSError("Bad file descriptor")

    monkeypatch.setattr("builtins.input", fake_input)

    assert await asyncio.wait_for(main_module._read_answer(PlainRenderer()), timeout=5) == ""


async def test_the_clarification_prompt_never_reaches_stdout(monkeypatch, capsys):
    """The report path is the final line of STDOUT, frozen because R1 depends on it. `input(prompt)`
    writes with no trailing newline, so the path landed on the same line as a pending `> `; the
    prompt belongs on stderr with the rest of the terminal chatter.
    """
    monkeypatch.setattr("builtins.input", lambda: "the metal")

    answer = await main_module._read_answer(PlainRenderer(), "> ")

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

    await main_module._read_answer(PlainRenderer())

    assert recorded.get("daemon") is True


async def test_read_answer_returns_what_was_typed(monkeypatch):
    """The thread-based read must not mangle the answer, and `_answer_questions`' normalization
    must still see it whole.
    """
    monkeypatch.setattr("builtins.input", lambda prompt="": "  Yes, region EU-West  ")
    interrupt = Interrupt(value={"action_requests": [{"args": {"question": "Which region?"}}]})
    renderer = PlainRenderer()

    decisions = await main_module._answer_questions(
        interrupt, renderer, SourceRegistry(), StageTracker(renderer)
    )

    assert decisions == [{"type": "respond", "message": "Yes, region EU-West"}]


async def test_a_url_pasted_into_a_clarifying_answer_becomes_fetchable(monkeypatch):
    """Phase 4 (R2): an answer is user-supplied text like the question itself, so a URL pasted
    into it must be approved — the natural reply to "which page do you mean?" is that URL, and
    without approval every later fetch of it is provenance_rejected.
    """
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": "this one: https://example.test/docs/page"
    )
    interrupt = Interrupt(value={"action_requests": [{"args": {"question": "Which page?"}}]})
    registry = SourceRegistry()
    renderer = PlainRenderer()

    await main_module._answer_questions(interrupt, renderer, registry, StageTracker(renderer))

    assert registry.is_approved("https://example.test/docs/page")


async def test_main_cuts_the_run_short_at_the_round_cap(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """`max_rounds=1` with a model that never stops proposing tool calls forces the stream
    loop's own turn counter to end the run rather than the graph terminating on its own.
    `ScriptedChatModel` raises `IndexError` when its script runs out, so far more responses are
    scripted than the cap (plus its bounded synthesis pass) can consume — proving the cap, not
    an exhausted script, is what ended it.
    """
    agent = AgentSettings(
        max_rounds=1, workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
    )
    config = make_config(agent=agent)
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
    model = scripted_model([*([keep_going] * 20)])
    patch_run(monkeypatch, config, model)

    exit_code = await main_module.main(["question that never settles"])

    out, lines = drain_stdout(capsys)
    assert exit_code == 0
    assert lines, "main() printed no report path"
    report_path = Path(lines[-1].strip())
    assert report_path.exists()
    # A run that consumed the whole 20-item script would have driven far more model calls:
    # this allows the capped round plus the bounded synthesis pass, nothing like 21.
    assert len(model._received_messages) < 8
    body = report_path.read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    # Names the ROUND CAP specifically: without this, swapping the `GraphRecursionError` and
    # `TimeoutError` labels in `__main__`'s except clauses would keep every cut-short test green.
    assert _ROUND_CAP_TEXT in body
    assert _WALL_CLOCK_TEXT not in body


async def test_a_clarification_on_the_capped_round_is_still_asked(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """An `ask_user` landing exactly on `max_rounds` must reach the developer, not be skipped.

    `_note_model_turns` sets `cap_hit` for ANY tool call on the capped round, `ask_user`
    included, but that tool pauses the graph on an interrupt instead of returning a
    `ToolMessage`. Handling the cap first `break`s past the interrupt check, so the question
    was dropped and the synthesis pass then resumed a paused thread — which failed the run and
    wrote no report at all, for what should have been an ordinary clarifying question.
    """
    agent = AgentSettings(
        max_rounds=1, workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
    )
    config = make_config(agent=agent)
    ask = AIMessage(
        content="",
        tool_calls=[{"name": "ask_user", "args": {"question": "Which region?"}, "id": "call_1"}],
    )
    final = AIMessage(
        content="Final answer after the clarification.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ask, *([final] * 5)])
    patch_run(monkeypatch, config, model)

    asked = {"value": False}

    async def _answer(prompt: str = "> ") -> str:
        asked["value"] = True
        return "The EU."

    monkeypatch.setattr(main_module, "_read_answer", _answer)

    exit_code = await main_module.main(["Should we expand?"])

    out, lines = drain_stdout(capsys)
    assert asked["value"], "_read_answer was never awaited — the capped-round ask_user was dropped"
    assert exit_code == 0
    assert lines, "main() printed no report path"
    body = Path(lines[-1].strip()).read_text(encoding="utf-8")
    assert "Final answer after the clarification." in body


@pytest.mark.parametrize(("max_rounds", "expect_cut_short"), [(1, True), (2, False)])
async def test_max_rounds_counts_model_turns_not_supersteps(
    make_config, monkeypatch, scripted_model, tmp_path, capsys, max_rounds, expect_cut_short
):
    """Pins the cap's unit at its exact boundary: a run of one tool round plus the final answer
    turn is two MODEL TURNS, so it is capped at `max_rounds=1` and completes clean at
    `max_rounds=2`. Any supersteps-derived mapping (middleware `after_model` nodes cost ~4
    supersteps per round) would move this boundary and fail one side of the pair.

    The capped side also proves the graceful stop: the lead gets one bounded synthesis pass
    (`_SYNTHESIZE_NOW`) after the capped round's tools finish, so the report still carries a
    real final answer alongside the round-cap disclosure.
    """
    agent = AgentSettings(
        max_rounds=max_rounds,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)
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
    # A third reply for verification's Phase 2 Step 5 consolidation call: the answer cites
    # nothing, so it is the pass's only model call (`patch_run` binds every role to this one
    # model), made right after these two agent-loop turns.
    consolidation = AIMessage(content="Nothing was cited, so nothing was checked.")
    model = scripted_model([one_round, final, consolidation])
    patch_run(monkeypatch, config, model)

    exit_code = await main_module.main(["a question needing one round"])

    out, lines = drain_stdout(capsys)
    assert exit_code == 0
    body = Path(lines[-1].strip()).read_text(encoding="utf-8")
    assert (_CUT_SHORT_HEADING in body) is expect_cut_short
    # BOTH sides keep the answer: uncapped by finishing normally, capped via the synthesis
    # pass — the cap must no longer destroy a run's output.
    assert "Answered after exactly one tool round." in body
    if expect_cut_short:
        assert _ROUND_CAP_TEXT in body
        # The synthesis instruction actually reached the model as the resumed thread's last
        # human message, rather than the answer arriving by script-order coincidence. It is
        # the SECOND of exactly two agent-loop model calls (the docstring's own pinned count):
        # the third, scripted separately above, is verification's consolidation call.
        last_call = model._received_messages[1]
        assert any(
            main_module._SYNTHESIZE_NOW in str(getattr(message, "content", ""))
            for message in last_call
        )


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
    model = scripted_model([*([keep_going] * 20)])
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
    make_config, make_agent_settings, monkeypatch, scripted_model, tmp_path, capsys
):
    """D2: the wall clock expires while the only model turn is a bare tool-call proposal (no
    prose), so `_final_answer` sees nothing — no report, exit 1, stderr names the wall clock.

    The elapsed-time assertion pins the timeout itself: without it, a broad "catch anything,
    call it wall_clock" shortcut that ran the full sleep would still pass.

    Step 3 (Drift C): the clock now arms on the LEAD's `task(subagent_type="researcher")`
    dispatch, a top-level call `__main__`'s stream loop actually sees — the nested `search_web`
    call that stalls lives one tier deeper, inside the researcher `patch_run`'s single model
    also plays.
    """
    # `make_agent_settings` disables the margin -- this test is about the wall clock
    # itself, not the reserve.
    agent = make_agent_settings(wall_clock_seconds=1)
    config = make_config(agent=agent)

    task_call = _task_call("Research widgets")
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    model = scripted_model([task_call, search_call])
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


async def test_main_writes_no_report_when_the_wall_clock_expires_on_a_tool_calling_preamble(
    make_config, make_agent_settings, monkeypatch, scripted_model, tmp_path, capsys
):
    """R5 (PLAN-research-throughput.md Phase 1): the interrupted turn carried prose ALONGSIDE
    its `task(researcher)` dispatch — a plan, not an answer. `_final_answer` skips it, so this
    expiry is answerless and stays a FAILED run: no report, exit 1, stderr naming the clock.

    This test used to assert the opposite (exit 0 with a cut-short report), which is what
    published the failed 1800s run's planning preamble as its answer. The "wall clock expires
    while a real answer exists" branch it used to cover is now unreachable by construction: a
    tool-call-FREE AIMessage ends the agent loop, so no further work — and therefore no
    expiry — can follow one inside the clock's scope. "Cut short but answered" is still
    covered, on the round-cap and synthesis-margin paths that re-enter the graph after a break.

    Step 3 (Drift C): the LEAD's own turn carries the prose AND the `task(researcher)` dispatch
    — the slow `search_web` call that stalls the clock lives one tier deeper.
    """
    agent = make_agent_settings(wall_clock_seconds=1)
    config = make_config(agent=agent)

    partial_then_task = AIMessage(
        content="Partial finding: Acme quoted $4.20/unit.",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": "Research widgets", "subagent_type": "researcher"},
                "id": "call_task",
            }
        ],
    )
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    model = scripted_model([partial_then_task, search_call])
    patch_run(monkeypatch, config, model)
    # After the model is built — see `_install_slow_search`'s docstring.
    _install_slow_search(monkeypatch, delay_seconds=3)

    started = time.monotonic()
    exit_code, files, out, err = await _run_main(
        ["a question that starts researching"], config, capsys
    )
    elapsed = time.monotonic() - started

    assert exit_code == 1
    assert files == [], f"a report was written from a tool-calling preamble: {files}"
    assert any("wall clock" in line for line in err.splitlines()), err
    # The preamble prose reached no artifact at all — not the report (there is none) and not
    # stdout's post-run summary.
    assert "Partial finding: Acme quoted $4.20/unit." not in out
    assert "summary:" in out
    # Well under the full 3s sleep: cut off near the 1s bound rather than completed and mislabeled.
    assert elapsed < 2.5, f"run took {elapsed}s — the wall clock did not actually fire early"


# --- Phase 5 (parent plan): synthesis reserve (synthesis_margin_seconds) --------------


async def test_main_synthesizes_when_the_synthesis_margin_is_crossed(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """R7's reserve: once elapsed research time crosses `wall_clock_seconds -
    synthesis_margin_seconds`, the run gets the SAME bounded `_SYNTHESIZE_NOW` pass the round
    cap uses — synthesizing instead of running out the clock. `wall_clock_seconds=4,
    synthesis_margin_seconds=3` puts the trigger at 1s elapsed; the ~2s slow search crosses it
    while leaving 2s of wall clock unspent, so the margin — not the hard clock — must be what
    ends the run.
    """
    agent = AgentSettings(
        wall_clock_seconds=4,
        synthesis_margin_seconds=3,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)

    task_call = _task_call("Research widgets")
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    # No researcher final turn is scripted any more: Phase 1 of PLAN-research-throughput.md
    # arms a dispatch-path deadline at the SAME `wall_clock_seconds - synthesis_margin_seconds`
    # instant this threshold uses, so the 2s search is cancelled at 1s and the researcher never
    # gets a second turn — the margin is then crossed at the next turn boundary, as before.
    # The injected `_SYNTHESIZE_NOW` turn reaches the LEAD, not the researcher.
    synthesis_answer = AIMessage(content="Widgets cost about four dollars per unit.")
    # No citation in the answer above, so verification's consolidation call is its only call.
    consolidation = AIMessage(content="Nothing was cited, so nothing was checked.")
    model = scripted_model([task_call, search_call, synthesis_answer, consolidation])
    patch_run(monkeypatch, config, model)
    _install_slow_search(monkeypatch, delay_seconds=2)

    started = time.monotonic()
    exit_code, files, out, err = await _run_main(
        ["a question that starts researching"], config, capsys
    )
    elapsed = time.monotonic() - started

    assert exit_code == 0
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    # Names the SYNTHESIS MARGIN specifically, not the wall clock or the round cap.
    assert _SYNTHESIS_MARGIN_TEXT in body
    assert _WALL_CLOCK_TEXT not in body
    assert _ROUND_CAP_TEXT not in body
    assert "Widgets cost about four dollars per unit." in body
    # Exactly the LEAD's dispatch turn, the researcher's one turn (its second never runs —
    # the dispatch is cancelled at the reserve), the injected synthesis turn, and the
    # verification consolidation call — nothing extra, nothing dropped.
    assert len(model._received_messages) == 4
    # The synthesis instruction actually reached the model as the resumed thread's last human
    # message (the same technique `test_max_rounds_counts_model_turns_not_supersteps` uses for
    # the round cap), rather than the answer arriving by script-order coincidence.
    synthesis_call = model._received_messages[2]
    # The MARGIN's own wording (G3, 3F review), not the round cap's — a margin trip must never
    # tell the lead "the round cap has been reached".
    assert any(
        main_module._SYNTHESIZE_NOW_MARGIN in str(getattr(message, "content", ""))
        for message in synthesis_call
    )
    assert not any(
        main_module._SYNTHESIZE_NOW in str(getattr(message, "content", ""))
        for message in synthesis_call
    )
    # Well under the 4s wall clock: cut off near the ~2s search, not the hard clock.
    assert elapsed < 3.5, (
        f"run took {elapsed}s — should have cut short at the margin, not the wall clock"
    )


async def test_main_completes_normally_when_finishing_inside_the_synthesis_margin(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """A run that finishes well inside the reserve is untouched by it: no margin disclosure, no
    extra `_SYNTHESIZE_NOW` turn, ordinary completion.
    """
    agent = AgentSettings(
        wall_clock_seconds=10,
        synthesis_margin_seconds=1,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)

    task_call = _task_call("Research widgets")
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    researcher_done = AIMessage(content="Widgets found at $4.20/unit.")
    # The LEAD's own natural second turn — reached normally, never via a synthesis injection.
    final_answer = AIMessage(content="Widgets found at $4.20/unit, per the research above.")
    consolidation = AIMessage(content="Nothing was cited, so nothing was checked.")
    model = scripted_model([task_call, search_call, researcher_done, final_answer, consolidation])
    patch_run(monkeypatch, config, model)
    _install_slow_search(monkeypatch, delay_seconds=0)

    exit_code, files, out, err = await _run_main(
        ["a question that finishes quickly"], config, capsys
    )

    assert exit_code == 0
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING not in body
    assert _SYNTHESIS_MARGIN_TEXT not in body
    assert "Widgets found at $4.20/unit, per the research above." in body
    assert not any(
        main_module._SYNTHESIZE_NOW in str(getattr(message, "content", ""))
        or main_module._SYNTHESIZE_NOW_MARGIN in str(getattr(message, "content", ""))
        for batch in model._received_messages
        for message in batch
    ), "the synthesis pass fired despite finishing well inside the reserve"


@pytest.mark.parametrize(
    ("wall_clock", "margin", "expected"),
    [
        # margin 0 DISABLES the reserve (None). These are the cases a full-run test cannot
        # distinguish: at this boundary asyncio's timeout cancellation always wins the race,
        # so the run reports a wall-clock cut whether or not the disable guard exists.
        # `wall_clock - 0` would be a threshold of 1, which every elapsed here meets.
        (1, 0, None),
        (600, 0, None),
        # A negative margin is nonsense config-side (`ge=0`) but must not invert the rule.
        (4, -1, None),
        # Enabled: the threshold is wall_clock - margin, and both the dispatch path (arming
        # the deadline) and the turn-boundary check derive from this one value (issue #43 #5).
        (4, 3, 1.0),
        (1800, 240, 1560.0),
    ],
)
def test_research_reserve_boundaries(wall_clock, margin, expected):
    """R7's threshold decision, isolated. Pins the two things the full-run margin tests
    cannot: that `margin == 0` means DISABLED rather than "a threshold equal to the wall
    clock" (which would race the hard clock for the same run), and that the single value
    feeding both the dispatch deadline and the turn-boundary check is `wall_clock - margin`.
    The `remaining() <= 0` comparison fires exactly AT the instant, so landing precisely on
    the threshold still triggers the reserve.
    """
    assert _research_reserve(wall_clock, margin) == expected


async def test_synthesis_margin_seconds_zero_disables_the_reserve_and_reaches_the_wall_clock(
    make_config, make_agent_settings, monkeypatch, scripted_model, tmp_path, capsys
):
    """Contracts: `synthesis_margin_seconds = 0` disables the reserve entirely. The naive
    threshold `wall_clock_seconds - 0` equals the wall clock itself, which would make a margin
    cut fire at the same instant the clock expires — instead, a disabled reserve must let the
    run reach the HARD wall clock exactly as it did before this phase (mirrors
    `test_main_writes_no_report_when_the_wall_clock_expires_on_a_tool_calling_preamble` with
    the new field pinned to its disable value).

    A disabled reserve also leaves Phase 1's dispatch deadline UNARMED, so the researcher runs
    until the hard clock takes it — nothing is cut off early. Its turn carries prose alongside
    a tool call, which R5 no longer accepts as an answer, so the expiry is answerless: the
    assertions below are about which BOUND ended the run, which is what this test exists for.
    """
    agent = make_agent_settings(wall_clock_seconds=1)
    config = make_config(agent=agent)

    partial_then_task = AIMessage(
        content="Partial finding: Acme quoted $4.20/unit.",
        tool_calls=[
            {
                "name": "task",
                "args": {"description": "Research widgets", "subagent_type": "researcher"},
                "id": "call_task",
            }
        ],
    )
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    model = scripted_model([partial_then_task, search_call])
    patch_run(monkeypatch, config, model)
    _install_slow_search(monkeypatch, delay_seconds=3)

    started = time.monotonic()
    exit_code, files, out, err = await _run_main(
        ["a question that starts researching"], config, capsys
    )
    elapsed = time.monotonic() - started

    assert exit_code == 1
    assert files == [], f"a report was written despite no final answer: {files}"
    # The HARD clock ended this run, not a reserve that fired at `wall_clock_seconds - 0`:
    # stderr names the wall clock, and no synthesis pass was ever announced.
    assert any("wall clock" in line for line in err.splitlines()), err
    assert "synthesis margin" not in out
    assert "asking for a synthesis" not in out
    # Well under the full 3s sleep: cut off near the 1s bound rather than completed and mislabeled.
    assert elapsed < 2.5, f"run took {elapsed}s — the wall clock did not actually fire"


async def test_the_synthesis_margin_defers_its_break_until_a_crossing_turns_tool_calls_answer(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """G1 (3F review): `margin_hit and not awaiting_tool_ids` (the margin's break guard) is
    trivially true unless the CROSSING turn's own tool calls get added to `awaiting_tool_ids`,
    mirroring the round cap's bookkeeping (`rounds_used == max_rounds`'s branch). Without that,
    a margin trip on a turn that ALSO proposes more research work breaks the stream immediately
    with those calls unanswered, and the injected synthesis message lands after an `AIMessage`
    carrying unanswered `tool_calls` — a sequence a real provider rejects.

    The crossing turn here is a SLOW LEAD MODEL TURN (not a slow search, unlike the other margin
    tests above): the defect is specifically about the LEAD's OWN turn proposing a second `task`
    dispatch right as the margin trips, which is the realistic failure case (the threshold is
    crossed mid-research while the lead is dispatching researchers). `synthesis_margin_seconds=8`
    against `wall_clock_seconds=10` puts the threshold at a comfortable 2s, far from the 10s hard
    clock, so this is not a photo-finish against the real wall clock the way the `0`-disables
    boundary is — no flakiness risk from racing the real deadline.
    """
    agent = AgentSettings(
        wall_clock_seconds=10,
        synthesis_margin_seconds=8,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)

    task_call = _task_call("Research widgets", call_id="call_task_1")
    researcher_one_done = AIMessage(content="Widgets found at $4.20/unit.")
    # The LEAD's own second turn: proposing MORE tool work is what makes a margin trip here
    # dangerous without G1's fix (the crossing turn itself carries unanswered tool_calls).
    task_call_2 = _task_call("Research pricing further", call_id="call_task_2")
    researcher_two_done = AIMessage(content="Pricing confirmed at $4.10/unit this week.")
    synthesis_answer = AIMessage(content="Widgets cost about four dollars per unit.")
    consolidation = AIMessage(content="Nothing was cited, so nothing was checked.")
    model = scripted_model(
        [
            task_call,
            researcher_one_done,
            task_call_2,
            researcher_two_done,
            synthesis_answer,
            consolidation,
        ]
    )
    patch_run(monkeypatch, config, model)

    original_generate = ScriptedChatModel._generate

    def _slow_second_lead_turn(self, messages, stop=None, run_manager=None, **kwargs):
        # The third scripted call (`_call_count == 2`, checked before it increments) is the
        # LEAD's own second turn, proposing `task_call_2` — a genuinely slow MODEL call, not a
        # slow search, so the crossing is observed on a turn the top-level stream sees directly.
        if self._call_count == 2:
            time.sleep(2.5)
        return original_generate(self, messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr(ScriptedChatModel, "_generate", _slow_second_lead_turn)

    exit_code, files, out, err = await _run_main(
        ["a question that starts researching"], config, capsys
    )

    assert exit_code == 0
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    assert _SYNTHESIS_MARGIN_TEXT in body

    def _has_dangling_tool_calls(messages) -> bool:
        answered_ids = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        return any(
            isinstance(m, AIMessage)
            and any(call.get("id") not in answered_ids for call in m.tool_calls)
            for m in messages
        )

    # The shared phrase, not a specific constant: whichever synthesis-instruction wording
    # reaches the model (round-cap's or the margin's own, per G3), this is the injected
    # `HumanMessage` that must never follow an `AIMessage` with unanswered `tool_calls`.
    synthesis_calls = [
        received
        for received in model._received_messages
        if received and "Stop researching now" in str(getattr(received[-1], "content", ""))
    ]
    assert synthesis_calls, "no synthesis pass was ever triggered"
    synthesis_thread = synthesis_calls[-1]
    assert not _has_dangling_tool_calls(synthesis_thread), (
        "the synthesis pass was injected after an AIMessage with unanswered tool_calls"
    )
    # The sharper pin: langgraph auto-heals a dangling `tool_calls` entry by synthesizing its
    # own "cancelled" `ToolMessage` before the next human turn, which satisfies the assertion
    # above EVEN THOUGH the second dispatch never actually ran — so the real defect (the
    # crossing turn's own proposed research getting thrown away instead of awaited) shows up
    # here instead: the second dispatch's `ToolMessage` must carry the RESEARCHER's actual
    # finding, not langgraph's auto-cancellation text.
    task_2_results = [
        m
        for m in synthesis_thread
        if isinstance(m, ToolMessage) and m.tool_call_id == "call_task_2"
    ]
    assert task_2_results, "the second dispatch's tool call was never answered before synthesis"
    assert "cancelled" not in str(task_2_results[0].content).lower(), (
        "the margin trip cancelled the second research dispatch instead of waiting for it to "
        f"finish: {task_2_results[0].content!r}"
    )


async def test_a_runaway_synthesis_pass_after_the_margin_keeps_the_margin_label(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """G4 (3F review): `except GraphRecursionError` used to set `cut_short = "round_cap"`
    unconditionally, so a runaway SYNTHESIS PASS triggered by the MARGIN (not the cap) was
    disclosed as a round-cap cut, naming the wrong bound. A lead that keeps proposing tool
    calls despite `_SYNTHESIZE_NOW_MARGIN` exhausts `_SYNTHESIS_RECURSION_LIMIT`, and the
    report must still say the synthesis margin caused the cut.

    Mirrors `test_main_cuts_the_run_short_at_the_round_cap`'s "never stops proposing tool
    calls" shape, but reaches the SAME `GraphRecursionError` via the margin instead of the cap
    (`max_rounds` stays at its generous default so the cap never fires first).
    """
    agent = AgentSettings(
        wall_clock_seconds=10,
        synthesis_margin_seconds=8,  # threshold = 2s, comfortably short of the 10s wall clock
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)

    task_call = _task_call("Research widgets")
    # The nested researcher's own (single) turn: a plain final reply, no tool calls -- it does
    # not have `write_todos` bound (that is a LEAD-only tool), so it must not be handed
    # `keep_going`.
    researcher_done = AIMessage(content="Widgets found at $4.20/unit.")
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
    model = scripted_model([task_call, researcher_done, *([keep_going] * 60)])
    patch_run(monkeypatch, config, model)

    original_generate = ScriptedChatModel._generate

    def _slow_second_lead_turn(self, messages, stop=None, run_manager=None, **kwargs):
        # The third scripted call (`_call_count == 2`) is the LEAD's own second turn, the
        # first `keep_going` -- a slow MODEL turn crosses the margin threshold on a turn that
        # ALSO proposes a tool call, exactly like the G1 test above, so the break defers until
        # it is answered.
        if self._call_count == 2:
            time.sleep(2.5)
        return original_generate(self, messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr(ScriptedChatModel, "_generate", _slow_second_lead_turn)

    exit_code = await main_module.main(["question that never settles"])

    out, lines = drain_stdout(capsys)
    assert exit_code == 0
    assert lines, "main() printed no report path"
    report_path = Path(lines[-1].strip())
    assert report_path.exists()
    body = report_path.read_text(encoding="utf-8")
    assert _CUT_SHORT_HEADING in body
    # The margin's own label, not the round cap's — this is the whole point of G4.
    assert _SYNTHESIS_MARGIN_TEXT in body
    assert _ROUND_CAP_TEXT not in body
    assert _WALL_CLOCK_TEXT not in body


async def test_a_pre_research_clarification_does_not_start_the_wall_clock(
    make_config, make_agent_settings, monkeypatch, scripted_model, tmp_path, capsys
):
    """The clock arms at the first `task(subagent_type="researcher")` dispatch (Step 3 Drift C),
    not at process start, so a pre-research `ask_user` wait of any length must not trip it — the
    wait (2s) is longer than the configured clock (1s) and the run must still finish clean.
    Paired with the mid-run test below; neither alone pins where the clock starts.
    """
    agent = make_agent_settings(wall_clock_seconds=1)
    config = make_config(agent=agent)
    ask = AIMessage(
        content="",
        tool_calls=[{"name": "ask_user", "args": {"question": "Which scope?"}, "id": "call_1"}],
    )
    final = AIMessage(
        content="Final answer, no research needed.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([ask, final])
    patch_run(monkeypatch, config, model)

    read_answer_called = {"value": False}

    async def _slow_answer(prompt: str = "> ") -> str:
        read_answer_called["value"] = True
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
    # Proves the interrupt path actually ran rather than the run ending on an unconsumed
    # leading reply: without this, a stale scripted reply the graph consumes as its final
    # answer would end the run before `ask_user` ever fires, and the test would go vacuous.
    assert read_answer_called["value"], "_read_answer was never awaited — ask_user never fired"
    assert model._call_count == 2, "expected exactly the ask_user call and the final answer"
    assert "Final answer, no research needed." in body


async def test_a_mid_run_clarification_with_no_answer_is_bounded_by_the_wall_clock(
    make_config, make_agent_settings, monkeypatch, scripted_model, tmp_path, capsys
):
    """Pairs with the pre-research test above: once research has begun (a `task(researcher)`
    dispatch, Step 3 Drift C) the clock runs and is not paused for an interrupt, so an
    unanswered mid-run ask still ends the run at the bound.

    D2: nothing in this scripted run ever produces a final answer on the LEAD's own transcript
    (`task_call`/`search_call`/`ask` are all content-less tool-call proposals; the researcher's
    own report becomes a `task` ToolMessage, never an `AIMessage` the lead itself said), so this
    is the wall-clock NO-answer case — no report, exit 1. The elapsed-time assertion pins the
    timeout to the wait itself.
    """
    agent = make_agent_settings(wall_clock_seconds=1)
    config = make_config(agent=agent)

    task_call = _task_call("Research widgets")
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": "call_search"}],
    )
    researcher_report = AIMessage(content="Researcher report (no citations yet).")
    ask = AIMessage(
        content="",
        tool_calls=[
            {"name": "ask_user", "args": {"question": "Narrower scope?"}, "id": "call_ask"}
        ],
    )
    model = scripted_model([task_call, search_call, researcher_report, ask])
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
    model = scripted_model([plan_call])  # no second response — the run dies here
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
    at this boundary as `asyncio.CancelledError` instead — see the Phase 4 handoff note — and
    the sibling `test_main_exits_cleanly_on_cancelled_error_mid_stream` pins that path.
    """
    config = make_config(
        agent=AgentSettings(workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports")
    )
    # Not preflight fodder here (patch_run's default is a no-op preflight): this is the
    # graph's actual, only real turn — the interrupt fires on its first yielded chunk
    # regardless of content, so a bare answer with no tool call is enough.
    reply = AIMessage(content="pong")
    model = scripted_model([reply])
    patch_run(monkeypatch, config, model)

    import harness.agent as agent_module

    real_build_agent = agent_module.build_agent

    def _build_agent_that_interrupts(
        config: HarnessConfig,
        registry: Any,
        run_log: Any = None,
        sink: Any = None,
        browser: Any = None,
        deadline: Any = None,
    ) -> Any:
        real_agent = real_build_agent(config, registry, run_log, sink, browser, deadline)

        class _InterruptingAgent:
            def astream(self, *args: Any, **kwargs: Any) -> Any:
                async def _gen() -> Any:
                    async for mode, chunk in real_agent.astream(*args, **kwargs):
                        yield mode, chunk
                        raise KeyboardInterrupt("test abort")

                return _gen()

        return _InterruptingAgent()

    # At the source module: `main` imports `build_agent` at call time (the heavy-import
    # deferral), so the patched attribute is what that import binds.
    monkeypatch.setattr(agent_module, "build_agent", _build_agent_that_interrupts)

    exit_code, files, out, err = await _run_main(["a question"], config, capsys)

    assert exit_code == 1
    assert files == [], f"a report was written for a Ctrl+C abort: {files}"
    assert any(line.startswith("error:") for line in err.splitlines()), err
    assert "ctrl" in err.lower() or "abort" in err.lower()
    assert "Traceback" not in err
    assert "summary:" in out


async def test_main_exits_cleanly_on_cancelled_error_mid_stream(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """A genuine external cancellation (Ctrl+C surfacing as `asyncio.CancelledError` rather
    than `KeyboardInterrupt` — see the sibling `keyboard_interrupt` test's docstring) maps
    onto the same hard-error path: no report, exit 1, no traceback escaping `main()`.
    """
    config = make_config(
        agent=AgentSettings(workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports")
    )
    reply = AIMessage(content="pong")
    model = scripted_model([reply])
    patch_run(monkeypatch, config, model)

    import harness.agent as agent_module

    real_build_agent = agent_module.build_agent

    def _build_agent_that_interrupts(
        config: HarnessConfig,
        registry: Any,
        run_log: Any = None,
        sink: Any = None,
        browser: Any = None,
        deadline: Any = None,
    ) -> Any:
        real_agent = real_build_agent(config, registry, run_log, sink, browser, deadline)

        class _InterruptingAgent:
            def astream(self, *args: Any, **kwargs: Any) -> Any:
                async def _gen() -> Any:
                    async for mode, chunk in real_agent.astream(*args, **kwargs):
                        yield mode, chunk
                        raise asyncio.CancelledError("test abort")

                return _gen()

        return _InterruptingAgent()

    monkeypatch.setattr(agent_module, "build_agent", _build_agent_that_interrupts)

    exit_code, files, out, err = await _run_main(["a question"], config, capsys)

    assert exit_code == 1
    assert files == [], f"a report was written for a cancelled run: {files}"
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

    Step 3 (Drift C): `search_web` now lives on the RESEARCHER, so the abort must propagate up
    through the lead's own `task` dispatch — proving `_reader_failure_message`'s propagate
    branch and `_retry_on_non_search_abort`'s exclusion actually let it through rather than
    stringifying it into a soft `RESEARCHER FAILED` message.
    """
    config = make_config(
        agent=AgentSettings(workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports")
    )

    def _search_call(call_id: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": "search_web", "args": {"query": "widgets"}, "id": call_id}],
        )

    # make_config's default max_consecutive_failures is 3 — the lead's task(researcher) dispatch
    # plus three scripted search rounds (all served by the SAME patched model, one per role) are
    # exactly enough for the third tool execution to trip the abort before a fourth model call.
    model = scripted_model(
        [_task_call("Research widgets"), _search_call("c1"), _search_call("c2"), _search_call("c3")]
    )
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
    final = AIMessage(
        content="Final answer.",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    model = scripted_model([final])
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
    # The cited answer rides on a TOOL-CALL-FREE message, delivered by the bounded synthesis
    # pass the round cap triggers. A message carrying both prose and `tool_calls` is a plan,
    # not an answer, and `_final_answer` skips it (R5, PLAN-research-throughput.md Phase 1) —
    # so the claim under verification has to reach the run the way a real answer does.
    partial = AIMessage(
        content=f"Acme quoted $4.20 per unit [{source_id}].",
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    # Two capped rounds of tool work, then the synthesis pass's answer.
    model = scripted_model([keep_going, keep_going, partial])
    patch_run(monkeypatch, config, model)

    verify_model = scripted_model(
        [
            verify_reply("not_supported", "The capture reads $5.10."),
            # The consolidation call (Phase 2 Step 5), made after the per-paragraph loop.
            AIMessage(content="One paragraph was not supported: the capture reads $5.10."),
        ]
    )

    # One patch target serves every caller now, so the verify client is told apart by ROLE
    # (Phase 1 Step 4 moved verification onto its own "verifier" role) rather than call order,
    # which would have to be re-counted every time a new role gets preflighted or resolved.
    def _dispatch(cfg, role):
        return verify_model if role == "verifier" else model

    monkeypatch.setattr("harness.models.build_chat_model", _dispatch)

    await main_module.main(["what does Acme charge?"])

    _, lines = drain_stdout(capsys)
    body = Path(lines[-1].strip()).read_text(encoding="utf-8")

    assert _CUT_SHORT_HEADING in body, "this run was supposed to hit the round cap"
    # The check actually ran on the cut-short path — not skipped, not defaulted — including
    # its consolidation call (Phase 2 Step 5).
    assert verify_model._call_count == 2
    assert "One paragraph was not supported: the capture reads $5.10." in body
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

    async def _record(*_args: object, **_kwargs: object) -> str:
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

    renderer = PlainRenderer()
    decisions = await main_module._answer_questions(
        interrupt, renderer, SourceRegistry(), StageTracker(renderer)
    )

    out, _ = drain_stdout(capsys)
    asked = [line for line in out.splitlines() if line.strip()]
    metal_line = next(line for line in asked if line == "Metal or album?")
    isotope_line = next(line for line in asked if "isotope" in line)
    assert asked.index(metal_line) < asked.index(isotope_line), (
        "the description fallback never fired, or fired out of order"
    )
    assert [d["message"] for d in decisions] == ["answered", "answered"]


async def test_a_display_error_from_the_activity_sink_is_not_retried_or_blamed_on_the_reader(
    build_researcher, scripted_model
):
    """A `DisplayError` out of the sink's `on_change` must escape the `task` dispatch guard.

    The sink pushes from INSIDE `awrap_tool_call`, so without the
    `_PASS_THROUGH_TASK_FAILURES` exclusion `ToolRetryMiddleware` would re-run the whole
    subagent once and `ToolErrorMiddleware` would then convert the display's own bug into
    `"READER FAILED (...)"` plus a `subagent_failed` incident -- the wrong component blamed, at
    double that subagent's token cost.

    The notify COUNT is what discriminates: the exclusion means the dispatch is attempted once,
    so `on_change` raises once. Drop `DisplayError` from the exclusion and the retry attempts it
    a second time, making this 2 and swallowing the raise entirely.
    """
    from harness.activity import ActivitySink, DisplayError

    researcher_model = scripted_model(
        [
            _task_call("Fetch and digest https://a.test", subagent_type="reader"),
            AIMessage(content="done"),
        ]
    )
    reader_model = scripted_model([AIMessage(content="reader done")])

    notify_calls = 0

    def _exploding_display() -> None:
        nonlocal notify_calls
        notify_calls += 1
        raise DisplayError("the renderer blew up")

    run_log = RunLog()
    sink = ActivitySink(on_change=_exploding_display)
    researcher, _ = build_researcher(researcher_model, reader_model, run_log=run_log, sink=sink)

    with pytest.raises(DisplayError):
        await researcher.ainvoke({"messages": [HumanMessage(content="research this angle")]})

    assert notify_calls == 1, "the dispatch was retried -- DisplayError is not being excluded"
    assert run_log.incidents() == [], "a display bug was recorded as a subagent failure"


async def test_a_renderer_crash_mid_dispatch_fails_the_run_cleanly_and_writes_no_report(
    make_config, monkeypatch, scripted_model, tmp_path, capsys
):
    """A plain renderer exception during a pushed `ToolCall` becomes a `DisplayError`, escapes
    the `task` dispatch guard, and lands on `main()`'s existing hard-error path.

    Pins the whole route the `DisplayError` mechanism exists to create: the run is FAILED
    (exit 1, no report, error on stderr) rather than a soft "READER FAILED" incident inside a
    report that got written anyway -- and the terminal is still restored, with no traceback
    escaping under the alternate screen.
    """
    from harness.config import AgentSettings
    from harness.display import ToolCall as ToolCallEvent

    head_model = scripted_model(
        [
            _task_call("Angle A", call_id="call_researcher", subagent_type="researcher"),
            AIMessage(
                content="Final answer.",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            ),
        ]
    )
    researcher_model = scripted_model(
        [
            _task_call("Read it", call_id="call_reader", subagent_type="reader"),
            AIMessage(content="researcher done"),
        ]
    )
    reader_model = scripted_model([AIMessage(content="reader done")])
    models_by_role: dict[str, Any] = {
        "head": head_model,
        "researcher": researcher_model,
        "reader": reader_model,
        "verifier": head_model,
    }

    config = make_config(
        agent=AgentSettings(workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports")
    )
    patch_run(monkeypatch, config, head_model)
    monkeypatch.setattr("harness.models.build_chat_model", lambda cfg, role: models_by_role[role])

    real_build_renderer = main_module.build_renderer

    def _renderer_that_dies_on_a_tool_call() -> Any:
        inner = real_build_renderer()

        class _Exploding:
            def emit(self, event: Any) -> None:
                if isinstance(event, ToolCallEvent):
                    # A PLAIN exception, not a DisplayError: the point is that the callback
                    # boundary converts an ordinary renderer bug into the pass-through type.
                    raise RuntimeError("the renderer blew up")
                inner.emit(event)

            def suspend(self) -> Any:
                return inner.suspend()

            def close(self) -> None:
                inner.close()

        return _Exploding()

    monkeypatch.setattr(main_module, "build_renderer", _renderer_that_dies_on_a_tool_call)

    exit_code, files, out, err = await _run_main(["a question needing research"], config, capsys)

    assert exit_code == 1
    assert files == [], f"a report was written for a failed run: {files}"
    assert any(line.startswith("error:") for line in err.splitlines()), err
    assert "DisplayError" in err, f"the failure was not attributed to the display: {err}"
    assert "Traceback" not in err


# --- Tool-call summarizers (PR #25 review) -------------------------------------------------
#
# These feed the running pane's structured tool-call log. Every branch below was previously
# reachable only through a full agent run, so only the single-URL `fetch_pages` shape was
# exercised and the rest could be rewritten with nothing failing.


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("task", {"subagent_type": "reader", "description": "Read S3"}, "reader -- Read S3"),
        # A missing/blank subagent_type still names the tool rather than rendering "-- ...".
        ("task", {"description": "Read S3"}, "task -- Read S3"),
        # A full delegation prompt collapses to its first sentence — the plain renderer
        # prints this string verbatim, with no render-time ellipsis (PR #25 review).
        (
            "task",
            {
                "subagent_type": "researcher",
                "description": "Research the current state of X. I need a source-cited report"
                " covering definitions, adoption rates, and benchmarks.",
            },
            "researcher -- Research the current state of X.",
        ),
        ("search_web", {"query": "solar tariffs"}, "solar tariffs"),
        ("fetch_pages", {"urls": ["https://a.example"]}, "https://a.example"),
        (
            "fetch_pages",
            {"urls": ["https://a.example", "https://b.example"]},
            "https://a.example +1",
        ),
        (
            "fetch_raw",
            {"urls": ["https://a.example", "https://b.example", "https://c.example"]},
            "https://a.example +2",
        ),
        ("fetch_pages", {"urls": []}, ""),
        # Unrecognized tool: first string-valued arg, skipping non-strings.
        ("write_file", {"limit": 5, "path": "notes.md"}, "notes.md"),
        ("write_file", {"limit": 5}, ""),
    ],
)
def test_summarize_tool_args_covers_every_shape(name, args, expected):
    assert _summarize_tool_args(name, args) == expected


def test_summarize_tool_result_truncates_at_sixty_characters():
    long_line = "x" * 80
    summary = _summarize_tool_result(ToolMessage(content=long_line, tool_call_id="c1"))

    assert summary == "x" * 60 + "…"


def test_summarize_tool_result_truncates_a_leading_digit_line_too():
    """PR #25 review, Minor: the leading-digit branch used to return the line whole.

    A `task` result is free model prose, so a digest opening with a numbered list item or a
    year reached that branch and put an unbounded string on a one-line log row.
    """
    long_line = "2024 " + "y" * 100
    summary = _summarize_tool_result(ToolMessage(content=long_line, tool_call_id="c1"))

    assert len(summary) == 61  # 60 chars plus the ellipsis
    assert summary.endswith("…")


def test_summarize_tool_result_takes_only_the_first_line_and_strips_it():
    message = ToolMessage(content="  first line  \nsecond line\n", tool_call_id="c1")

    assert _summarize_tool_result(message) == "first line"


def test_summarize_tool_result_of_empty_content_is_empty():
    assert _summarize_tool_result(ToolMessage(content="", tool_call_id="c1")) == ""
