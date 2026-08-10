"""Shared test fixtures for the harness suite."""

from collections.abc import Callable, Sequence
from typing import Any

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
    BrowserSettings,
    FetchSettings,
    HarnessConfig,
    ProviderConfig,
    RoleConfig,
    SearchSettings,
)


class ScriptedChatModel(ChatOpenAI):
    """A `ChatOpenAI` subclass that plays back a scripted list of `AIMessage` replies.

    Subclasses `ChatOpenAI` — rather than `GenericFakeChatModel` — because `harness.agent`
    derives its deepagents `HarnessProfile` registry key from `get_model_provider`/
    `get_model_identifier` (Phase 3 plan, settled finding 4), which read `model_name` and
    `_get_ls_params()`. `GenericFakeChatModel` supplies neither, so it would silently fail
    profile resolution in a way that has nothing to do with the code under test.
    `_generate`/`_agenerate` are overridden so no network call is ever made; every other
    `ChatOpenAI` behavior — `bind_tools`'s schema conversion, `ls_provider="openai"` — is
    real, matching exactly what `build_agent` sees in production. Every call's `messages`
    argument is recorded on `_received_messages`, oldest call first, so a test can assert
    on what actually reached the model (e.g. that the rendered system prompt arrived, or
    that a later call's messages are the post-compression, truncated set).
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

        This is what lets a test assert on the schema the model was actually offered
        (e.g. that `execute` never appears in it) without inspecting deepagents internals.
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
        # `_generate` already records `messages` and is called synchronously below — no
        # separate recording needed here to cover the async path.
        return self._generate(messages, stop=stop, **kwargs)


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

    Defaults `agent.workspace_dir`/`reports_dir` to subdirectories of pytest's `tmp_path`
    rather than `AgentSettings()`'s repo-root-relative defaults, so a full test run never
    leaves `workspace/`/`reports/` behind in the repo. A caller-supplied `agent=` wins
    untouched.
    """

    def _make(
        *,
        page_timeout_ms: int = 15000,
        max_concurrency: int = 5,
        per_page_char_cap: int = 12000,
        max_urls_per_call: int = 4,
        base_url: str = "http://searx.test",
        default_max_results: int = 10,
        agent: AgentSettings | None = None,
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
                "head": RoleConfig(provider="opencode", model="test-model"),
                "subagent": RoleConfig(provider="opencode", model="test-model"),
            },
            browser=BrowserSettings(backend="playwright", cdp_url=None),
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
