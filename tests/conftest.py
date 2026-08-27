"""Shared test fixtures for the harness suite."""

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr, SecretStr

from harness.config import (
    AgentSettings,
    BlocklistSettings,
    FetchSettings,
    GuardSettings,
    HarnessConfig,
    ProviderConfig,
    RoleConfig,
    SearchSettings,
    run_workspace_dir,
)
from harness.sources import sources_dir

CHALLENGE_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "challenge"


class _FakeMarkdown:
    """Stand-in for crawl4ai's StringCompatibleMarkdown, exposing raw and fit variants."""

    def __init__(self, raw_markdown: str = "", fit_markdown: str = "") -> None:
        self.raw_markdown = raw_markdown
        self.fit_markdown = fit_markdown


class _FakeResult:
    """Stand-in for crawl4ai's CrawlResult, exposing only the attributes fetch.py reads."""

    def __init__(
        self,
        url: str,
        *,
        error_message: str | None = None,
        status_code: int | None = 200,
        response_headers: dict | None = None,
        metadata: dict | None = None,
        markdown: _FakeMarkdown | None = None,
    ) -> None:
        self.url = url
        self.error_message = error_message
        self.status_code = status_code
        self.response_headers = response_headers
        self.metadata = metadata
        self.markdown = markdown


class _FakePDFCrawlerStrategy:
    """Stand-in for crawl4ai's `PDFCrawlerStrategy` — the PDF-seam construction marker."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class _FakePDFContentScrapingStrategy:
    """Stand-in for crawl4ai's `PDFContentScrapingStrategy`."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class _FakeHTTPCrawlerStrategy:
    """Stand-in for crawl4ai's `AsyncHTTPCrawlerStrategy` — the HTTP-seam construction marker."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.kwargs = kwargs


class _FakeHTTPCrawlerConfig:
    """Stand-in for crawl4ai's `HTTPCrawlerConfig`.

    Retains `kwargs` for the same reason `_FakeHTTPCrawlerStrategy` does: it is reachable as
    `fake_cls.http_strategies[0].kwargs["browser_config"].kwargs`, which is how a test asserts
    `downloads_path` was contained inside the workspace rather than left at crawl4ai's
    `~/.crawl4ai/downloads` default.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.kwargs = kwargs


