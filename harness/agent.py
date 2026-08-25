"""Compile the lead research agent: model, tools, workspace backend, and middleware.

The ONLY module that imports `deepagents`: every other module works with plain
LangChain/pydantic types, so the rest of the harness stays independent of the agent
framework's API surface. Same shape as `harness/tools/search.py` — one builder function
closing over `config` and the caller's `registry`, no class.
"""

from collections.abc import Awaitable, Callable
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

from deepagents import (
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
from harness import activity, models
from harness.activity import (
    ActivitySink,
    DisplayError,
    active_reader,
    brief_summary,
    reader_scope,
)
from harness.config import HarnessConfig, run_workspace_dir
from harness.prompts import render
from harness.runlog import RunLog, or_default
from harness.sources import SourceRegistry, pending_digest_scope
from harness.tools import build_tools
from harness.tools.ask_user import ASK_USER_TOOL_NAME
from harness.tools.search import SearchUnavailableError

if TYPE_CHECKING:
    from harness.browser import BrowserSession

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

        Returns `None` for `_PASS_THROUGH_TASK_FAILURES`: a mid-run search abort
        (`SearchUnavailableError`, Drift C) must reach `__main__`'s existing abort handling as a
        raised exception, not be stringified into a soft "... FAILED" message that would hide
        the documented three-consecutive-failures invariant; and a `DisplayError` (Phase 6) is
        the display's fault, not the subagent's, so labelling it "READER FAILED" and recording a
        `subagent_failed` incident would blame the wrong component.
        `ToolErrorMiddleware.on_error` treats a `None` return as "let the exception propagate".
        """
        if isinstance(exc, _PASS_THROUGH_TASK_FAILURES):
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


# The two exceptions that must never be retried NOR converted to a soft "... FAILED"
# ToolMessage — one home, so the retry predicate and the error handler below cannot drift into
# disagreeing about which failures are the subagent's fault. `SearchUnavailableError` is Drift
# C's mid-run abort; `DisplayError` is Phase 6's, and see its own docstring for why.
_PASS_THROUGH_TASK_FAILURES = (SearchUnavailableError, DisplayError)


def _retry_on_non_search_abort(exc: Exception) -> bool:
    """Exclude the pass-through failures from the task-tool retry (Drift C; Phase 6).

    Retrying would waste a whole researcher (or reader) re-run before the abort ever reaches
    `_task_failure_handler`'s propagate branch. `ToolRetryMiddleware.retry_on` accepts this
    predicate form alongside its default exception-tuple shape.
    """
    return not isinstance(exc, _PASS_THROUGH_TASK_FAILURES)


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


def _is_reader_dispatch(name: str | None, args: dict[str, Any]) -> bool:
    """Is this `task` call a dispatch to the reader subagent?

    One predicate for the three sites that all needed to ask this (CLAUDE.md: a third
    repetition gets factored out) -- `_ReaderDispatchCapMiddleware`'s own early return, its
    inner history scan, and `_ToolActivityMiddleware`'s reader/researcher split. `.get`-guarded
    so a malformed or missing `subagent_type` reads as "not a reader dispatch" rather than
    raising.
    """
    return name == "task" and args.get("subagent_type") == "reader"


class _ReaderDispatchCapMiddleware(AgentMiddleware[Any, Any, Any]):
    """Enforce `[agent].max_reader_dispatches` (R5; parent plan's 2026-08-21 Reconciliations
    entry) by refusing a reader dispatch once the researcher's OWN message history already
    carries that many distinct ones — no contextvar, no dispatch counter of its own.

    Counts by POSITION, not a running tally: a model can emit several
    `task(subagent_type="reader")` calls in a single `AIMessage`, so all of them are already in
    `request.state["messages"]` when the first is processed. A raw count would refuse the whole
    batch; indexing into the ordered list of distinct dispatch ids lets the first N through and
    refuses only the surplus, and a `ToolRetryMiddleware` re-invocation of the same
    `tool_call["id"]` (D2) resolves to the same position — hence the same verdict — for free.

    Reading the count from the researcher's own history assumes deepagents' own summarizer
    never rewrites `state["messages"]` -- it DOES run on the researcher tier (via
    `create_deep_agent`'s auto-injected base stack, not the hand-written list above it in
    `_researcher_spec`'s `middleware=`). `_DeepAgentsSummarizationMiddleware` implements only
    `wrap_model_call`/`awrap_model_call`, tracking eviction in a private field rather than
    `before_model`/`after_model` and never issuing a `RemoveMessage` -- a deliberate divergence
    from LangChain's own `SummarizationMiddleware`, which rewrites the list with
    `RemoveMessage(id=REMOVE_ALL_MESSAGES)` from `before_model`. The standing risk: a
    deepagents bump to that LangChain-style mutating behavior would evict old dispatch ids,
    reset the count mid-attempt, and silently uncap this dispatch limit. See the parent plan's
    corrected 2026-08-21 `## Reconciliations` entry.

    A refusal thins this angle's coverage DURING research, so it records a `RunLog` incident
    like every sibling throttle (`guard_blocked`, `domain_blocklisted`, `subagent_failed`) --
    the plan's own D7 rationale puts mid-research thinning on the incident stream, and the
    refused ToolMessage alone leaves disclosure to whether the model chooses to mention it.
    Unlike the round cap and wall clock, this bound is per-angle and has no `CutShortReason`
    to disclose it, so the incident is the only structural trace it can leave.
    """

    def __init__(self, max_dispatches: int, run_log: RunLog | None = None) -> None:
        super().__init__()
        self._max_dispatches = max_dispatches
        self._run_log = or_default(run_log)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call
        args = call["args"] or {}
        if not _is_reader_dispatch(call["name"], args):
            return await handler(request)

        ids: list[str] = []
        state = request.state
        messages = state.get("messages") if isinstance(state, dict) else None
        for message in messages or []:
            tool_calls = getattr(message, "tool_calls", None) or []
            for tool_call in tool_calls:
                if not _is_reader_dispatch(tool_call.get("name"), tool_call.get("args") or {}):
                    continue
                call_id = tool_call.get("id")
                if call_id is not None and call_id not in ids:
                    ids.append(call_id)

        call_id = call.get("id")
        position = ids.index(call_id) if call_id in ids else len(ids)
        if position >= self._max_dispatches:
            self._run_log.record(
                "reader_budget_exhausted",
                f"a researcher's reader dispatch was refused after "
                f"{self._max_dispatches} dispatches; that angle reported "
                "on what it had already read",
            )
            return ToolMessage(
                content=(
                    f"Reader dispatch budget exhausted: {self._max_dispatches} reader "
                    "dispatches already used for this task. No reader was dispatched. "
                    "Report your findings now, including what you could not settle."
                ),
                tool_call_id=call["id"],
                name="task",
            )
        return await handler(request)


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


def _summarize_tool_args(name: str, args: dict[str, Any]) -> str:
    """A short, total description of one tool call's arguments for the activity log (D-C).

    Must never raise on model-supplied args -- every lookup here is `.get`-guarded, and an
    unrecognized tool falls back to its first string-valued argument rather than erroring.
    """
    if name == "task":
        subagent_type = str(args.get("subagent_type") or "")
        # `brief_summary`, not the raw description: a `task` description is the model's full
        # delegation prompt, and this string reaches the plain renderer verbatim (PR #25 review).
        return f"{subagent_type or 'task'} -- {brief_summary(str(args.get('description', '')))}"
    if name == "search_web":
        return str(args.get("query", ""))
    if name in ("fetch_pages", "fetch_raw"):
        urls = args.get("urls") or []
        if not urls:
            return ""
        first_url = str(urls[0])
        extra = len(urls) - 1
        return f"{first_url} +{extra}" if extra else first_url
    for value in args.values():
        if isinstance(value, str):
            return value
    return ""


def _summarize_tool_result(result: ToolMessage | Command[Any]) -> str:
    """A short, honest description of one tool call's result for the activity log (D-C).

    Reuses `_digest_text` to pull the message text out however the tool wrapped it. Takes the
    first line, truncated to ~60 chars: the renderer truncates for display too, so this only
    has to be short and honest, not a full summary.

    The cap is unconditional. A leading-digit line used to be returned whole, on the
    assumption that a line starting with a count is already brief -- but a `task` result is
    free model prose, and a digest opening "1. ..." or "2024 ..." hit that branch and put an
    arbitrarily long string on a one-line log row (PR #25 review).
    """
    text = _digest_text(result)
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    if len(first_line) > 60:
        return first_line[:60].rstrip() + "…"
    return first_line


class _ToolActivityMiddleware(AgentMiddleware[Any, Any, Any]):
    """Report every observed tool call to the run's `ActivitySink` (Phase 6, D-C).

    Registered on the researcher and reader tiers only -- the lead tier is deliberately NOT
    instrumented (developer decision at the 3C gate, 2026-08-20). Innermost in each tier's
    middleware list: `ToolRetryMiddleware` re-invokes the inner handler with the SAME
    `tool_call["id"]` on a retry, which is how the `retry` flag is derived here with no new
    retry bookkeeping of its own.

    Distinguishes a reader dispatch from a researcher dispatch by reading
    `request.tool_call["args"]["subagent_type"]` -- the same way `_task_failure_handler`
    already does -- never by where it is registered (it is registered on both tiers).

    Known limitation: the mockup shows a retry row for `search_web`, but
    `_task_dispatch_guard` scopes its retry middleware to `tools=["task"]`, so the only
    retries this middleware can ever observe are subagent-dispatch retries. Search-level
    retry lives inside the tool itself and is not observable here.

    Async-only, like the tools it observes and like its sibling `_ReaderDigestMiddleware`:
    the graph is driven with `ainvoke`/`astream` (substrate D1).
    """

    def __init__(self, sink: ActivitySink) -> None:
        super().__init__()
        self._sink = sink
        # A retried `task(reader)` dispatch re-invokes this whole method with the SAME
        # `call_id` (D-C): without this, `start_reader` would mint a SECOND `reader/N` row
        # for one dispatch. Local to the middleware, not the sink, since it is bookkeeping
        # about ITS OWN pairing of a call id to the reader id it already minted.
        self._reader_ids_by_call: dict[str, str] = {}

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        name = request.tool_call["name"]
        call_id = str(request.tool_call.get("id") or "")
        if not call_id:
            # No id means no way to pair this start with its eventual finish -- a missing id
            # must never break the dispatch itself.
            return await handler(request)
        args = request.tool_call["args"] or {}
        is_reader = _is_reader_dispatch(name, args)

        retry = self._sink.start_call(call_id, name, _summarize_tool_args(name, args))

        reader_id: str | None = None
        if is_reader:
            if retry and call_id in self._reader_ids_by_call:
                reader_id = self._reader_ids_by_call[call_id]
                # The first attempt already marked this row done/failed (fix-pass item 3):
                # without reopening it, the whole retry would read as a finished reader.
                self._sink.reopen_reader(reader_id)
            else:
                reader_id = self._sink.start_reader(str(args.get("description", "")))
                self._reader_ids_by_call[call_id] = reader_id
        else:
            attributed_to = active_reader()
            if attributed_to is not None:
                self._sink.note_reader_tool(attributed_to, name)

        try:
            if reader_id is not None:
                with reader_scope(reader_id):
                    result = await handler(request)
            else:
                result = await handler(request)
        except BaseException:
            # `BaseException`, not `Exception`: a cancelled dispatch (wall clock) must not
            # leave a row stuck at "running..." either. Re-raised unchanged -- the outer
            # `ToolRetryMiddleware`/`ToolErrorMiddleware` pair owns the run's failure
            # semantics (out of scope: no change to reader dispatch retry/failure behavior).
            self._sink.finish_call(call_id, "failed")
            if reader_id is not None:
                self._sink.finish_reader(reader_id, failed=True)
            raise

        result_summary = _summarize_tool_result(result)
        # No "{label} FAILED"-prefix check here (fix-pass item 8, confirmed dead): that text is
        # only ever produced by `_task_failure_handler`, `_task_dispatch_guard`'s
        # `ToolErrorMiddleware.on_error` callback, which sits OUTSIDE this middleware in the
        # chain and crafts its substitute ToolMessage after this middleware's own `except`
        # branch above has already fired and re-raised -- deepagents' `task`/`atask` never
        # catches a subgraph crash and returns it as a normal string either (it lets the
        # exception propagate). A non-exception return here is therefore always a success.
        self._sink.finish_call(call_id, result_summary)
        if reader_id is not None:
            self._sink.finish_reader(reader_id, failed=False)
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
    sink: ActivitySink,
) -> SubAgent:
    """Build the declared `reader` `SubAgent` spec (D1): the researcher's only route to
    `fetch_pages` (Step 3 nested this one level deeper — the lead never dispatches it directly).

    `interrupt_on` is deliberately left unset — the reader has no checkpointer forwarded and
    cannot interrupt, so inheriting an ancestor's `ask_user` entry would register an interrupt
    that can never fire.

    `middleware` restores PART of the base stack `create_deep_agent`'s top-level `subagents=`
    path auto-injects for every declared subagent (`graph.py`: `FilesystemMiddleware`,
    `create_summarization_middleware`, `PatchToolCallsMiddleware`) — nesting the reader via a
    hand-built `SubAgentMiddleware` (Step 3) bypasses that path entirely, so a manually
    nested spec gets NONE of it for free and every entry needed must be re-added here
    explicitly. `FilesystemMiddleware` (and the scratch workspace it backed) is deliberately
    OMITTED (R6, Phase 4 trim): the reader has no write tools, `reader.md` no longer promises
    a scratch workspace, and the only remaining reader toolset is `fetch_pages`.
    `create_summarization_middleware` stays so a reader digesting large fetched pages gets
    context-window eviction instead of a provider context-length error (PR #18 review) — it
    holds its own `backend` reference and offloads evicted history through it independent of
    `FilesystemMiddleware`. `PatchToolCallsMiddleware` matches the injected stack. Mirrors only
    the `backend`/`model` arguments from that site: this harness never sets
    `tool_description_overrides` or per-subagent `permissions`, so the other
    `FilesystemMiddleware` kwargs there would always have been `None` anyway.

    Two further consequences of dropping `FilesystemMiddleware` (review fix F6): it also
    evicted oversized tool RESULTS to disk (`tool_token_limit_before_evict` default 20000
    tokens, ~80,000 chars at deepagents' `NUM_CHARS_PER_TOKEN=4`), and `fetch_pages` was never
    in its `TOOLS_EXCLUDED_FROM_EVICTION` list — so the reader has lost that safety net, and
    already at THIS harness's own default `[fetch].per_page_char_cap` (120000): one fetched
    page at the cap alone already exceeds the 80,000-char threshold, before a second URL is
    even joined in. Separately, the summarization offload above still writes evicted history
    to disk and still tells the model it may `read_file` the saved path — a tool the reader no
    longer has; a dead `read_file` attempt there is expected degradation, not a new bug.

    `_ToolActivityMiddleware(sink)` is appended last (Phase 6, D-C): this is the ONLY tier
    where `fetch_pages` is observable, since the reader is the only tier that calls it.
    """
    # Explicitly typed, matching `_middleware`'s own convention: `create_summarization_middleware`
    # returns a middleware with its state type param fixed, and an unannotated list literal here
    # infers that concrete type instead of the broad `AgentMiddleware[Any, Any, Any]`
    # `SubAgent["middleware"]` expects, which mypy then rejects as a list-item mismatch.
    reader_middleware: list[AgentMiddleware[Any, Any, Any]] = [
        create_summarization_middleware(reader_model, backend),
        PatchToolCallsMiddleware(),
        _ToolActivityMiddleware(sink),
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
    sink: ActivitySink,
) -> SubAgent:
    """Build the declared `researcher` `SubAgent` spec (Step 3): the lead's only route to
    `search_web` and (through its own nested `reader` declaration) page reading.

    `interrupt_on` is deliberately left unset (D6): interrupts are pinned off below the lead —
    a nested researcher has no checkpointer forwarded and cannot interrupt.

    `middleware`: `SubAgentMiddleware` nests the reader tier under THIS researcher's own
    `task` tool; `_ReaderDispatchCapMiddleware` (R5, Phase 4) sits immediately after it and
    before `_task_dispatch_guard`, so a refused dispatch short-circuits the retry guard, the
    digest scope, and the activity sink — nothing scopes a reader that never ran, and the
    refusal's own `RunLog` incident is the one trace it leaves;
    `_task_dispatch_guard` guards a crashed (or aborted, Drift C) reader dispatch, the same
    shared pair as the lead's own guard on dispatching a researcher; `_ReaderDigestMiddleware`
    marks a source `digested` only when a reader's digest actually reaches this researcher
    (R7's mechanism moved, not broken); and `_ToolActivityMiddleware` (Phase 6, D-C) is
    appended LAST (innermost) so a retried `task` dispatch to the reader arrives as a second
    start for the same `tool_call["id"]`.
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
            max_reader_dispatches=config.agent.max_reader_dispatches,
        ),
        model=researcher_model,
        tools=researcher_tools,
        middleware=[
            SubAgentMiddleware(backend=backend, subagents=[reader_spec]),
            _ReaderDispatchCapMiddleware(config.agent.max_reader_dispatches, run_log),
            *_task_dispatch_guard(run_log),
            _ReaderDigestMiddleware(registry),
            _ToolActivityMiddleware(sink),
        ],
    )


def build_agent(
    config: HarnessConfig,
    registry: SourceRegistry,
    run_log: RunLog | None = None,
    sink: ActivitySink | None = None,
    browser: "BrowserSession | None" = None,
) -> Runnable:
    """Compile the lead research agent, driven with `ainvoke`/`astream` (substrate D1).

    The research question is NOT baked into the system prompt: this signature has no access to
    it. It travels as the initial `HumanMessage` the caller streams in, and the rendered
    orchestrator prompt carries only `$current_date` — the lead no longer manages URL batching
    itself (Step 3 moved that detail down onto the researcher/reader tiers).

    `run_log` collects the tools' degraded-coverage incidents; the caller shares one instance
    between this agent and the report so disclosure sees everything (best-effort + disclose).
    `sink` (Phase 6, D-C) collects the researcher/reader tiers' observed tool calls for the
    running pane's structured log and reader strip -- the lead tier is deliberately not
    instrumented, so `_middleware` below is unchanged. `browser` (Phase 1, R2) is forwarded
    to `build_tools` unchanged -- this function has no lifecycle over it, `main()` owns
    start/close.
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

    tool_sets = build_tools(config, registry, run_log, browser)

    system_prompt = render(
        "orchestrator",
        current_date=date.today().isoformat(),
    )

    shared_log = or_default(run_log)
    # The SAME sink goes to both specs (D-D): reader-tier attribution needs the reader's own
    # middleware updating the very `ReaderState` the researcher's middleware created.
    shared_sink = activity.or_default(sink)
    reader_spec = _reader_spec(config, reader_model, tool_sets.reader, backend, shared_sink)
    researcher_spec = _researcher_spec(
        config,
        researcher_model,
        tool_sets.researcher,
        reader_spec,
        backend,
        registry,
        shared_log,
        shared_sink,
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
