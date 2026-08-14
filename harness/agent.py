"""Compile the lead research agent: model, tools, workspace backend, and middleware.

The ONLY module that imports `deepagents`: every other module works with plain
LangChain/pydantic types, so the rest of the harness stays independent of the agent
framework's API surface. Same shape as `harness/tools/search.py` — one builder function
closing over `config` and the caller's `registry`, no class.
"""

from datetime import date
from typing import Any, Literal

from deepagents import (
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    SubAgent,
    create_deep_agent,
    register_harness_profile,
)
from deepagents._models import get_model_identifier, get_model_provider
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware import AgentMiddleware, InterruptOnConfig, TodoListMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from harness.config import HarnessConfig, run_workspace_dir
from harness.models import build_chat_model
from harness.prompts import render
from harness.sources import SourceRegistry
from harness.tools import build_tools
from harness.tools.ask_user import ASK_USER_TOOL_NAME

# The summarizer's policy, deliberately not config-driven (D7). `trigger` sits above the
# profile fallback of 170,000: generous for a reasoning model that spends heavily on output,
# and it defers the attribution loss D7 guards against as long as the context window allows.
# `keep` is explicit rather than deepagents' default of 6 messages, so recent `[Sn]`-bearing
# findings have room to survive into synthesis.
_SUMMARIZATION_TRIGGER: tuple[Literal["tokens"], int] = ("tokens", 200_000)
_SUMMARIZATION_KEEP: tuple[Literal["messages"], int] = ("messages", 20)

# `["respond"]` only: the developer answers on behalf of the tool and the tool never executes.
# Filesystem `permissions` are `mode="allow"`, which generates no interrupt entries, so this is
# the whole interrupt surface.
_INTERRUPT_ON: dict[str, bool | InterruptOnConfig] = {
    ASK_USER_TOOL_NAME: InterruptOnConfig(allowed_decisions=["respond"])
}


def _register_no_shell_profile(model: BaseChatModel) -> None:
    """Register the no-shell HarnessProfile under `model`'s resolved profile key.

    deepagents keys its harness-profile registry by `f"{provider}:{identifier}"`, derived from
    the model instance rather than from config literals, so it tracks whatever
    `build_chat_model` actually returns. Called for both the head model and the reader model
    (Risk #1): a profile is resolved PER SUBAGENT MODEL key, so leaving the reader's key
    unregistered would silently expose it to `execute`.

    Accepted residue: the registry is process-global and keyed by provider:model-name, not
    scoped to our `base_url`. Re-registering per call is idempotent — the same key always maps
    to the same profile, so a second run in one process is unaffected.
    """
    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    profile_key = f"{provider}:{identifier}"
    register_harness_profile(
        profile_key,
        HarnessProfile(
            excluded_tools=frozenset({"execute"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )


def _reader_spec(
    config: HarnessConfig, reader_model: BaseChatModel, reader_tools: list[BaseTool]
) -> SubAgent:
    """Build the declared `reader` `SubAgent` spec (D1): the lead's only route to `fetch_pages`.

    `interrupt_on` is deliberately left unset — the reader has no checkpointer forwarded and
    cannot interrupt, so inheriting the lead's `ask_user` entry would register an interrupt
    that can never fire.
    """
    return SubAgent(
        name="reader",
        description=(
            "Fetches and digests the given URLs with its own tool calls, returning a "
            "source-cited digest of what they say about the requested facet."
        ),
        system_prompt=render(
            "reader",
            current_date=date.today().isoformat(),
            max_urls_per_call=config.fetch.max_urls_per_call,
        ),
        model=reader_model,
        tools=reader_tools,
    )


def build_agent(config: HarnessConfig, registry: SourceRegistry) -> Runnable:
    """Compile the lead research agent, driven with `ainvoke`/`astream` (substrate D1).

    The research question is NOT baked into the system prompt: this signature has no access to
    it. It travels as the initial `HumanMessage` the caller streams in, and the rendered
    orchestrator prompt carries only `$current_date` and `$max_urls_per_call`.
    """
    model = build_chat_model(config, "head")
    _register_no_shell_profile(model)

    reader_model = build_chat_model(config, "subagent")
    _register_no_shell_profile(reader_model)

    # Rooted at THIS run's subdirectory, not the shared workspace: the agent can only
    # reach its own notes, and two concurrent runs cannot read each other's.
    workspace = run_workspace_dir(config, registry.run_id)
    workspace.mkdir(parents=True, exist_ok=True)
    backend = FilesystemBackend(root_dir=workspace)

    tool_sets = build_tools(config, registry)

    system_prompt = render(
        "orchestrator",
        current_date=date.today().isoformat(),
        max_urls_per_call=config.fetch.max_urls_per_call,
    )

    return create_deep_agent(
        model=model,
        tools=tool_sets.lead,
        system_prompt=system_prompt,
        backend=backend,
        # `paths` are virtual POSIX paths relative to the backend's own root, never real OS
        # paths, so `"/**"` means "everything under the confined root". Second layer of
        # confinement; the first is the backend rejecting any path that traverses outside it.
        permissions=[
            FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="allow")
        ],
        middleware=_middleware(model, backend),
        subagents=[_reader_spec(config, reader_model, tool_sets.reader)],
        # One saver per call, holding this run's thread. In-memory keeps the no-database
        # invariant (D5): no durable, cross-invocation checkpointing.
        checkpointer=InMemorySaver(),
        interrupt_on=_INTERRUPT_ON,
    )


def _middleware(model: Any, backend: BackendProtocol) -> list[AgentMiddleware[Any, Any, Any]]:
    """Build the middleware list with an explicit, broad element type.

    Without the annotation mypy unifies the element type from the first entry and rejects the
    second — a false positive with heterogeneous lists of generic `AgentMiddleware` subclasses.

    The summarizer is deepagents', not langchain's plain one. Both publish the same `.name`, so
    `create_deep_agent` still merges down to one summarizer, but only the deepagents wrapper
    offloads evicted messages to `backend` before dropping them from context — langchain's
    issues a destructive `RemoveMessage(REMOVE_ALL_MESSAGES)` with no recovery path for a
    dropped `[Sn]`-to-finding association, which D7/R3/R7 depend on.
    """
    return [
        TodoListMiddleware(),
        SummarizationMiddleware(
            model=model, backend=backend, trigger=_SUMMARIZATION_TRIGGER, keep=_SUMMARIZATION_KEEP
        ),
    ]
