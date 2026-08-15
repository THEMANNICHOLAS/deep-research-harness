"""Load and validate `harness.toml`: providers, model roles, fetch/search limits.

Secrets are never stored in the file — each provider names an environment variable,
resolved at load time.
"""

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(Exception):
    """Raised for any failure loading or validating the harness config."""


def _load_dotenv(path: Path) -> None:
    """Populate `os.environ` from a `.env` file next to `harness.toml`, if present.

    A real environment variable always wins over `.env` — this only fills gaps, matching
    standard dotenv precedence. Hand-rolled rather than a `python-dotenv` dependency: the file
    is just `KEY=VALUE` lines, comments, and blanks (see `.env.example`).
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


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
        # Raw input only; revalidating a built instance passes through. A literal key would sit
        # in version control while being silently ignored in favor of the env var.
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
    # Bounded, not merely typed: these reach crawl4ai's dispatcher and the truncation cap,
    # where 0 or a negative is nonsense.
    page_timeout_ms: int = Field(default=15000, gt=0)
    max_concurrency: int = Field(default=5, gt=0)
    per_page_char_cap: int = Field(default=12000, gt=0)
    # Judgment, not a measured optimum (D1): bounds one call to ~15k tokens at the current
    # per-page cap. Bounds one `fetch_pages` call, never the run (R9/D11).
    max_urls_per_call: int = Field(default=5, gt=0)


class SearchSettings(_StrictModel):
    base_url: str
    default_max_results: int = Field(default=10, gt=0)
    # R2/D3: consecutive connection-level search failures that abort the run.
    max_consecutive_failures: int = Field(default=3, gt=0)


class AgentSettings(_StrictModel):
    max_rounds: int = Field(default=50, gt=0)  # hard cap on agent-loop rounds
    wall_clock_seconds: int = Field(default=1800, gt=0)  # wall-clock budget, in seconds
    # Under the user's home dir, not the repo root; overridable per-key from [agent].
    workspace_dir: Path = Field(
        default_factory=lambda: Path.home() / "deep-research" / "workspace"
    )  # scratch dir the loop may write to
    reports_dir: Path = Field(
        default_factory=lambda: Path.home() / "deep-research" / "reports"
    )  # where finished reports land
    # Retries AFTER the initial attempt, mapping 1:1 onto the OpenAI SDK's `max_retries`, which
    # already applies bounded exponential backoff with jitter — there is no separate knob.
    max_retries: int = Field(default=2, ge=0)
    request_timeout_seconds: float = Field(default=120.0, gt=0)  # per-request timeout, seconds


class HarnessConfig(_StrictModel):
    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]
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

    _load_dotenv(path.parent / ".env")

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

    Pydantic's `msg` alone reads "Field required" with no clue which field, which R7's "fails at
    startup with a clear message" needs; `loc` carries the path.
    """
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(segment) for segment in error["loc"])
        message = str(error["msg"])
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts)


def run_workspace_dir(config: HarnessConfig, run_id: str) -> Path:
    """The one place the per-run workspace root `<workspace_dir>/<run_id>` is built.

    Everything a run writes lives under it: working notes, captured sources, evicted history.
    `workspace_dir` itself is fixed and nothing ever clears it, so without the per-run level two
    runs in flight wrote notes into one tree and each rendered the other's as its own findings —
    the overstatement R3 forbids, in the report a reader can least check.

    Lives here rather than beside a consumer because the three consumers are peers: `agent.py`
    roots the backend at it, `tools/fetch.py` hangs `sources/` off it, `report.py` scans it for
    notes. Takes the bare `run_id`, not a `SourceRegistry`, so config stays free of that import.
    """
    return config.agent.workspace_dir / run_id