def _make_fake_crawler_class(
    results: list[_FakeResult],
    pdf_results: list[_FakeResult] | None = None,
    http_results: list[_FakeResult] | None = None,
    fail_batches: int = 0,
) -> type:
    """Build a fake AsyncWebCrawler class recording construction and `arun_many` calls.

    One fake class serves the Playwright batch, the HTTP batch, and the PDF batch — `_fetch`
    gets all three from the same `_crawler_class()` seam, distinguished only by the
    `crawler_strategy` kwarg passed at construction. `pdf_results`/`http_results` (each
    defaulting to `results`, so existing single-arg callers are unaffected) let a test give
    the PDF batch and/or the HTTP pass their own canned results distinct from the Playwright
    (browser) batch's.

    `fail_batches` (default 0, so every existing caller is unaffected) makes the first N calls
    to `arun_many` — across ALL instances of this class, matching crawl4ai's real dead-handle
    behavior surfacing on whichever instance is current — raise `RuntimeError` instead of
    recording/returning anything, for `harness.browser.BrowserSession`'s relaunch path to
    relaunch from. It means "the Chromium handle is dead" and drives `BrowserSession`'s
    relaunch, so it is gated to non-HTTP instances — an HTTP call consuming one would silently
    change what Phase 1's relaunch tests exercise. `start`/`close` are real coroutines (not
    just `__aenter__`/`__aexit__`) so `BrowserSession` can drive the fake directly; `closed`
    records each `close()` call.

    `arun_many` yields (`asyncio.sleep`) before doing anything, but ONLY when `fail_batches > 0`
    (and not for an HTTP instance, for the same reason as above): with no real `await` in its
    body it never actually suspends, so two `asyncio.gather`-driven calls (Phase 1 fix #3's
    concurrency test) would run one to completion before the other's Task ever got a turn —
    same reasoning as `ConcurrencyTrackingModel`'s `_sleep_seconds`. Gated to the relaunch
    scenario alone so the rest of the suite, which never sets `fail_batches`, pays nothing.
    """

    class _FakeCrawler:
        constructed_with: list[object] = []
        constructed_kinds: list[str] = []
        # The `_FakeHTTPCrawlerStrategy` instance passed at construction, one per HTTP
        # construction -- how a test reaches its `.kwargs` to assert `max_connections` was
        # wired from `config.fetch.max_concurrency` (D6).
        http_strategies: list[object] = []
        calls: list[SimpleNamespace] = []
        closed: list[object] = []
        _fail_remaining: int = fail_batches
        _yield_on_call: bool = fail_batches > 0

        def __init__(self, config: object = None, crawler_strategy: object = None) -> None:
            _FakeCrawler.constructed_with.append(config)
            self._is_pdf = isinstance(crawler_strategy, _FakePDFCrawlerStrategy)
            self._is_http = isinstance(crawler_strategy, _FakeHTTPCrawlerStrategy)
            if self._is_http:
                _FakeCrawler.http_strategies.append(crawler_strategy)
            _FakeCrawler.constructed_kinds.append(
                "pdf" if self._is_pdf else "http" if self._is_http else "browser"
            )

        async def __aenter__(self) -> "_FakeCrawler":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            _FakeCrawler.closed.append(self)

        async def arun_many(
            self, urls: list[str], config: object = None, dispatcher: object = None
        ) -> list[_FakeResult]:
            if _FakeCrawler._yield_on_call and not self._is_http:
                await asyncio.sleep(0.05)
            if _FakeCrawler._fail_remaining > 0 and not self._is_http:
                _FakeCrawler._fail_remaining -= 1
                raise RuntimeError("crawler handle is dead")
            _FakeCrawler.calls.append(
                SimpleNamespace(
                    urls=urls,
                    config=config,
                    dispatcher=dispatcher,
                    is_pdf=self._is_pdf,
                    is_http=self._is_http,
                )
            )
            if self._is_pdf:
                return pdf_results if pdf_results is not None else results
            if self._is_http:
                return http_results if http_results is not None else results
            return results

    return _FakeCrawler


@pytest.fixture
def install_crawler(monkeypatch):
    """Patch `harness.tools.fetch._crawler_class`/`_pdf_crawler_parts`/`_http_crawler_parts`
    with fakes.

    `_crawler_class` (not a module-level `AsyncWebCrawler` name) because fetch.py imports
    crawl4ai lazily inside `_fetch` — the function is the deliberate patch seam. Patches
    fetch.py's namespace regardless of caller: `fallback.py` reuses fetch.py's `_fetch`,
    and that is where the crawler is actually constructed, so both fetch and fallback
    tests share this one fixture. `_pdf_crawler_parts` is the parallel seam for the PDF
    strategy classes fetch.py's PDF batch constructs; `_http_crawler_parts` is the parallel
    seam for the HTTP-first pass, used by both `_fetch`'s per-call fallback and
    `BrowserSession.start`'s warm crawler.

    Also the fixture `harness.browser.BrowserSession` tests use: it calls the very same
    `_crawler_class()` seam (see `browser.py`'s module docstring), so `fail_batches` (see
    `_make_fake_crawler_class`) lets a browser test script a dead-handle relaunch without a
    second, parallel fixture.
    """

    def _install(
        results: list[_FakeResult],
        pdf_results: list[_FakeResult] | None = None,
        http_results: list[_FakeResult] | None = None,
        fail_batches: int = 0,
    ) -> type:
        fake_cls = _make_fake_crawler_class(results, pdf_results, http_results, fail_batches)
        monkeypatch.setattr("harness.tools.fetch._crawler_class", lambda: fake_cls)
        monkeypatch.setattr(
            "harness.tools.fetch._pdf_crawler_parts",
            lambda: (_FakePDFCrawlerStrategy, _FakePDFContentScrapingStrategy),
        )
        monkeypatch.setattr(
            "harness.tools.fetch._http_crawler_parts",
            lambda: (_FakeHTTPCrawlerStrategy, _FakeHTTPCrawlerConfig),
        )
        return fake_cls

    return _install


