"""Compile the lead research agent: model, tools, workspace backend, and middleware.

The ONLY module that imports `deepagents`: every other module works with plain
LangChain/pydantic types, so the rest of the harness stays independent of the agent
framework's API surface. Same shape as `harness/tools/search.py` — one builder function
closing over `config` and the caller's `registry`, no class.
"""

from collections.abc import Awaitable, Callable
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
from langchain.agents.middleware import (
    AgentMiddleware,
    InterruptOnConfig,
    TodoListMiddleware,
    ToolCallRequest,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

# The MODULE, not `from harness.models import build_chat_model`: a by-value import binds a
# module-local name each test would have to patch separately. Attribute lookup at call time
# means patching `harness.models.build_chat_model` covers every caller (see PR #4 review).
from harness import models
from harness.config import HarnessConfig, run_workspace_dir
from harness.prompts import render
from harness.runlog import RunLog
from harness.sources import SourceRegistry, pending_digest_scope
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


# D2: a reader crash must not kill the run. Phase 3's prompt keys off the "READER FAILED"
# prefix, so the derived label must stay exact for `subagent_type="reader"`. Derived, not
# hardcoded: this middleware wraps EVERY `task` dispatch, so a future researcher tier fails as
# "RESEARCHER FAILED" without touching this function, the prompt contract, or its pinned test.
def _reader_failure_message(exc: Exception, request: ToolCallRequest) -> str:
    """Render a subagent (`task`) crash as content for an error `ToolMessage`."""
    # `.get`: the args are model-supplied, and a malformed call must still get a label
    # rather than raising a KeyError out of the error handler itself.
    subagent_type = str(request.tool_call["args"].get("subagent_type", "task"))
    return f"{subagent_type.upper()} FAILED ({type(exc).__name__}): {exc}"


def _digest_text(result: ToolMessage | Command[Any]) -> str:
    """The task ToolMessage's text inside `result`, however the tool wrapped it.

    deepagents' task tool returns a `Command` carrying the digest as a single ToolMessage in
    its `messages` update; langchain wraps a plain-string return (e.g. the unknown-subagent
    notice) into a bare ToolMessage. Anything unrecognized reads as empty, which the caller
    treats as "no digest reached the lead".
    """
    message: ToolMessage | None = None
    if isinstance(result, ToolMessage):
        message = result
    elif isinstance(result.update, dict):
        candidates = result.update.get("messages") or []
        message = next((m for m in candidates if isinstance(m, ToolMessage)), None)
    return message.text.strip() if message is not None else ""


class _ReaderDigestMiddleware(AgentMiddleware[Any, Any, Any]):
    """Promote reader-fetched sources to `digested` only when a digest reaches the lead (R5).

    The reader's `fetch_pages` call only NOMINATES the source IDs it captured
    (`sources.note_digest_candidate`, context-local per task attempt); this middleware marks
    them when — and only when — the attempt returns a non-empty digest. A crash (converted to
    a `... FAILED` error ToolMessage by the outer `ToolErrorMiddleware`) or an empty digest
    leaves them "unread", so the report never discloses a digest the lead never received.

    Async-only, like the tools it observes: the graph is driven with `ainvoke`/`astream`
    (substrate D1) and `fetch_pages` itself is coroutine-only.
    """

    def __init__(self, registry: SourceRegistry) -> None:
        super().__init__()
        self._registry = registry

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        if request.tool_call["name"] != "task":
            return await handler(request)
        with pending_digest_scope() as fetched:
            result = await handler(request)
        if _digest_text(result):
            for source_id in fetched:
                self._registry.mark_read(source_id, "digested")
        return result


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


def build_agent(
    config: HarnessConfig, registry: SourceRegistry, run_log: RunLog | None = None
) -> Runnable:
    """Compile the lead research agent, driven with `ainvoke`/`astream` (substrate D1).

    The research question is NOT baked into the system prompt: this signature has no access to
    it. It travels as the initial `HumanMessage` the caller streams in, and the rendered
    orchestrator prompt carries only `$current_date` and `$max_urls_per_call`.

    `run_log` collects the tools' degraded-coverage incidents; the caller shares one instance
    between this agent and the report so disclosure sees everything (best-effort + disclose).
    """
    model = models.build_chat_model(config, "head")
    _register_no_shell_profile(model)

    # Return value unused until Step 3 wires the researcher spec — the bare call is what makes
    # an undeclared `researcher` role fail loud at build time, same as `head`/`reader` do.
    models.build_chat_model(config, "researcher")

    reader_model = models.build_chat_model(config, "reader")
    _register_no_shell_profile(reader_model)

    # Rooted at THIS run's subdirectory, not the shared workspace: the agent can only
    # reach its own notes, and two concurrent runs cannot read each other's.
    workspace = run_workspace_dir(config, registry.run_id)
    workspace.mkdir(parents=True, exist_ok=True)
    backend = FilesystemBackend(root_dir=workspace)

    tool_sets = build_tools(config, registry, run_log)

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
        middleware=_middleware(model, backend, registry),
        subagents=[_reader_spec(config, reader_model, tool_sets.reader)],
        # One saver per call, holding this run's thread. In-memory keeps the no-database
        # invariant (D5): no durable, cross-invocation checkpointing.
        checkpointer=InMemorySaver(),
        interrupt_on=_INTERRUPT_ON,
    )


def _middleware(
    model: Any, backend: BackendProtocol, registry: SourceRegistry
) -> list[AgentMiddleware[Any, Any, Any]]:
    """Build the middleware list with an explicit, broad element type.

    Without the annotation mypy unifies the element type from the first entry and rejects the
    second — a false positive with heterogeneous lists of generic `AgentMiddleware` subclasses.

    The summarizer is deepagents', not langchain's plain one. Both publish the same `.name`, so
    `create_deep_agent` still merges down to one summarizer, but only the deepagents wrapper
    offloads evicted messages to `backend` before dropping them from context — langchain's
    issues a destructive `RemoveMessage(REMOVE_ALL_MESSAGES)` with no recovery path for a
    dropped `[Sn]`-to-finding association, which D7/R3/R7 depend on.

    The last two entries (D2) scope to the `task` tool only: `ToolErrorMiddleware` defined
    first (outermost) catches whatever exception exhausts `ToolRetryMiddleware` (inner,
    `on_failure="error"` so the exhausted exception reaches the outer catch rather than being
    swallowed into a "continue" message here) and converts it to a `status="error"`
    ToolMessage. `max_retries=1`: retrying `task` re-runs the whole reader subagent, so the
    budget already doubles at one retry. `initial_delay=0.0, jitter=False`: this retry exists
    for reader crashes, not transient network waits, so it should be deterministic and
    test-fast rather than backed off.

    `_ReaderDigestMiddleware` is defined last (innermost, inside the retry) so each attempt
    gets its own digest-candidate scope: a crashed first attempt's fetches are discarded, and
    only the attempt whose digest actually returns marks its sources `digested`.
    """
    return [
        TodoListMiddleware(),
        SummarizationMiddleware(
            model=model, backend=backend, trigger=_SUMMARIZATION_TRIGGER, keep=_SUMMARIZATION_KEEP
        ),
        ToolErrorMiddleware(on_error=_reader_failure_message, tools=["task"]),
        ToolRetryMiddleware(
            max_retries=1,
            tools=["task"],
            on_failure="error",
            initial_delay=0.0,
            jitter=False,
        ),
        _ReaderDigestMiddleware(registry),
    ]
