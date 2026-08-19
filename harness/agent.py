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
    FilesystemMiddleware,
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    SubAgent,
    SubAgentMiddleware,
    create_deep_agent,
    register_harness_profile,
)
from deepagents._models import get_model_identifier, get_model_provider
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.summarization import (
    SummarizationMiddleware,
    create_summarization_middleware,
)
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
from harness.runlog import RunLog, or_default
from harness.sources import SourceRegistry, pending_digest_scope
from harness.tools import build_tools
from harness.tools.ask_user import ASK_USER_TOOL_NAME
from harness.tools.search import SearchUnavailableError

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
# hardcoded: this middleware wraps EVERY `task` dispatch, so the researcher tier fails as
# "RESEARCHER FAILED" without touching this function, the prompt contract, or its pinned test.
def _task_failure_handler(
    run_log: RunLog,
) -> Callable[[Exception, ToolCallRequest], str | None]:
    """Build the `ToolErrorMiddleware.on_error` handler for a `task` dispatch.

    A factory rather than a plain function so the handler closes over the run's shared
    `RunLog`: a swallowed subagent crash is degraded coverage, and best-effort + disclose
    requires the CAUSE to reach the terminal and the report's `## Gaps and disclosures`, not
    just the effect (a source left "unread", or an answer with no sources at all). Without
    this the only trace was a `... FAILED` ToolMessage the model saw and the operator did not.
    """

    def _handle(exc: Exception, request: ToolCallRequest) -> str | None:
        """Render a subagent (`task`) crash as content for an error `ToolMessage`.

        Returns `None` for `SearchUnavailableError` (and only it — Drift C): a mid-run search
        abort must reach `__main__`'s existing abort handling as a raised exception, not be
        stringified into a soft "... FAILED" message that would hide the documented
        three-consecutive-failures invariant. `ToolErrorMiddleware.on_error` treats a `None`
        return as "let the exception propagate".
        """
        if isinstance(exc, SearchUnavailableError):
            return None
        # `.get`: the args are model-supplied, and a malformed call must still get a label
        # rather than raising a KeyError out of the error handler itself.
        subagent_type = str(request.tool_call["args"].get("subagent_type", "task"))
        label = subagent_type.upper()
        run_log.record(
            "subagent_failed",
            f"{subagent_type} dispatch failed ({type(exc).__name__}): {exc}",
        )
        return f"{label} FAILED ({type(exc).__name__}): {exc}"

    return _handle


def _retry_on_non_search_abort(exc: Exception) -> bool:
    """Exclude `SearchUnavailableError` from the task-tool retry (Drift C).

    Retrying would waste a whole researcher (or reader) re-run before the abort ever reaches
    `_task_failure_handler`'s propagate branch. `ToolRetryMiddleware.retry_on` accepts this
    predicate form alongside its default exception-tuple shape.
    """
    return not isinstance(exc, SearchUnavailableError)