class ScriptedChatModel(ChatOpenAI):
    """A `ChatOpenAI` subclass that plays back a scripted list of `AIMessage` replies.

    Subclasses `ChatOpenAI` rather than `GenericFakeChatModel` because `harness.agent` derives
    its deepagents profile key from `get_model_provider`/`get_model_identifier`, which read
    `model_name` and `_get_ls_params()`; `GenericFakeChatModel` supplies neither and would fail
    profile resolution for reasons unrelated to the code under test.

    Only `_generate`/`_agenerate` are overridden, so no network call happens while every other
    `ChatOpenAI` behavior stays real. Each call's `messages` are recorded on `_received_messages`,
    oldest first, so a test can assert on what actually reached the model.
    """

    _script: list[AIMessage] = PrivateAttr(default_factory=list)
    _call_count: int = PrivateAttr(default=0)
    _bound_tool_names: list[list[str]] = PrivateAttr(default_factory=list)
    _received_messages: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def script(self, responses: list[AIMessage]) -> "ScriptedChatModel":
        """Queue `responses`, one per model call, oldest first. Resets the call count."""
        self._script = list(responses)
        self._call_count = 0
        return self

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: dict[str, Any] | str | bool | None = None,
        strict: bool | None = None,
        parallel_tool_calls: bool | None = None,
        response_format: Any = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        """Record the tool names offered on each bind, then delegate to the real schema logic.

        Lets a test assert on the schema the model was offered (e.g. that `execute` never
        appears) without inspecting deepagents internals.
        """
        names: list[str] = []
        for entry in tools:
            if isinstance(entry, dict):
                function = entry.get("function", entry)
                name = function.get("name")
            else:
                name = getattr(entry, "name", None)
            if name:
                names.append(name)
        self._bound_tool_names.append(sorted(names))
        return super().bind_tools(
            tools,
            tool_choice=tool_choice,
            strict=strict,
            parallel_tool_calls=parallel_tool_calls,
            response_format=response_format,
            **kwargs,
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._received_messages.append(list(messages))
        # A fresh copy with a per-call id, like a real model: scripts commonly repeat one
        # AIMessage object (`[keep_going] * 20`), and reusing it verbatim gives every turn the
        # SAME message id — which `__main__`'s round counter deduplicates by, so the run would
        # count one round no matter how many turns actually happened.
        response = self._script[self._call_count].model_copy(deep=True)
        if response.id is None:
            response.id = f"scripted-{self._call_count}"
        self._call_count += 1
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # `_generate` records `messages` itself, so the async path needs no second recording.
        return self._generate(messages, stop=stop, **kwargs)


class ConcurrencyTrackingModel(ScriptedChatModel):
    """A `ScriptedChatModel` that tracks in-flight `_agenerate` calls, for asserting on peak
    concurrency in either direction.

    `_sleep_seconds` is load-bearing both ways. Proving calls are SEQUENTIAL
    (test_verify.py, D4) needs only `0`: with a single-tick yield, even a concurrent
    `asyncio.gather` reveals itself, and anything longer just slows the suite. Proving calls
    are CONCURRENT (test_agent.py) needs a real, non-zero yield — with `sleep(0)` the two
    gathered coroutines' scheduling can still interleave such that one fully completes before
    the other's Task gets its first turn (observed flaky in this suite); `0.05` reliably gives
    both a chance to increment before either decrements. Set it by assigning the private attr
    after construction: `model._sleep_seconds = 0.05`.
    """

    _in_flight: int = PrivateAttr(default=0)
    _peak_in_flight: int = PrivateAttr(default=0)
    _sleep_seconds: float = PrivateAttr(default=0.0)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._in_flight += 1
        self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
        await asyncio.sleep(self._sleep_seconds)
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        finally:
            self._in_flight -= 1


def patch_model(monkeypatch: pytest.MonkeyPatch, model: Any) -> None:
    """Point `build_chat_model` at `model` for every caller.

    One target: `harness.agent` and `harness.verify` call it as a module attribute
    (`models.build_chat_model(...)`, resolved at call time) rather than importing it by
    value, so patching the definition covers them all — including any future caller,
    which a hand-maintained target list silently missed.
    """
    monkeypatch.setattr("harness.models.build_chat_model", lambda cfg, role: model)


def patch_models_by_role(monkeypatch: pytest.MonkeyPatch, models: dict[str, Any]) -> None:
    """Like `patch_model`, but returns a different model per role (e.g. head vs reader).

    A role missing from `models` falls back to `head`, so a test that cares about two roles
    lists two rather than every role the run happens to build a client for. The ONE definition
    of that fallback — `patch_run_by_role` and the fixture below both come through here, so a
    test can never see two different answers for an unlisted role.
    """
    monkeypatch.setattr(
        "harness.models.build_chat_model", lambda cfg, role: models.get(role, models["head"])
    )


@pytest.fixture(name="patch_models_by_role")
def _patch_models_by_role_fixture(monkeypatch: pytest.MonkeyPatch):
    """The fixture form of `patch_models_by_role`, with `monkeypatch` already bound."""

    def _patch(models: dict[str, Any]) -> None:
        patch_models_by_role(monkeypatch, models)

    return _patch


def patch_run(
    monkeypatch: pytest.MonkeyPatch,
    config: HarnessConfig,
    model: Any,
    *,
    skip_preflight: bool = True,
    run_search_preflight: bool = False,
    run_browser_preflight: bool = False,
) -> None:
    """Patch everything a `main()` test reaches outside the compiled graph.

    `skip_preflight` replaces `preflight` (the model check) with a no-op. `True` by default:
    each preflighted role (`head`, `verifier`, and whatever Phase 2 adds) makes its own real
    `ainvoke` against the scripted model, consuming one scripted reply per role before the
    graph ever runs — a test that does not care about preflight itself would otherwise have to
    keep its script in lockstep with however many roles happen to be preflighted today. Pass
    `skip_preflight=False` for a test that asserts something about preflight itself (or about a
    scripted reply preflight is meant to consume) and script a leading reply per preflighted
    role.

    The search preflight (`preflight_search`) is a real HTTP probe against `config.search
    .base_url`, which has no scripted-model equivalent — most `main()` tests never touch
    `search_web` and have no transport installed, so it is ALWAYS neutralized here by default
    (regardless of `skip_preflight`). Pass `run_search_preflight=True` to let the real probe run
    instead (install a search transport via `install_search_transport` before calling `main()`).

    The browser preflight (`BrowserSession.start`) is a real Chromium launch, same story: most
    `main()` tests never fetch anything, so a real browser launch per test is exactly what the
    fixture-based suite exists to avoid, and it is neutralized here by default too. Pass
    `run_browser_preflight=True` for a test that asserts something about the browser preflight
    itself (or installs its own `BrowserSession.start`/`close` patch and needs this one to stay
    out of the way).
    """
    import harness.__main__ as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    if skip_preflight:

        async def _noop_preflight(cfg: HarnessConfig, role: str) -> None:
            return None

        # At the source module, not `main_module`: `main` imports `preflight` at call time
        # (the heavy-import deferral), so the patched attribute is what that import binds.
        monkeypatch.setattr("harness.models.preflight", _noop_preflight)
    if not run_search_preflight:

        async def _noop_search_preflight(cfg: HarnessConfig) -> None:
            return None

        # On `main_module`, NOT the source module — unlike `preflight`, this one is imported
        # by value at module import time (it is cheap, so it stays out of the deferred block),
        # so `harness.tools.search.preflight_search` is not the name `main` calls.
        monkeypatch.setattr(main_module, "preflight_search", _noop_search_preflight)
    if not run_browser_preflight:
        from harness.browser import BrowserSession

        async def _noop_browser_start(self: BrowserSession) -> None:
            return None

        # On the CLASS, not a module attribute: `main_module` holds a module-level reference
        # to `BrowserSession` itself, so patching the class's method covers every binding
        # regardless of how it was imported.
        monkeypatch.setattr(BrowserSession, "start", _noop_browser_start)
    patch_model(monkeypatch, model)


# The phrase both synthesis instructions share (`_SYNTHESIZE_NOW_INSTRUCTION`), which is how
# `_LeadModel` recognizes the bounded synthesis pass without pinning either full wording.
_SYNTHESIS_PHRASE = "Stop researching now"


class _LeadModel(ScriptedChatModel):
    """A scripted LEAD model that understands the session's two control tools (D1/D3).

    Three things a plain positional script cannot express now that the lead's turns are driven
    by an event loop rather than one long stream:

    - `_answer`: on seeing the synthesis instruction it calls `submit_report(_answer)`, because
      a cut-short run writes NO report without that call (D3) and which scripted index the
      bounded synthesis pass lands on is a langgraph topology detail, not a test's business.
    - `_after_submit`: what it says once it has submitted — text ends the pass, a tool call
      keeps it going, which is how the runaway-synthesis case is scripted.
    - `_slow_calls` / `_delay_seconds`: hold a chosen call long enough that a wall clock armed
      earlier in the same turn expires while the lead is still inside the model.

    `_replies` are its ordinary turns, in order; the last one repeats.
    """

    _replies: list = PrivateAttr(default_factory=list)
    _answer: str = PrivateAttr(default="")
    _after_submit: Any = PrivateAttr(default=None)
    _slow_calls: frozenset = PrivateAttr(default=frozenset())
    _delay_seconds: float = PrivateAttr(default=3.0)
    _submitted: bool = PrivateAttr(default=False)
    _plain_calls: int = PrivateAttr(default=0)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        self._received_messages.append(list(messages))
        if self._call_count in self._slow_calls:
            await asyncio.sleep(self._delay_seconds)
        if self._submitted:
            reply = self._after_submit or AIMessage(content="Report submitted.")
        elif any(_SYNTHESIS_PHRASE in str(m.content) for m in messages):
            self._submitted = True
            reply = _submit_call(self._answer)
        else:
            reply = self._replies[min(self._plain_calls, len(self._replies) - 1)]
            self._plain_calls += 1
            if any(call["name"] == "submit_report" for call in reply.tool_calls):
                self._submitted = True
        self._call_count += 1
        # A per-call id, like a real model: the round counter deduplicates by message id, so a
        # repeated reply object would count as one round no matter how many turns happened.
        reply = reply.model_copy(deep=True, update={"id": f"lead-{self._call_count}"})
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _lead_model(**private: Any) -> _LeadModel:
    """A `_LeadModel` on a throwaway endpoint, with its private attrs set."""
    from pydantic import SecretStr

    model = _LeadModel(
        model="head-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    )
    for name, value in private.items():
        setattr(model, f"_{name}", value)
    return model


def patch_run_by_role(
    monkeypatch: pytest.MonkeyPatch,
    config: HarnessConfig,
    models: dict[str, Any],
    **kwargs: Any,
) -> None:
    """`patch_run`, but with a distinct model per role (unlisted roles fall back to `head`).

    Needed by every `main()` test that reaches the researcher tier: a lead turn no longer
    blocks on its researchers (PLAN-interactive-lead-chat D1), so the two tiers' model calls
    interleave and one shared script would be consumed in a nondeterministic order.

    `patch_run` first for everything else it neutralizes (config, the preflights), then
    `patch_models_by_role` over the top — it owns the role lookup and its fallback.
    """
    patch_run(monkeypatch, config, models["head"], **kwargs)
    patch_models_by_role(monkeypatch, models)


def install_search_transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[..., Any]) -> None:
    """Route `harness.tools.search`'s `httpx.AsyncClient` through a MockTransport on `handler`.

    Shared by the search, agent, and display suites, each supplying its own handler.

    In a `main()`-driven test, CALL THIS AFTER `scripted_model(...)`, never before: it replaces
    the process-global `httpx.AsyncClient`, and `openai`'s constructor rejects anything that is
    not an instance of whatever that name was bound to at build time — including
    `langchain_openai`'s wrapper, which subclasses the ORIGINAL class. Building the model first
    means that check has already run.
    """
    real = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("harness.tools.search.httpx.AsyncClient", factory)


def _dispatch_call(
    label: str,
    call_id: str = "call_dispatch",
    *,
    objective: str = "",
    output_format: str = "a short cited report",
    boundaries: str = "nothing else",
) -> AIMessage:
    """One `dispatch_researcher` tool call — the lead's way of starting a researcher (D1).

    Shared, not per-file: the lead lost `task` in PLAN-interactive-lead-chat Phase 1, so every
    suite that scripts a lead turn (`test_session`, `test_agent`, `test_display`,
    `test_ask_user`, `test_delegation_e2e`) needs this exact shape — it is the lead-tier
    replacement for `test_agent.py`'s `_task_call`, which now only ever scripts the
    researcher's own dispatch to a reader. The tool name is written literally for the same
    reason `_task_call` writes `"task"`: it is the model-facing wire name.
    """
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "dispatch_researcher",
                "args": {
                    "label": label,
                    "objective": objective or f"Investigate {label}",
                    "output_format": output_format,
                    "boundaries": boundaries,
                },
                "id": call_id,
            }
        ],
    )


