"""Behavioral tests for harness.config."""

from pathlib import Path

import pytest

from harness.config import ConfigError, load_config

VALID_TOML = """
[providers.opencode]
base_url = "https://opencode.example/v1"
api_key_env = "OPENCODE_API_KEY"

[providers.cerebras]
base_url = "https://api.cerebras.ai/v1"
api_key_env = "CEREBRAS_API_KEY"

[roles.head]
provider = "opencode"
model = "glm-5.2"

[roles.subagent]
provider = "cerebras"
model = "gemma-4-31b"

[browser]
backend = "playwright"

[fetch]
page_timeout_ms = 20000
max_concurrency = 8
per_page_char_cap = 9000

[search]
base_url = "http://localhost:8080"
default_max_results = 7
"""

MINIMAL_TOML = """
[providers.opencode]
base_url = "https://opencode.example/v1"
api_key_env = "OPENCODE_API_KEY"

[providers.cerebras]
base_url = "https://api.cerebras.ai/v1"
api_key_env = "CEREBRAS_API_KEY"

[roles.head]
provider = "opencode"
model = "glm-5.2"

[roles.subagent]
provider = "cerebras"
model = "gemma-4-31b"

[browser]
backend = "playwright"

[search]
base_url = "http://localhost:8080"
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "harness.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_toml_loads_full_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, VALID_TOML)

    config = load_config(path)

    assert set(config.providers) == {"opencode", "cerebras"}
    assert config.providers["opencode"].base_url == "https://opencode.example/v1"
    assert config.providers["opencode"].api_key == "opencode-secret"
    assert config.providers["cerebras"].base_url == "https://api.cerebras.ai/v1"
    assert config.providers["cerebras"].api_key == "cerebras-secret"

    assert config.roles["head"].provider == "opencode"
    assert config.roles["head"].model == "glm-5.2"
    assert config.roles["subagent"].provider == "cerebras"
    assert config.roles["subagent"].model == "gemma-4-31b"

    assert config.browser.backend == "playwright"

    assert config.fetch.page_timeout_ms == 20000
    assert config.fetch.max_concurrency == 8
    assert config.fetch.per_page_char_cap == 9000

    assert config.search.base_url == "http://localhost:8080"
    assert config.search.default_max_results == 7


def test_omitted_limits_fall_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, MINIMAL_TOML)

    config = load_config(path)

    assert config.fetch.page_timeout_ms == 15000
    assert config.fetch.max_concurrency == 5
    assert config.fetch.per_page_char_cap == 12000
    assert config.search.default_max_results == 10


def test_missing_env_var_raises_config_error_naming_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    path = _write(tmp_path, VALID_TOML)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "OPENCODE_API_KEY" in str(excinfo.value)


def test_role_referencing_undeclared_provider_names_role_and_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        '[roles.subagent]\nprovider = "cerebras"',
        '[roles.subagent]\nprovider = "nonexistent"',
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "subagent" in message
    assert "nonexistent" in message


def test_unknown_browser_backend_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace('backend = "playwright"', 'backend = "chromium-supreme"')
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError):
        load_config(path)


def test_lightpanda_backend_without_cdp_url_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace('backend = "playwright"', 'backend = "lightpanda"')
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "lightpanda" in message
    assert "cdp_url" in message


def test_malformed_toml_raises_config_error_not_tomldecodeerror(tmp_path):
    path = _write(tmp_path, "this is [ not valid toml =")

    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_toml_file_raises_config_error_not_oserror(tmp_path):
    path = tmp_path / "does-not-exist.toml"

    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_section_error_names_the_offending_field(tmp_path, monkeypatch):
    """R7: a missing setting must be identifiable from the message alone.

    Pydantic's bare `msg` is "Field required" with no field name; the loc path is what
    makes it actionable.
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        '[search]\nbase_url = "http://localhost:8080"\ndefault_max_results = 7\n', ""
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "search" in str(excinfo.value)


def test_typo_in_key_error_names_the_offending_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace("page_timeout_ms = 20000", "page_timout_ms = 20000")
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "page_timout_ms" in str(excinfo.value)


