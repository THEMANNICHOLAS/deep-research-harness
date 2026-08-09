"""Behavioral tests for harness.models."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import openai
import pytest
from langchain_openai import ChatOpenAI

import harness.models as models
from harness.config import AgentSettings, HarnessConfig, ProviderConfig, RoleConfig
from harness.models import ModelError, build_chat_model, preflight


def _completion_body(model: str = "test-model", content: str = "hi there") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class _RecordingHandler:
    """Fails the first `fail_times` calls with 429, then succeeds; records every request."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) <= self.fail_times:
            return httpx.Response(
                429, json={"error": {"message": "rate limited", "type": "rate_limit_error"}}
            )
        return httpx.Response(200, json=_completion_body())


def _patch_chat_openai_with_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Monkeypatch harness.models.ChatOpenAI to inject a mock transport, keeping the real
    OpenAI-SDK retry path under test rather than reimplementing it."""
    real_chat_openai = models.ChatOpenAI

    def _factory(**kwargs: Any) -> ChatOpenAI:
        kwargs["http_async_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return real_chat_openai(**kwargs)

    monkeypatch.setattr(models, "ChatOpenAI", _factory)


class _RecordingResponder:
    """Records every request and delegates the response (or raised exception) to `respond`.

    Third use of the "record requests, hand back a canned outcome" shape after
    `_RecordingHandler` and the inline handler in `test_non_transient_failure_is_not_retried`
    — factored out per CLAUDE.md's Code Reuse rule.
    """

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self._respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def _raise_connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("simulated DNS/connection failure")


def _respond_with(status_code: int, body: dict) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return _handler


def test_valid_role_resolves_to_client_with_model_and_base_url(make_config):
    config = make_config()

    client = build_chat_model(config, "head")

    assert client.model_name == "test-model"
    assert client.openai_api_base == "https://example.test/v1"


def test_unknown_role_raises_model_error_naming_role(make_config):
    config = make_config()

    with pytest.raises(ModelError) as excinfo:
        build_chat_model(config, "nope")

    assert "nope" in str(excinfo.value)


def test_role_with_undeclared_provider_raises_model_error_naming_both(make_config):
    config = make_config()
    broken = HarnessConfig.model_construct(
        providers=config.providers,
        roles={
            **config.roles,
            "head": RoleConfig(provider="ghost-provider", model="test-model"),
        },
        browser=config.browser,
        fetch=config.fetch,
        search=config.search,
        agent=config.agent,
    )

    with pytest.raises(ModelError) as excinfo:
        build_chat_model(broken, "head")

    message = str(excinfo.value)
    assert "head" in message
    assert "ghost-provider" in message


def test_absent_api_key_raises_model_error_naming_role_and_provider(make_config):
    config = make_config()
    broken_provider = ProviderConfig.model_construct(
        base_url="https://example.test/v1", api_key_env="OPENCODE_API_KEY", api_key=""
    )
    # HarnessConfig(...) (unlike .model_construct) re-runs each nested ProviderConfig's
    # after-validator, which would re-resolve api_key from the (set) environment variable
    # and defeat the point of this test — so the whole config is built via model_construct,
    # matching test 3's technique.
    broken = HarnessConfig.model_construct(
        providers={**config.providers, "opencode": broken_provider},
        roles=config.roles,
        browser=config.browser,
        fetch=config.fetch,
        search=config.search,
        agent=config.agent,
    )

    with pytest.raises(ModelError) as excinfo:
        build_chat_model(broken, "head")

    message = str(excinfo.value)
    assert "head" in message
    assert "opencode" in message
    assert "OPENCODE_API_KEY" in message


def test_todo_model_raises_model_error_naming_role_and_provider(make_config):
    config = make_config()
    todo_config = HarnessConfig(
        providers=config.providers,
        roles={**config.roles, "head": RoleConfig(provider="opencode", model="TODO")},
        browser=config.browser,
        fetch=config.fetch,
        search=config.search,
        agent=config.agent,
    )

    with pytest.raises(ModelError) as excinfo:
        build_chat_model(todo_config, "head")

    message = str(excinfo.value)
    assert "head" in message
    assert "opencode" in message
    assert "model" in message
    assert "TODO" in message


def test_todo_base_url_raises_model_error_naming_role_and_provider(make_config):
    config = make_config()
    todo_config = HarnessConfig(
        providers={
            **config.providers,
            "opencode": ProviderConfig(base_url="TODO", api_key_env="OPENCODE_API_KEY"),
        },
        roles=config.roles,
        browser=config.browser,
        fetch=config.fetch,
        search=config.search,
        agent=config.agent,
    )

    with pytest.raises(ModelError) as excinfo:
        build_chat_model(todo_config, "head")

    message = str(excinfo.value)
    assert "head" in message
    assert "opencode" in message
    assert "base_url" in message
    assert "TODO" in message


async def test_transient_failure_is_retried_up_to_the_configured_bound(make_config, monkeypatch):
    config = make_config(agent=AgentSettings(max_retries=2))
    handler = _RecordingHandler(fail_times=2)
    _patch_chat_openai_with_transport(monkeypatch, handler)

    client = build_chat_model(config, "head")
    result = await client.ainvoke("hi")

    assert result.content == "hi there"
    assert len(handler.requests) == 3


async def test_retry_bound_exhausted_surfaces_the_error(make_config, monkeypatch):
    config = make_config(agent=AgentSettings(max_retries=2))
    handler = _RecordingHandler(fail_times=999)
    _patch_chat_openai_with_transport(monkeypatch, handler)

    client = build_chat_model(config, "head")

    with pytest.raises(openai.RateLimitError):
        await client.ainvoke("hi")

    assert len(handler.requests) == 3


async def test_non_transient_failure_is_not_retried(make_config, monkeypatch):
    config = make_config(agent=AgentSettings(max_retries=2))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            400, json={"error": {"message": "bad request", "type": "invalid_request_error"}}
        )

    _patch_chat_openai_with_transport(monkeypatch, handler)

    client = build_chat_model(config, "head")

    with pytest.raises(openai.BadRequestError):
        await client.ainvoke("hi")

    assert len(requests) == 1


async def test_retry_never_switches_model(make_config, monkeypatch):
    config = make_config(agent=AgentSettings(max_retries=2))
    handler = _RecordingHandler(fail_times=2)
    _patch_chat_openai_with_transport(monkeypatch, handler)

    client = build_chat_model(config, "head")
    await client.ainvoke("hi")

    models_seen = {json.loads(request.read())["model"] for request in handler.requests}
    assert models_seen == {"test-model"}


async def test_preflight_succeeds_against_a_reachable_endpoint(make_config, monkeypatch):
    config = make_config()
    handler = _RecordingResponder(_respond_with(200, _completion_body()))
    _patch_chat_openai_with_transport(monkeypatch, handler)

    result = await preflight(config, "head")

    assert result is None


async def test_preflight_raises_model_error_when_endpoint_unreachable(make_config, monkeypatch):
    config = make_config(agent=AgentSettings(max_retries=0))
    handler = _RecordingResponder(_raise_connect_error)
    _patch_chat_openai_with_transport(monkeypatch, handler)

    with pytest.raises(ModelError) as excinfo:
        await preflight(config, "head")

    message = str(excinfo.value)
    assert "head" in message
    assert "opencode" in message
    assert "https://example.test/v1" in message
    assert "test-model" in message
    assert "unreachable" in message


async def test_preflight_raises_model_error_when_credentials_rejected(make_config, monkeypatch):
    config = make_config(agent=AgentSettings(max_retries=0))
    handler = _RecordingResponder(
        _respond_with(
            401,
            {
                "error": {
                    "message": "invalid api key",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )
    )
    _patch_chat_openai_with_transport(monkeypatch, handler)

    with pytest.raises(ModelError) as excinfo:
        await preflight(config, "head")

    message = str(excinfo.value)
    assert "head" in message
    assert "opencode" in message
    assert "credentials" in message
    assert "unreachable" not in message


async def test_preflight_raises_model_error_when_model_unknown(make_config, monkeypatch):
    config = make_config(agent=AgentSettings(max_retries=0))
    handler = _RecordingResponder(
        _respond_with(
            404,
            {
                "error": {
                    "message": "model not found",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
        )
    )
    _patch_chat_openai_with_transport(monkeypatch, handler)

    with pytest.raises(ModelError) as excinfo:
        await preflight(config, "head")

    message = str(excinfo.value)
    assert "test-model" in message


async def test_preflight_retries_a_transient_failure_before_failing(make_config, monkeypatch):
    config = make_config(agent=AgentSettings(max_retries=2))
    handler = _RecordingHandler(fail_times=1)
    _patch_chat_openai_with_transport(monkeypatch, handler)

    result = await preflight(config, "head")

    assert result is None
    assert len(handler.requests) == 2


async def test_preflight_request_is_capped_to_one_token(make_config, monkeypatch):
    config = make_config()
    handler = _RecordingResponder(_respond_with(200, _completion_body()))
    _patch_chat_openai_with_transport(monkeypatch, handler)

    await preflight(config, "head")

    assert len(handler.requests) == 1
    body = json.loads(handler.requests[0].read())
    assert body["max_completion_tokens"] == 1


@pytest.mark.parametrize(
    "handler_factory",
    [
        lambda: _RecordingResponder(_raise_connect_error),
        lambda: _RecordingResponder(_respond_with(401, {"error": {"message": "invalid api key"}})),
        lambda: _RecordingResponder(_respond_with(404, {"error": {"message": "model not found"}})),
    ],
)
async def test_preflight_does_not_leak_library_exceptions(
    make_config, monkeypatch, handler_factory
):
    config = make_config(agent=AgentSettings(max_retries=0))
    handler = handler_factory()
    _patch_chat_openai_with_transport(monkeypatch, handler)

    with pytest.raises(ModelError):
        await preflight(config, "head")
