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
    HarnessConfig,
    ProviderConfig,
    RoleConfig,
    SearchSettings,
    run_workspace_dir,
)
from harness.tools.fetch import FETCH_FAILED_PREFIX, _sources_dir


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


def _make_fake_crawler_class(results: list[_FakeResult]) -> type:
    """Build a fake AsyncWebCrawler class recording construction and `arun_many` calls."""

    class _FakeCrawler:
        constructed_with: list[object] = []
        calls: list[SimpleNamespace] = []

        def __init__(self, config: object = None) -> None:
            _FakeCrawler.constructed_with.append(config)

        async def __aenter__(self) -> "_FakeCrawler":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        async def arun_many(
            self, urls: list[str], config: object = None, dispatcher: object = None
        ) -> list[_FakeResult]:
            _FakeCrawler.calls.append(
                SimpleNamespace(urls=urls, config=config, dispatcher=dispatcher)
            )
            return results

    return _FakeCrawler


@pytest.fixture
def install_crawler(monkeypatch):
    """Patch `harness.tools.fetch.AsyncWebCrawler` with a fake serving canned results.

    Patches fetch.py's namespace regardless of caller: `fallback.py` reuses fetch.py's
    `_fetch`, and that is where the crawler is actually constructed, so both fetch and
    fallback tests share this one fixture.
    """

    def _install(results: list[_FakeResult]) -> type:
        fake_cls = _make_fake_crawler_class(results)
        monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", fake_cls)
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
        response = self._script[self._call_count]
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
    """Point EVERY module-local `build_chat_model` binding at `model`.

    Three modules do `from harness.models import build_chat_model`, so each holds its own binding
    that patching the others does not touch: `harness.agent`, `harness.models`, and
    `harness.verify`. Missing one leaves a `main()`-driven test either avoiding the state that
    reaches it or dialing `https://example.test/v1` for real. One home for the list, so a fourth
    importer cannot reopen that hole silently.
    """
    for target in (
        "harness.agent.build_chat_model",
        "harness.models.build_chat_model",
        "harness.verify.build_chat_model",
    ):
        monkeypatch.setattr(target, lambda cfg, role: model)


@pytest.fixture
def patch_models_by_role(monkeypatch: pytest.MonkeyPatch):
    """Like `patch_model`, but returns a different model per role (e.g. head vs subagent)."""

    def _patch(models: dict[str, Any]) -> None:
        def _by_role(cfg: Any, role: str) -> Any:
            return models[role]

        for target in (
            "harness.agent.build_chat_model",
            "harness.models.build_chat_model",
            "harness.verify.build_chat_model",
        ):
            monkeypatch.setattr(target, _by_role)

    return _patch


def patch_run(
    monkeypatch: pytest.MonkeyPatch,
    config: HarnessConfig,
    model: Any,
    *,
    skip_preflight: bool = False,
) -> None:
    """Patch everything a `main()` test reaches outside the compiled graph.

    `skip_preflight` replaces `preflight` with a no-op. `False` by default, so most tests let the
    REAL `preflight` run against the scripted model and script a leading reply for it — that
    keeps R6's call site exercised rather than stubbed out everywhere.
    """
    import harness.__main__ as main_module

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    if skip_preflight:

        async def _noop_preflight(cfg: HarnessConfig, role: str) -> None:
            return None

        monkeypatch.setattr(main_module, "preflight", _noop_preflight)
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
    sources_dir = _sources_dir(config, registry)
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / f"{source_id}.md").write_text(
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


def write_failed_capture(
    config: HarnessConfig, registry: Any, source_id: str, outcome: str = "error"
) -> None:
    """Write a failure stub — the shape `harness/tools/fetch.py` writes for a bad fetch."""
    sources_dir = _sources_dir(config, registry)
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / f"{source_id}.md").write_text(
        f"{FETCH_FAILED_PREFIX}{outcome}\n", encoding="utf-8"
    )


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

    `head_model`/`subagent_model` default to the same string; pass distinct values when a test
    must prove the two roles are read from different places.
    """

    def _make(
        *,
        page_timeout_ms: int = 15000,
        max_concurrency: int = 5,
        per_page_char_cap: int = 12000,
        max_urls_per_call: int = 5,
        base_url: str = "http://searx.test",
        default_max_results: int = 10,
        agent: AgentSettings | None = None,
        head_model: str = "test-model",
        subagent_model: str = "test-model",
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
                "subagent": RoleConfig(provider="opencode", model=subagent_model),
            },
            fetch=FetchSettings(
                page_timeout_ms=page_timeout_ms,
                max_concurrency=max_concurrency,
                per_page_char_cap=per_page_char_cap,
                max_urls_per_call=max_urls_per_call,
            ),
            search=SearchSettings(base_url=base_url, default_max_results=default_max_results),
            agent=agent,
        )

    return _make
