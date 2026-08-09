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
