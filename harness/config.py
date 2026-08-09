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


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key_env: str
    api_key: str = ""

    @model_validator(mode="after")
    def _resolve_api_key(self) -> "ProviderConfig":
        value = os.environ.get(self.api_key_env, "")
        if not value:
            raise ValueError(
                f"environment variable {self.api_key_env!r} is not set (required by api_key_env)"
            )
        self.api_key = value
        return self


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str


class BrowserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["lightpanda", "playwright"]
    cdp_url: str | None = None

    @model_validator(mode="after")
    def _require_cdp_url_for_lightpanda(self) -> "BrowserSettings":
        if self.backend == "lightpanda" and not self.cdp_url:
            raise ValueError("browser.backend is 'lightpanda' but browser.cdp_url is not set")
        return self


class FetchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bounded, not merely typed: these cross the config trust boundary into crawl4ai's
    # dispatcher and the per-page truncation cap, where 0 or a negative is nonsense.
    page_timeout_ms: int = Field(default=15000, gt=0)
    max_concurrency: int = Field(default=5, gt=0)
    per_page_char_cap: int = Field(default=12000, gt=0)


class SearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    default_max_results: int = Field(default=10, gt=0)


class HarnessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]
    browser: BrowserSettings
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    search: SearchSettings

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
