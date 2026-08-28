"""Build LangChain chat model clients for the harness's configured roles.

Each role (`head`, `researcher`, `reader`, `verifier`) resolves through `harness.toml`'s
`[roles]` and `[providers]` tables to a concrete `ChatOpenAI` client. Retry is the OpenAI SDK's own
bounded exponential backoff with jitter (via `max_retries`); callers must not wrap the
returned client in another retry layer.
"""

import openai
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from harness.config import HarnessConfig


class ModelError(Exception):
    """Raised when a role cannot be resolved to a usable model client."""


def build_chat_model(config: HarnessConfig, role: str) -> BaseChatModel:
    """Resolve `role` through `config` and return a client with retry already applied.

    Raises `ModelError` naming the role and its provider when the role is undeclared,
    the provider is undeclared, the role's model or the provider's `base_url` is the
    literal placeholder `"TODO"`, or the provider's API key is absent.
    """
    role_config = config.roles.get(role)
    if role_config is None:
        raise ModelError(f"role {role!r} is not declared in [roles]")

    provider_config = config.providers.get(role_config.provider)
    if provider_config is None:
        raise ModelError(f"role {role!r} references undeclared provider {role_config.provider!r}")

    if role_config.model == "TODO":
        raise ModelError(
            f"role {role!r} (provider {role_config.provider!r}) has model={role_config.model!r} "
            "— fill in the TODO placeholder in harness.toml"
        )

    if provider_config.base_url == "TODO":
        raise ModelError(
            f"role {role!r} (provider {role_config.provider!r}) has "
            f"base_url={provider_config.base_url!r} — fill in the TODO placeholder in "
            "harness.toml"
        )

    if not provider_config.api_key:
        raise ModelError(
            f"role {role!r} (provider {role_config.provider!r}) has no API key resolved "
            f"from {provider_config.api_key_env!r} — set that environment variable"
        )

    # D6: the role's own timeout when it sets one, else the `[agent]` default.
    timeout = (
        role_config.request_timeout_seconds
        if role_config.request_timeout_seconds is not None
        else config.agent.request_timeout_seconds
    )

    return ChatOpenAI(
        model=role_config.model,
        base_url=provider_config.base_url,
        api_key=SecretStr(provider_config.api_key),
        max_retries=config.agent.max_retries,
        timeout=timeout,
    )


async def preflight(config: HarnessConfig, role: str) -> None:
    """Verify `role` resolves to a chat client that can actually reach the endpoint.

    R6's "before any research starts" check: `build_chat_model` catches config-shape problems,
    but a wrong or dead `base_url` only surfaces as a raw `openai`/`httpx` exception mid-run
    unless something makes one real call first. `__main__` calls this before the research loop.

    Builds its client via `build_chat_model` — no duplicated validation, no extra retry layer —
    and makes one chat call capped at a single completion token. Returns `None` on success.

    Raises `ModelError` naming the role, provider, `base_url` and model, classifying the
    underlying `openai` exception so the operator knows what to fix (endpoint unreachable,
    credentials rejected, model unknown) with the original text appended. No `openai` or `httpx`
    exception escapes.
    """
    client = build_chat_model(config, role)
    role_config = config.roles[role]
    provider_config = config.providers[role_config.provider]
    label = (
        f"role {role!r} (provider {role_config.provider!r}, "
        f"base_url={provider_config.base_url!r}, model={role_config.model!r})"
    )

    try:
        await client.ainvoke("ping", max_tokens=1)
    except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
        raise ModelError(f"{label}: credentials rejected by the endpoint — {exc}") from exc
    except openai.NotFoundError as exc:
        raise ModelError(
            f"{label}: model {role_config.model!r} is unknown or rejected by the endpoint — {exc}"
        ) from exc
    except openai.APIConnectionError as exc:
        raise ModelError(
            f"{label}: endpoint unreachable (connection failed — check base_url, DNS, and "
            f"network reachability) — {exc}"
        ) from exc
    except openai.OpenAIError as exc:
        raise ModelError(f"{label}: preflight chat call failed — {exc}") from exc
