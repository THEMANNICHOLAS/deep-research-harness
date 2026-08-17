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

[fetch]
page_timeout_ms = 20000
http_concurrency = 8
http_deadline_ms = 4000
max_retries = 3
per_page_char_cap = 9000
max_urls_per_call = 3

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

    assert config.fetch.page_timeout_ms == 20000
    assert config.fetch.http_concurrency == 8
    assert config.fetch.http_deadline_ms == 4000
    assert config.fetch.max_retries == 3
    assert config.fetch.per_page_char_cap == 9000
    assert config.fetch.max_urls_per_call == 3

    assert config.search.base_url == "http://localhost:8080"
    assert config.search.default_max_results == 7


def test_omitted_limits_fall_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, MINIMAL_TOML)

    config = load_config(path)

    assert config.fetch.page_timeout_ms == 15000
    assert config.fetch.http_concurrency == 10
    assert config.fetch.http_deadline_ms == 3000
    assert config.fetch.max_retries == 2
    assert config.fetch.per_page_char_cap == 12000
    assert config.fetch.max_urls_per_call == 5
    assert config.fetch.min_markdown_words == 50
    assert config.fetch.browser_deadline_ms == 20000
    assert config.fetch.browser_concurrency == 2
    assert config.fetch.downloads_dir == "workspace/downloads"
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
        ("http_concurrency", -1),
        ("http_deadline_ms", 0),
        ("max_retries", 0),
        ("per_page_char_cap", 0),
        ("max_urls_per_call", 0),
    ],
)
def test_non_positive_limits_are_rejected(tmp_path, monkeypatch, setting, bad_value):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    original = {
        "page_timeout_ms": 20000,
        "http_concurrency": 8,
        "http_deadline_ms": 4000,
        "max_retries": 3,
        "per_page_char_cap": 9000,
        "max_urls_per_call": 3,
    }
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


def test_shipped_harness_toml_loads_with_its_todo_placeholders(monkeypatch):
    """Deliberate, disclosed gap: literal "TODO" endpoint/model IDs are well-formed
    strings, so the checked-in harness.toml loads while nothing reads those values.
    Validation moves to startup when the agent loop first consumes them — see
    docs/backlog.md. This test keeps the gap visible instead of accidental.
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "any")
    monkeypatch.setenv("CEREBRAS_API_KEY", "any")

    config = load_config()

    assert config.providers["opencode"].base_url == "TODO"
    assert config.roles["head"].model == "TODO"


def test_shipped_harness_toml_has_no_browser_surface(monkeypatch):
    # Chromium is the only path; no config key selects a browser backend (R1).
    monkeypatch.setenv("OPENCODE_API_KEY", "any")
    monkeypatch.setenv("CEREBRAS_API_KEY", "any")

    config = load_config()

    assert not hasattr(config, "browser")


def test_browser_table_is_rejected_now_that_the_backend_is_gone(tmp_path, monkeypatch):
    # Proves the key is genuinely GONE rather than merely unread — a config still
    # carrying it now fails loudly instead of being silently ignored.
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    # Models a config file written before the backend was removed and never updated.
    toml_content = VALID_TOML.replace("[fetch]", '[browser]\nbackend = "playwright"\n\n[fetch]')
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "browser" in str(excinfo.value)


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