def _task_dispatch_guard(run_log: RunLog) -> list[AgentMiddleware[Any, Any, Any]]:
    """The ToolError/ToolRetry pair guarding one tier's `task` dispatch (D2).

    One definition for the two dispatch sites — the lead dispatching a researcher
    (`_middleware`) and the researcher dispatching a reader (`_researcher_spec`) — so the
    retry policy cannot drift between them. `ToolErrorMiddleware` defined first (outermost)
    catches whatever exception exhausts `ToolRetryMiddleware` (inner, `on_failure="error"` so
    the exhausted exception reaches the outer catch rather than being swallowed into a
    "continue" message) and converts it to a `status="error"` ToolMessage. `max_retries=1`:
    retrying `task` re-runs the whole subagent, so the budget already doubles at one retry.
    `initial_delay=0.0, jitter=False`: this retry exists for subagent crashes, not transient
    network waits, so it should be deterministic and test-fast rather than backed off.
    `retry_on` excludes `SearchUnavailableError` (Drift C) so a mid-run search abort is not
    wastefully retried through a whole subagent re-run before it reaches
    `_task_failure_handler`'s propagate branch.
    """
    return [
        ToolErrorMiddleware(on_error=_task_failure_handler(run_log), tools=["task"]),
        ToolRetryMiddleware(
            max_retries=1,
            tools=["task"],
            on_failure="error",
            initial_delay=0.0,
            jitter=False,
            retry_on=_retry_on_non_search_abort,
        ),
    ]


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
    """Promote reader-fetched sources to `digested` only when a digest reaches the researcher
    that dispatched it (R5, relocated from the lead in Step 3 — the mechanism, not the
    semantics, moved).

    The reader's `fetch_pages` call only NOMINATES the source IDs it captured
    (`sources.note_digest_candidate`, context-local per task attempt); this middleware marks
    them when — and only when — the attempt returns a non-empty digest. A crash (converted to
    a `... FAILED` error ToolMessage by the outer `ToolErrorMiddleware`) or an empty digest
    leaves them "unread", so the report never discloses a digest nobody ever received.

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
    config: HarnessConfig,
    reader_model: BaseChatModel,
    reader_tools: list[BaseTool],
    backend: BackendProtocol,
) -> SubAgent:
    """Build the declared `reader` `SubAgent` spec (D1): the researcher's only route to
    `fetch_pages` (Step 3 nested this one level deeper — the lead never dispatches it directly).

    `interrupt_on` is deliberately left unset — the reader has no checkpointer forwarded and
    cannot interrupt, so inheriting an ancestor's `ask_user` entry would register an interrupt
    that can never fire.

    `middleware` restores the base stack `create_deep_agent`'s top-level `subagents=` path
    auto-injects for every declared subagent (`graph.py`: `FilesystemMiddleware`,
    `create_summarization_middleware`, `PatchToolCallsMiddleware`) — nesting the reader via a
    hand-built `SubAgentMiddleware` (Step 3) bypasses that path entirely, so a manually
    nested spec gets NONE of it for free and every entry must be re-added here explicitly:
    `FilesystemMiddleware` for the scratch workspace `reader.md` promises
    (`write_file`/`read_file`/`edit_file`/`ls`/`glob`/`grep`), `create_summarization_middleware`
    so a reader digesting large fetched pages gets context-window eviction instead of a
    provider context-length error (PR #18 review), and `PatchToolCallsMiddleware` matching the
    injected stack. Mirrors only the `backend`/`model` arguments from that site: this harness
    never sets `tool_description_overrides` or per-subagent `permissions`, so the other
    `FilesystemMiddleware` kwargs there are always `None` and add nothing here.
    """
    # Explicitly typed, matching `_middleware`'s own convention: `FilesystemMiddleware`'s state
    # type param is fixed to `FilesystemState`, and an unannotated list literal here infers that
    # concrete type instead of the broad `AgentMiddleware[Any, Any, Any]` `SubAgent["middleware"]`
    # expects, which mypy then rejects as a list-item mismatch.
    reader_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        FilesystemMiddleware(backend=backend),
        create_summarization_middleware(reader_model, backend),
        PatchToolCallsMiddleware(),
    ]
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
        middleware=reader_middleware,
    )


def _researcher_spec(
    config: HarnessConfig,
    researcher_model: BaseChatModel,
    researcher_tools: list[BaseTool],
    reader_spec: SubAgent,
    backend: BackendProtocol,
    registry: SourceRegistry,
    run_log: RunLog,
) -> SubAgent:
    """Build the declared `researcher` `SubAgent` spec (Step 3): the lead's only route to
    `search_web` and (through its own nested `reader` declaration) page reading.

    `interrupt_on` is deliberately left unset (D6): interrupts are pinned off below the lead —
    a nested researcher has no checkpointer forwarded and cannot interrupt.

    `middleware`: `SubAgentMiddleware` nests the reader tier under THIS researcher's own
    `task` tool; `_task_dispatch_guard` guards a crashed (or aborted, Drift C) reader
    dispatch, the same shared pair as the lead's own guard on dispatching a researcher; and
    `_ReaderDigestMiddleware` marks a source `digested` only when a reader's digest actually
    reaches this researcher (R7's mechanism moved, not broken).
    """
    return SubAgent(
        name="researcher",
        description=(
            "Researches one assigned angle of the question: searches the web and delegates "
            "page reading to the reader, returning a source-cited report of its findings."
        ),
        system_prompt=render(
            "subagent",
            current_date=date.today().isoformat(),
            max_urls_per_call=config.fetch.max_urls_per_call,
        ),
        model=researcher_model,
        tools=researcher_tools,
        middleware=[
            SubAgentMiddleware(backend=backend, subagents=[reader_spec]),
            *_task_dispatch_guard(run_log),
            _ReaderDigestMiddleware(registry),
        ],
    )


def build_agent(
    config: HarnessConfig, registry: SourceRegistry, run_log: RunLog | None = None
) -> Runnable:
    """Compile the lead research agent, driven with `ainvoke`/`astream` (substrate D1).

    The research question is NOT baked into the system prompt: this signature has no access to
    it. It travels as the initial `HumanMessage` the caller streams in, and the rendered
    orchestrator prompt carries only `$current_date` — the lead no longer manages URL batching
    itself (Step 3 moved that detail down onto the researcher/reader tiers).

    `run_log` collects the tools' degraded-coverage incidents; the caller shares one instance
    between this agent and the report so disclosure sees everything (best-effort + disclose).
    """
    model = models.build_chat_model(config, "head")
    _register_no_shell_profile(model)

    researcher_model = models.build_chat_model(config, "researcher")
    _register_no_shell_profile(researcher_model)

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
    )

    shared_log = or_default(run_log)
    reader_spec = _reader_spec(config, reader_model, tool_sets.reader, backend)
    researcher_spec = _researcher_spec(
        config, researcher_model, tool_sets.researcher, reader_spec, backend, registry, shared_log
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
        middleware=_middleware(model, backend, shared_log),
        subagents=[researcher_spec],
        # One saver per call, holding this run's thread. In-memory keeps the no-database
        # invariant (D5): no durable, cross-invocation checkpointing.
        checkpointer=InMemorySaver(),
        interrupt_on=_INTERRUPT_ON,
    )


def _middleware(
    model: Any, backend: BackendProtocol, run_log: RunLog
) -> list[AgentMiddleware[Any, Any, Any]]:
    """Build the middleware list with an explicit, broad element type.

    Without the annotation mypy unifies the element type from the first entry and rejects the
    second — a false positive with heterogeneous lists of generic `AgentMiddleware` subclasses.

    The summarizer is deepagents', not langchain's plain one. Both publish the same `.name`, so
    `create_deep_agent` still merges down to one summarizer, but only the deepagents wrapper
    offloads evicted messages to `backend` before dropping them from context — langchain's
    issues a destructive `RemoveMessage(REMOVE_ALL_MESSAGES)` with no recovery path for a
    dropped `[Sn]`-to-finding association, which D7/R3/R7 depend on.

    `_task_dispatch_guard` (D2) scopes to the `task` tool only and here guards the LEAD's
    dispatch of a RESEARCHER (Step 3 relocated `_ReaderDigestMiddleware` down onto the
    researcher's own middleware, since its subject — a reader's digest — is nested one level
    deeper); the policy itself is documented on the shared helper.
    """
    return [
        TodoListMiddleware(),
        SummarizationMiddleware(
            model=model, backend=backend, trigger=_SUMMARIZATION_TRIGGER, keep=_SUMMARIZATION_KEEP
        ),
        *_task_dispatch_guard(run_log),
    ]
