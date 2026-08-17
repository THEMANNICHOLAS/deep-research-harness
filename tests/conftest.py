"""Shared test fixtures for the harness suite."""

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
    FetchSettings,
    GuardSettings,
    HarnessConfig,
    ProviderConfig,
    RoleConfig,
    SearchSettings,
    run_workspace_dir,
)
from harness.sources import sources_dir


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


def _make_fake_crawler_class(
    results: list[_FakeResult], pdf_results: list[_FakeResult] | None = None
) -> type:
    """Build a fake AsyncWebCrawler class recording construction and `arun_many` calls.

    One fake class serves both the Playwright batch and the PDF batch — `_fetch` gets both
    from the same `_crawler_class()` seam, distinguished only by the `crawler_strategy` kwarg
    passed at construction. `pdf_results` (defaulting to `results`, so existing single-arg
    callers are unaffected) lets a test give the PDF batch its own canned results distinct
    from the Playwright batch's.
    """

    class _FakeCrawler:
        constructed_with: list[object] = []
        calls: list[SimpleNamespace] = []

        def __init__(self, config: object = None, crawler_strategy: object = None) -> None:
            _FakeCrawler.constructed_with.append(config)
            self._is_pdf = crawler_strategy is not None

        async def __aenter__(self) -> "_FakeCrawler":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def arun_many(
            self, urls: list[str], config: object = None, dispatcher: object = None
        ) -> list[_FakeResult]:
            _FakeCrawler.calls.append(
                SimpleNamespace(
                    urls=urls, config=config, dispatcher=dispatcher, is_pdf=self._is_pdf
                )
            )
            if self._is_pdf:
                return pdf_results if pdf_results is not None else results
            return results

    return _FakeCrawler


@pytest.fixture
def install_crawler(monkeypatch):
    """Patch `harness.tools.fetch._crawler_class`/`_pdf_crawler_parts` with fakes.

    `_crawler_class` (not a module-level `AsyncWebCrawler` name) because fetch.py imports
    crawl4ai lazily inside `_fetch` — the function is the deliberate patch seam. Patches
    fetch.py's namespace regardless of caller: `fallback.py` reuses fetch.py's `_fetch`,
    and that is where the crawler is actually constructed, so both fetch and fallback
    tests share this one fixture. `_pdf_crawler_parts` is the parallel seam for the PDF
    strategy classes fetch.py's PDF batch constructs.
    """

    def _install(results: list[_FakeResult], pdf_results: list[_FakeResult] | None = None) -> type:
        fake_cls = _make_fake_crawler_class(results, pdf_results)
        monkeypatch.setattr("harness.tools.fetch._crawler_class", lambda: fake_cls)
        monkeypatch.setattr(
            "harness.tools.fetch._pdf_crawler_parts",
            lambda: (_FakePDFCrawlerStrategy, _FakePDFContentScrapingStrategy),
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


def patch_model(monkeypatch: pytest.MonkeyPatch, model: Any) -> None:
    """Point `build_chat_model` at `model` for every caller.

    One target: `harness.agent` and `harness.verify` call it as a module attribute
    (`models.build_chat_model(...)`, resolved at call time) rather than importing it by
    value, so patching the definition covers them all — including any future caller,
    which a hand-maintained target list silently missed.
    """
    monkeypatch.setattr("harness.models.build_chat_model", lambda cfg, role: model)


@pytest.fixture
def patch_models_by_role(monkeypatch: pytest.MonkeyPatch):
    """Like `patch_model`, but returns a different model per role (e.g. head vs reader)."""

    def _patch(models: dict[str, Any]) -> None:
        def _by_role(cfg: Any, role: str) -> Any:
            return models[role]

        monkeypatch.setattr("harness.models.build_chat_model", _by_role)

    return _patch


def patch_run(
    monkeypatch: pytest.MonkeyPatch,
    config: HarnessConfig,
    model: Any,
    *,
    skip_preflight: bool = True,
    run_search_preflight: bool = False,
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
    patch_model(monkeypatch, model)


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
def make_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Return a factory building a valid HarnessConfig from pydantic models (no TOML).

    Defaults `agent.workspace_dir`/`reports_dir` under pytest's `tmp_path`, because
    `AgentSettings()`'s own defaults are HOME-relative and would write into the developer's real
    output directory. A caller-supplied `agent=` wins untouched.

    `head_model`/`researcher_model`/`reader_model`/`verifier_model` default to the same string;
    pass distinct values when a test must prove the roles are read from different places.
    """

    def _make(
        *,
        page_timeout_ms: int = 15000,
        max_concurrency: int = 5,
        per_page_char_cap: int = 12000,
        max_urls_per_call: int = 5,
        base_url: str = "http://searx.test",
        default_max_results: int = 10,
        max_consecutive_failures: int = 3,
        agent: AgentSettings | None = None,
        guard: GuardSettings | None = None,
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
            ),
            search=SearchSettings(
                base_url=base_url,
                default_max_results=default_max_results,
                max_consecutive_failures=max_consecutive_failures,
            ),
            agent=agent,
            guard=guard or GuardSettings(),
        )

    return _make
