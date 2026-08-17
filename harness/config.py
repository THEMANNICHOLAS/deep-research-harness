"""Load and validate the harness's TOML config surface.

Providers, model roles, and fetch/search limits are declared in `harness.toml` at the
repo root. Secrets are never stored in the file — each provider names an environment
variable, resolved at load time.
"""

import os
import tomllib
from pathlib import Path

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


class FetchSettings(_StrictModel):
    # Bounded, not merely typed: these cross the config trust boundary into crawl4ai's
    # HTTP strategy and the per-page truncation cap, where 0 or a negative is nonsense.
    page_timeout_ms: int = Field(default=15000, gt=0)
    http_concurrency: int = Field(default=10, gt=0)
    # ~3s: crawl4ai's HTTP strategy hardcodes a 10s connect timeout, so this deadline is
    # enforced by our own caller-side asyncio.wait_for, not by crawl4ai itself.
    http_deadline_ms: int = Field(default=3000, gt=0)
    # gt=0, not ge=0: the contract forbids disabling retries outright (2 extra attempts
    # after the first is the plan's floor, not an oversight to relax).
    max_retries: int = Field(default=2, gt=0)
    per_page_char_cap: int = Field(default=12000, gt=0)
    # 5 is engineering judgment, not a measured optimum (D1): it bounds one call to ~15k
    # tokens at the current per-page cap. Operators change it here, not in code.
    max_urls_per_call: int = Field(default=5, gt=0)
    # 50 is a starting guess (risk !#5), not a measured threshold: check the escalation
    # rate on a real run before treating it as settled — too high sends ordinary short
    # pages to Chromium needlessly, too low lets real JS shells through as "fetched".
    min_markdown_words: int = Field(default=50, gt=0)
    browser_deadline_ms: int = Field(default=20000, gt=0)
    # 2 is deliberately low (risk !#2): dropping arun_many also dropped crawl4ai's
    # MemoryAdaptiveDispatcher backpressure, so this is now the only thing bounding browser
    # memory use. Raising it requires a memory measurement on the box, not just an edit here.
    browser_concurrency: int = Field(default=2, gt=0)
    # A plain relative path, not resolved against anything: no workspace/reports root key
    # exists in this config yet (see docs/plans/PLAN-http-first-fetch.md Phase 3 notes). This
    # IS the containment mechanism for now — a real workspace root, once one exists, should
    # absorb it. Passed to crawl4ai as `downloads_path` so nothing writes to
    # ~/.crawl4ai/downloads. No `gt=0` here: it is a path, not a bound.
    downloads_dir: str = Field(default="workspace/downloads")
    # Path convention matches downloads_dir above (no workspace/reports root exists yet).
    blocklist_path: str = Field(default="workspace/blocklist.json")
    # Risk !#4: a transient 403 (aggressive WAF, a rate-limit answered as 403) locks the
    # whole domain out for the full TTL. The file is hand-editable JSON, so an operator can
    # delete a wrongly-blocked entry directly; if false positives show up in practice the
    # cheap fix is requiring two strikes before recording, not shortening this default.
    blocklist_ttl_days: int = Field(default=30, gt=0)


class SearchSettings(_StrictModel):
    base_url: str
    default_max_results: int = Field(default=10, gt=0)


class HarnessConfig(_StrictModel):
    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    search: SearchSettings
    # D6/R6: a declared contract, not runtime enforcement — no agent loop exists yet to
    # honor it, so nothing here schedules or counts subagents. It multiplies with
    # `fetch.http_concurrency` to set the worst-case fetch load; the arithmetic lives in
    # docs/architecture.md `## Concurrency Bounds` rather than being restated here.
    max_subagents: int = Field(default=3, gt=0)

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