@pytest.mark.parametrize(
    ("setting", "bad_value"),
    [
        ("page_timeout_ms", 0),
        ("max_concurrency", -1),
        ("per_page_char_cap", 0),
    ],
)
def test_non_positive_limits_are_rejected(tmp_path, monkeypatch, setting, bad_value):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    original = {"page_timeout_ms": 20000, "max_concurrency": 8, "per_page_char_cap": 9000}
    toml_content = VALID_TOML.replace(
        f"{setting} = {original[setting]}", f"{setting} = {bad_value}"
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert setting in str(excinfo.value)


def test_literal_api_key_in_the_file_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        'api_key_env = "OPENCODE_API_KEY"',
        'api_key_env = "OPENCODE_API_KEY"\napi_key = "sk-live-oops"',
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "api_key" in message
    assert "OPENCODE_API_KEY" in message


@pytest.mark.parametrize(
    ("section", "blocks"),
    [
        (
            "providers",
            [
                '[providers.opencode]\nbase_url = "https://opencode.example/v1"\n'
                'api_key_env = "OPENCODE_API_KEY"\n\n',
                '[providers.cerebras]\nbase_url = "https://api.cerebras.ai/v1"\n'
                'api_key_env = "CEREBRAS_API_KEY"\n\n',
            ],
        ),
        (
            "roles",
            [
                '[roles.head]\nprovider = "opencode"\nmodel = "glm-5.2"\n\n',
                '[roles.subagent]\nprovider = "cerebras"\nmodel = "gemma-4-31b"\n\n',
            ],
        ),
        ("browser", ['[browser]\nbackend = "playwright"\n\n']),
    ],
)
def test_missing_top_level_table_raises_config_error_naming_it(
    tmp_path, monkeypatch, section, blocks
):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML
    for block in blocks:
        assert block in toml_content, "fixture drifted from VALID_TOML — update the block"
        toml_content = toml_content.replace(block, "")
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert section in str(excinfo.value)


def test_shipped_harness_toml_loads_with_no_todo_placeholders_left(monkeypatch):
    """The gap this test used to keep visible is now closed. Literal "TODO" values are
    well-formed strings that `load_config` still accepts, so nothing but this test stops
    one from being reintroduced into the checked-in config; `build_chat_model` rejects a
    `TODO` it is handed at runtime (see tests/test_models.py), and this guards the file
    itself.

    Checks shape, not the specific endpoint/model in use: pinning those exact deployment
    facts would fail this suite on a legitimate endpoint or model swap, working against the
    config-swappable invariant (CLAUDE.md -> Invariants).
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "any")

    config = load_config()

    assert config.providers["opencode"].base_url.startswith("https://")
    assert config.roles["head"].model != ""
    offenders = [
        f"providers.{name}.base_url" for name, p in config.providers.items() if p.base_url == "TODO"
    ] + [f"roles.{name}.model" for name, r in config.roles.items() if r.model == "TODO"]
    assert offenders == []


def test_missing_head_role_raises_config_error_naming_head(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        '[roles.head]\nprovider = "opencode"\nmodel = "glm-5.2"\n\n', ""
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "head" in str(excinfo.value)


def test_missing_subagent_role_raises_config_error_naming_subagent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        '[roles.subagent]\nprovider = "cerebras"\nmodel = "gemma-4-31b"\n\n', ""
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "subagent" in str(excinfo.value)


def test_agent_section_loads_declared_values(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = (
        VALID_TOML
        + """
[agent]
max_rounds = 12
wall_clock_seconds = 600
workspace_dir = "custom-workspace"
reports_dir = "custom-reports"
max_retries = 4
request_timeout_seconds = 30.0
"""
    )
    path = _write(tmp_path, toml_content)

    config = load_config(path)

    assert config.agent.max_rounds == 12
    assert config.agent.wall_clock_seconds == 600
    assert config.agent.workspace_dir == Path("custom-workspace")
    assert config.agent.reports_dir == Path("custom-reports")
    assert config.agent.max_retries == 4
    assert config.agent.request_timeout_seconds == 30.0


def test_agent_section_omitted_falls_back_to_documented_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, VALID_TOML)

    config = load_config(path)

    assert config.agent.max_rounds == 20
    assert config.agent.wall_clock_seconds == 1800
    assert config.agent.workspace_dir == Path("workspace")
    assert config.agent.reports_dir == Path("reports")
    assert config.agent.max_retries == 2
    assert config.agent.request_timeout_seconds == 120.0


def test_agent_section_rejects_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = (
        VALID_TOML
        + """
[agent]
max_rounds = 12
typo_key = "oops"
"""
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "typo_key" in str(excinfo.value)
