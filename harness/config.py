"""Load and validate the harness's TOML config surface.

Providers, model roles, the browser backend, and fetch/search limits are declared in
`harness.toml` at the repo root. Secrets are never stored in the file — each provider
names an environment variable, resolved at load time.
"""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(Exception):
    """Raised for any failure loading or validating the harness config."""


class _StrictModel(BaseModel):
    """Shared strictness for every config model: an unknown key is a typo, not data."""

    model_config = ConfigDict(extra="forbid")


class ProviderConfig(_StrictModel):
    base_url: str
    api_key_env: str
    api_key: str = ""

    @model_validator(mode="before")
    @classmethod
    def _reject_literal_api_key(cls, data: object) -> object:
        # Raw input only (revalidation of a built instance passes through): a literal
        # key in the file would sit in version control while being silently ignored
        # in favor of the env var — reject it outright.
        if isinstance(data, dict) and data.get("api_key"):
            env_name = data.get("api_key_env", "api_key_env")
            raise ValueError(
                "api_key must never be set in the config file — set the environment "
                f"variable named by api_key_env ({env_name!r}) instead"
            )
        return data

    @model_validator(mode="after")
    def _resolve_api_key(self) -> "ProviderConfig":
        value = os.environ.get(self.api_key_env, "")
        if not value:
            raise ValueError(
                f"environment variable {self.api_key_env!r} is not set (required by api_key_env)"
            )
        self.api_key = value
        return self


class RoleConfig(_StrictModel):
    provider: str
    model: str


class BrowserSettings(_StrictModel):
    backend: Literal["lightpanda", "playwright"]
    cdp_url: str | None = None

    @model_validator(mode="after")
    def _require_cdp_url_for_lightpanda(self) -> "BrowserSettings":
        if self.backend == "lightpanda" and not self.cdp_url:
            raise ValueError("browser.backend is 'lightpanda' but browser.cdp_url is not set")
        return self


class FetchSettings(_StrictModel):
    # Bounded, not merely typed: these cross the config trust boundary into crawl4ai's
    # dispatcher and the per-page truncation cap, where 0 or a negative is nonsense.
    page_timeout_ms: int = Field(default=15000, gt=0)
    max_concurrency: int = Field(default=5, gt=0)
    per_page_char_cap: int = Field(default=12000, gt=0)


class SearchSettings(_StrictModel):
    base_url: str
    default_max_results: int = Field(default=10, gt=0)


class AgentSettings(_StrictModel):
    # Frozen for later phases (Phase 3's agent loop, Phase 2's workspace capture) — see
    # docs/plans/PLAN-research-loop.md Phase 1 Contracts.
    max_rounds: int = Field(default=20, gt=0)  # hard cap on agent-loop rounds
    wall_clock_seconds: int = Field(default=1800, gt=0)  # wall-clock budget, in seconds
    workspace_dir: Path = Field(default=Path("workspace"))  # scratch dir the loop may write to
    reports_dir: Path = Field(default=Path("reports"))  # where finished reports land
    # Counts retries AFTER the initial attempt — maps 1:1 onto the OpenAI SDK's
    # `max_retries`, which already applies its own bounded exponential backoff with
    # jitter; there is no separate backoff knob here.
    max_retries: int = Field(default=2, ge=0)
    request_timeout_seconds: float = Field(default=120.0, gt=0)  # per-request timeout, seconds


class HarnessConfig(_StrictModel):
    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]
    browser: BrowserSettings
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    search: SearchSettings
    agent: AgentSettings = Field(default_factory=AgentSettings)

    @model_validator(mode="after")
    def _cross_check_roles(self) -> "HarnessConfig":
        for required_role in ("head", "subagent"):
            if required_role not in self.roles:
                raise ValueError(f"required role {required_role!r} is missing")

        for role_key, role in self.roles.items():
            if role.provider not in self.providers:
                raise ValueError(
                    f"role {role_key!r} references undeclared provider {role.provider!r}"
                )
        return self


def load_config(path: Path | None = None) -> HarnessConfig:
    """Load and validate `harness.toml`, raising `ConfigError` on any failure."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / "harness.toml"

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"config file not readable: {path}: {exc}") from exc

    try:
        return HarnessConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}: {_describe(exc)}") from exc


def _describe(exc: ValidationError) -> str:
    """Render a ValidationError naming the offending field, not just the complaint.

    Pydantic's `msg` alone reads "Field required" with no clue which field, which is
    useless for R7's "fails at startup with a clear message". `loc` carries the path.
    """
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(segment) for segment in error["loc"])
        message = str(error["msg"])
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts)
