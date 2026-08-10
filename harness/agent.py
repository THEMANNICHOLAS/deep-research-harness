"""Compile the lead research agent: model, tools, workspace backend, and middleware.

This is the ONLY module that imports `deepagents` — every other module works with plain
LangChain/pydantic types, so the rest of the harness stays independent of the agent
framework's API surface. Mirrors `harness/tools/search.py`'s module shape: one builder
function, closing over `config` and the caller's `registry`, no class.
"""

from datetime import date
from typing import Any, Literal

from deepagents import (
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents._models import get_model_identifier, get_model_provider
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.runnables import Runnable

from harness.config import HarnessConfig
from harness.models import build_chat_model
from harness.prompts import render
from harness.sources import SourceRegistry
from harness.tools import build_tools

# The summarizer's own policy constants (not config-driven — see D7). `trigger` matches
# the profile-driven fallback that would apply anyway if unset (settled finding 6):
# 170,000 tokens is deliberately generous for a reasoning model that spends heavily on
# output. `keep` is set explicitly, per D7, rather than left to fall back to deepagents'
# smaller profile default of 6 messages — a larger kept tail gives recent `[Sn]`-bearing
# findings more room to survive into synthesis.
_SUMMARIZATION_TRIGGER: tuple[Literal["tokens"], int] = ("tokens", 170_000)
_SUMMARIZATION_KEEP: tuple[Literal["messages"], int] = ("messages", 20)


def build_agent(config: HarnessConfig, registry: SourceRegistry) -> Runnable:
    """Compile the lead research agent, driven with `ainvoke`/`astream` (substrate D1).

    The research question is NOT baked into the system prompt here — `build_agent`'s
    signature (frozen by the plan's Contracts) has no access to it. It travels instead as
    the initial `HumanMessage` the caller sends into `ainvoke`/`astream`; the rendered
    orchestrator prompt only carries `$current_date` and `$max_urls_per_call`.
    """
    model = build_chat_model(config, "head")

    # deepagents keys its harness-profile registry by `f"{provider}:{identifier}"`,
    # derived from the model instance itself (settled finding 4) — not assembled from
    # config string literals, so it tracks whatever `build_chat_model` actually returns.
    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    profile_key = f"{provider}:{identifier}"
    # Accepted residue (plan `## Reconciliations` 2026-08-09 (b)): this registry is
    # process-global and keyed by provider:model-name only, not scoped to our `base_url`.
    # Re-registering on every `build_agent` call is idempotent — the same key always maps
    # to this same profile, so a second run in the same process is unaffected.
    register_harness_profile(
        profile_key,
        HarnessProfile(
            excluded_tools=frozenset({"execute"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )

    config.agent.workspace_dir.mkdir(parents=True, exist_ok=True)
    backend = FilesystemBackend(root_dir=config.agent.workspace_dir)

    tools = build_tools(config, registry)

    system_prompt = render(
        "orchestrator",
        current_date=date.today().isoformat(),
        max_urls_per_call=config.fetch.max_urls_per_call,
    )

    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        backend=backend,
        # `FilesystemPermission.paths` are virtual POSIX paths relative to the backend's
        # own root (`virtual_mode=True` by default), never real OS paths — `"/**"` is
        # "everything under the confined root", which `FilesystemBackend` already is.
        # This is the second, belt-and-suspenders confinement layer; the first is the
        # backend itself rejecting any path that would traverse outside its root.
        permissions=[
            FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="allow")
        ],
        middleware=_middleware(model, backend),
    )


def _middleware(model: Any, backend: BackendProtocol) -> list[AgentMiddleware[Any, Any, Any]]:
    """Build the middleware list with an explicit, broad element type.

    Without this annotation mypy unifies the list's element type from the first entry and
    then rejects the second as incompatible — a known false positive with heterogeneous
    lists of generic `AgentMiddleware` subclasses, not a real type error.

    Uses `deepagents.middleware.summarization.SummarizationMiddleware` (Blocker 1 fix), not
    langchain's plain one — both classes publish the same `.name`
    (`"SummarizationMiddleware"`), so `create_deep_agent` still merges down to exactly one
    summarizer, but only the deepagents wrapper offloads evicted messages to `backend`
    before dropping them from the model's context, which is what D7/R3/R7 depend on:
    langchain's plain middleware issues a destructive `RemoveMessage(REMOVE_ALL_MESSAGES)`
    with no recovery path for a dropped `[Sn]`↔finding association.
    """
    return [
        TodoListMiddleware(),
        SummarizationMiddleware(
            model=model, backend=backend, trigger=_SUMMARIZATION_TRIGGER, keep=_SUMMARIZATION_KEEP
        ),
    ]