def _submit_call(answer: str, call_id: str = "call_submit") -> AIMessage:
    """One `submit_report` tool call — the ONLY way a lead ends research with a report (D3)."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "submit_report", "args": {"answer": answer}, "id": call_id}],
    )


def verify_reply(
    verdict: str,
    detail: str,
    *,
    sources_conflict: bool = False,
    unsupported_items: list[int] | None = None,
) -> AIMessage:
    """A model reply in the pooled-paragraph JSON envelope `harness/verify.py` parses.

    Shared so an end-to-end `main()` test scripts the verification pass with the same envelope
    `tests/test_verify.py` uses, rather than hand-rolling a third copy.
    """
    return AIMessage(
        content=json.dumps(
            {
                "verdict": verdict,
                "detail": detail,
                "sources_conflict": sources_conflict,
                "unsupported_items": unsupported_items or [],
            }
        )
    )


def drain_stdout(capsys: pytest.CaptureFixture[str]) -> tuple[str, list[str]]:
    """Return stdout and its non-empty lines. `readouterr` drains, so call this once."""
    out = capsys.readouterr().out
    return out, [line for line in out.splitlines() if line.strip()]


def _challenge_fixtures() -> list[Path]:
    """Every `challenge_*.txt` fixture, sorted — shared by test_blocklist.py and test_fetch.py."""
    return sorted(CHALLENGE_FIXTURES_DIR.glob("challenge_*.txt"))


def read_blocklist_file(path: Path) -> dict:
    """Read a blocklist JSON file back, the read side of `_seed_blocklist_file`.

    Shared for the same reason the seed helper is: test_fetch.py had its own private copy and
    test_blocklist.py pasted the same one-liner inline, so a schema change to the file meant
    editing assertions in two places that never referenced each other.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_blocklist_file(path: Path, hostname: str, reason: str = "403") -> None:
    """Pre-write a blocklist JSON file directly, bypassing `Blocklist.add`.

    Shared by test_fetch.py and test_search.py, which were byte-identical copies of this.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({hostname: {"reason": reason, "first_seen": "2026-01-01T00:00:00+00:00"}}),
        encoding="utf-8",
    )


def approve_all(registry: Any, urls: list[str]) -> None:
    """Approve every URL in `urls` on `registry` (Phase 4 strict provenance, R2).

    Shared arrange step for fetch/fallback tests that exercise URLs never seen by
    `search_web` -- without this, `_fetch`'s pre-crawl provenance check rejects them.
    """
    for url in urls:
        registry.approve(url)


def write_source_capture(
    config: HarnessConfig,
    registry: Any,
    source_id: str,
    body: str = "Some captured body text.",
) -> None:
    """Write a real, `fetched`-shaped capture under `registry`'s run directory.

    The one home for the captured-file shape that `harness/report.py` (is this usable evidence?)
    and `harness/verify.py` (can it settle a claim?) both read. Takes `registry` so the file
    lands under this run's directory rather than a flat `sources/`.
    """
    captures_dir = sources_dir(config, registry)
    captures_dir.mkdir(parents=True, exist_ok=True)
    (captures_dir / f"{source_id}.md").write_text(
        f"# {source_id}: captured page\n\n- Outcome: fetched\n\n{body}", encoding="utf-8"
    )


def write_workspace_note(
    config: HarnessConfig, registry: Any, relative_path: str, text: str
) -> Path:
    """Write an agent working note into `registry`'s run workspace; return its path.

    The one home for "where a run's notes live", as `write_source_capture` is for captures. Every
    run owns a subdirectory, so a note written to the shared workspace root belongs to no run and
    no report sees it — which is what keeps two concurrent runs apart.
    """
    path = run_workspace_dir(config, registry.run_id) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def scripted_model():
    """Factory for a `ScriptedChatModel` bound to a throwaway (never-dialed) endpoint."""

    def _make(responses: list[AIMessage]) -> ScriptedChatModel:
        model = ScriptedChatModel(
            model="test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
        )
        return model.script(responses)

    return _make


@pytest.fixture
def make_agent_settings(tmp_path):
    """Factory for `AgentSettings` with the synthesis margin DISABLED and the two output dirs
    scoped to `tmp_path`.

    Two policies in one place. The margin default (240s) exceeds the tiny `wall_clock_seconds`
    a wall-clock test needs and fails `AgentSettings`' cross-field validator, so every such
    test pinned `synthesis_margin_seconds=0` by hand — a comment-and-a-half pasted seven times
    across three files. And a bare `AgentSettings(...)` reverts `workspace_dir`/`reports_dir`
    to their HOME-relative defaults, which `build_fetch_tool` eagerly `mkdir`s, leaking run
    directories into the developer's real `~/deep-research/`.

    `**overrides` passes any other field straight through; pass `synthesis_margin_seconds`
    explicitly when the margin itself is what a test exercises.
    """

    def _make(**overrides: object) -> AgentSettings:
        fields: dict[str, object] = {
            "synthesis_margin_seconds": 0,
            "workspace_dir": tmp_path / "workspace",
            "reports_dir": tmp_path / "reports",
        }
        fields.update(overrides)
        return AgentSettings(**fields)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def make_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Return a factory building a valid HarnessConfig from pydantic models (no TOML).

    Defaults `agent.workspace_dir`/`reports_dir` and `blocklist.path` under pytest's
    `tmp_path`, because `AgentSettings()`'s and `BlocklistSettings()`'s own defaults are
    HOME-relative and would write into the developer's real `~/deep-research/`. A
    caller-supplied `agent=`/`blocklist=` wins untouched.

    `head_model`/`researcher_model`/`reader_model`/`verifier_model` default to the same string;
    pass distinct values when a test must prove the roles are read from different places.
    """

    def _make(
        *,
        page_timeout_ms: int = 15000,
        max_concurrency: int = 5,
        per_page_char_cap: int = 12000,
        max_urls_per_call: int = 5,
        min_markdown_words: int = 50,
        base_url: str = "http://searx.test",
        default_max_results: int = 10,
        max_consecutive_failures: int = 3,
        agent: AgentSettings | None = None,
        guard: GuardSettings | None = None,
        blocklist: BlocklistSettings | None = None,
        head_model: str = "test-model",
        researcher_model: str = "test-model",
        reader_model: str = "test-model",
        verifier_model: str = "test-model",
    ) -> HarnessConfig:
        monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
        if agent is None:
            agent = AgentSettings(
                workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
            )
        if blocklist is None:
            blocklist = BlocklistSettings(path=tmp_path / "blocked-domains.json")
        return HarnessConfig(
            providers={
                "opencode": ProviderConfig(
                    base_url="https://example.test/v1", api_key_env="OPENCODE_API_KEY"
                )
            },
            roles={
                "head": RoleConfig(provider="opencode", model=head_model),
                "researcher": RoleConfig(provider="opencode", model=researcher_model),
                "reader": RoleConfig(provider="opencode", model=reader_model),
                "verifier": RoleConfig(provider="opencode", model=verifier_model),
            },
            fetch=FetchSettings(
                page_timeout_ms=page_timeout_ms,
                max_concurrency=max_concurrency,
                per_page_char_cap=per_page_char_cap,
                max_urls_per_call=max_urls_per_call,
                min_markdown_words=min_markdown_words,
            ),
            search=SearchSettings(
                base_url=base_url,
                default_max_results=default_max_results,
                max_consecutive_failures=max_consecutive_failures,
            ),
            agent=agent,
            guard=guard or GuardSettings(),
            blocklist=blocklist,
        )

    return _make
