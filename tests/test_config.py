"""Behavioral tests for harness.config."""

from pathlib import Path

import pytest

from harness.config import BlocklistSettings, ConfigError, GuardSettings, load_config

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

[roles.researcher]
provider = "cerebras"
model = "gemma-4-31b"

[fetch]
page_timeout_ms = 20000
max_concurrency = 8
per_page_char_cap = 9000
max_urls_per_call = 3
min_markdown_words = 30

[search]
base_url = "http://localhost:8080"
default_max_results = 7
max_consecutive_failures = 4
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

[roles.researcher]
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
    assert config.roles["researcher"].provider == "cerebras"
    assert config.roles["researcher"].model == "gemma-4-31b"

    assert config.fetch.page_timeout_ms == 20000
    assert config.fetch.max_concurrency == 8
    assert config.fetch.per_page_char_cap == 9000
    assert config.fetch.max_urls_per_call == 3
    assert config.fetch.min_markdown_words == 30

    assert config.search.base_url == "http://localhost:8080"
    assert config.search.default_max_results == 7
    assert config.search.max_consecutive_failures == 4


def test_omitted_limits_fall_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, MINIMAL_TOML)

    config = load_config(path)

    assert config.fetch.page_timeout_ms == 15000
    assert config.fetch.max_concurrency == 5
    assert config.fetch.per_page_char_cap == 120000
    assert config.fetch.max_urls_per_call == 5
    assert config.fetch.min_markdown_words == 50
    assert config.search.default_max_results == 10
    assert config.search.max_consecutive_failures == 3


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
        '[roles.researcher]\nprovider = "cerebras"',
        '[roles.researcher]\nprovider = "nonexistent"',
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "researcher" in message
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
    """R7: a missing setting must be identifiable from the message alone, and pydantic's bare `msg`
    is "Field required" with no field name — the `loc` path is what makes it actionable.
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        '[search]\nbase_url = "http://localhost:8080"\ndefault_max_results = 7\n'
        "max_consecutive_failures = 4\n",
        "",
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
        ("max_urls_per_call", 0),
        ("min_markdown_words", 0),
        ("max_consecutive_failures", 0),
        ("max_reader_dispatches", 0),
        ("max_researchers", 0),
    ],
)
def test_non_positive_limits_are_rejected(tmp_path, monkeypatch, setting, bad_value):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    original = {
        "page_timeout_ms": 20000,
        "max_concurrency": 8,
        "per_page_char_cap": 9000,
        "max_urls_per_call": 3,
        "min_markdown_words": 30,
        "max_consecutive_failures": 4,
        "max_reader_dispatches": 6,
        "max_researchers": 4,
    }
    # These two live in `[agent]`, which VALID_TOML omits (the omitted-section default is
    # covered by test_agent_section_omitted_falls_back_to_documented_defaults) -- append a
    # literal default here so the same `.replace()` pattern has something to swap.
    base_toml = VALID_TOML
    if setting in ("max_reader_dispatches", "max_researchers"):
        base_toml += f"\n[agent]\n{setting} = {original[setting]}\n"
    toml_content = base_toml.replace(f"{setting} = {original[setting]}", f"{setting} = {bad_value}")
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
                '[roles.researcher]\nprovider = "cerebras"\nmodel = "gemma-4-31b"\n\n',
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


def test_shipped_harness_toml_loads_with_no_todo_placeholders_left(monkeypatch):
    """Literal "TODO" values are well-formed strings `load_config` accepts, so nothing but this
    test stops one from being reintroduced into the checked-in config; `build_chat_model` rejects a
    `TODO` handed to it at runtime, and this guards the file itself.

    Checks shape, not the endpoint/model in use: pinning those deployment facts would fail on a
    legitimate swap, against the config-swappable invariant.
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "any")

    config = load_config()

    assert config.providers["opencode"].base_url.startswith("https://")
    assert config.roles["head"].model != ""
    offenders = [
        f"providers.{name}.base_url" for name, p in config.providers.items() if p.base_url == "TODO"
    ] + [f"roles.{name}.model" for name, r in config.roles.items() if r.model == "TODO"]
    assert offenders == []


def test_shipped_harness_toml_has_no_browser_surface(monkeypatch):
    # Chromium is the only path; no config key selects a browser backend (R1).
    monkeypatch.setenv("OPENCODE_API_KEY", "any")
    monkeypatch.setenv("CEREBRAS_API_KEY", "any")

    config = load_config()

    assert not hasattr(config, "browser")


def test_browser_table_is_rejected_now_that_the_backend_is_gone(tmp_path, monkeypatch):
    # The key is genuinely GONE rather than merely unread: a config still carrying it fails loudly.
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    # A config file written before the backend was removed and never updated.
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


def test_missing_researcher_role_still_loads(tmp_path, monkeypatch):
    """`head` is the only role required at LOAD time: a config missing `[roles.researcher]`
    (or `reader`/`verifier`) loads fine and fails loud later, at build/preflight, via
    `ModelError` naming the role. Pins the relaxation so a future tightening of
    `_cross_check_roles` is a deliberate edit, not an accident.
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        '[roles.researcher]\nprovider = "cerebras"\nmodel = "gemma-4-31b"\n\n', ""
    )
    assert toml_content != VALID_TOML, "fixture drifted from VALID_TOML — update the block"
    path = _write(tmp_path, toml_content)

    config = load_config(path)

    assert "researcher" not in config.roles
    assert "head" in config.roles


def test_agent_section_loads_declared_values(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = (
        VALID_TOML
        + """
[agent]
max_rounds = 12
wall_clock_seconds = 600
synthesis_margin_seconds = 200
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
    assert config.agent.synthesis_margin_seconds == 200
    assert config.agent.workspace_dir == Path("custom-workspace")
    assert config.agent.reports_dir == Path("custom-reports")
    assert config.agent.max_retries == 4
    assert config.agent.request_timeout_seconds == 30.0


def test_agent_section_omitted_falls_back_to_documented_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, VALID_TOML)

    config = load_config(path)

    assert config.agent.max_rounds == 50
    assert config.agent.wall_clock_seconds == 1800
    assert config.agent.synthesis_margin_seconds == 240
    assert config.agent.workspace_dir == Path.home() / "deep-research" / "workspace"
    assert config.agent.reports_dir == Path.home() / "deep-research" / "reports"
    assert config.agent.max_retries == 2
    assert config.agent.request_timeout_seconds == 120.0
    assert config.agent.max_researchers == 4


def test_shipped_harness_toml_declares_the_researcher_cap(monkeypatch):
    """The cap is harness-enforced (D1) -- `dispatch_researcher` refuses past it -- so the
    shipped config states it outright rather than leaving it to the model default."""
    monkeypatch.setenv("OPENCODE_API_KEY", "any")

    config = load_config()

    assert config.agent.max_researchers == 4


def test_synthesis_margin_at_or_above_the_wall_clock_is_rejected(tmp_path, monkeypatch):
    """R7 (parent plan Phase 5): the reserve is measured back from the wall clock, so a margin
    equal to (or larger than) `wall_clock_seconds` would fire at or after the clock has already
    expired -- diagnosed at load time, not discovered mid-run.
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = (
        VALID_TOML + "\n[agent]\nwall_clock_seconds = 100\nsynthesis_margin_seconds = 100\n"
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "synthesis_margin_seconds" in message
    assert "wall_clock_seconds" in message


def test_synthesis_margin_seconds_zero_is_accepted(tmp_path, monkeypatch):
    """Contracts: `0` is the documented disable value, not a violation of `synthesis_margin_seconds
    < wall_clock_seconds` -- it must load cleanly regardless of `wall_clock_seconds`.
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML + "\n[agent]\nsynthesis_margin_seconds = 0\n"
    path = _write(tmp_path, toml_content)

    config = load_config(path)

    assert config.agent.synthesis_margin_seconds == 0
    assert config.agent.max_reader_dispatches == 6


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


def test_dotenv_beside_the_toml_supplies_a_missing_api_key(tmp_path, monkeypatch):
    """Keys live in `.env`, never in `harness.toml` — so loading it is part of loading config."""
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, VALID_TOML)
    (tmp_path / ".env").write_text(
        "# a comment\n\nOPENCODE_API_KEY=from-dotenv\nMALFORMED_NO_EQUALS\n", encoding="utf-8"
    )

    config = load_config(path)

    assert config.providers["opencode"].api_key == "from-dotenv"


def test_a_real_env_var_wins_over_the_same_key_in_dotenv(tmp_path, monkeypatch):
    """Standard dotenv precedence: the file fills gaps, it never overrides the environment.

    Flipping this would let a stale checked-out `.env` silently outrank the key an operator
    exported for one run.
    """
    monkeypatch.setenv("OPENCODE_API_KEY", "from-environment")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, VALID_TOML)
    (tmp_path / ".env").write_text("OPENCODE_API_KEY=from-dotenv\n", encoding="utf-8")

    config = load_config(path)

    assert config.providers["opencode"].api_key == "from-environment"


# --- Phase 3: GuardSettings / [guard] ---------------------------------------------------


def test_guard_defaults_to_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, VALID_TOML)

    config = load_config(path)

    assert config.guard.enabled is True


def test_guard_section_is_parsed_from_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML + "\n[guard]\nenabled = false\n"
    path = _write(tmp_path, toml_content)

    config = load_config(path)

    assert config.guard.enabled is False


def test_guard_section_rejects_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML + '\n[guard]\nenabled = true\ntypo_key = "oops"\n'
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "typo_key" in str(excinfo.value)


def test_guard_settings_model_defaults_enabled():
    assert GuardSettings().enabled is True


# --- Phase 3: BlocklistSettings / [blocklist] -------------------------------------------


def test_blocklist_path_defaults_to_the_home_relative_location(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, VALID_TOML)

    config = load_config(path)

    assert config.blocklist.path == Path.home() / "deep-research" / "blocked-domains.json"


def test_empty_blocklist_section_parses_and_keeps_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML + "\n[blocklist]\n"
    path = _write(tmp_path, toml_content)

    config = load_config(path)

    assert config.blocklist.path == Path.home() / "deep-research" / "blocked-domains.json"


def test_blocklist_section_is_parsed_from_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML + '\n[blocklist]\npath = "custom-blocked-domains.json"\n'
    path = _write(tmp_path, toml_content)

    config = load_config(path)

    assert config.blocklist.path == Path("custom-blocked-domains.json")


def test_blocklist_section_rejects_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML + '\n[blocklist]\ntypo_key = "oops"\n'
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "typo_key" in str(excinfo.value)


def test_blocklist_settings_model_defaults_path():
    assert BlocklistSettings().path == Path.home() / "deep-research" / "blocked-domains.json"


# --- Phase 2: RoleConfig.choices (/model picker) ----------------------------------------


def test_role_choices_defaults_to_none(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, VALID_TOML)

    config = load_config(path)

    assert config.roles["head"].choices is None


def test_role_choices_loads_from_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        '[roles.head]\nprovider = "opencode"\nmodel = "glm-5.2"\n',
        '[roles.head]\nprovider = "opencode"\nmodel = "glm-5.2"\n'
        'choices = ["glm-5.2", "glm-5.3", "kimi-k3"]\n',
    )
    path = _write(tmp_path, toml_content)

    config = load_config(path)

    assert config.roles["head"].choices == ["glm-5.2", "glm-5.3", "kimi-k3"]


def test_role_choices_empty_list_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        '[roles.head]\nprovider = "opencode"\nmodel = "glm-5.2"\n',
        '[roles.head]\nprovider = "opencode"\nmodel = "glm-5.2"\nchoices = []\n',
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "choices" in str(excinfo.value)


# `3` is here deliberately alongside the blank strings: it is rejected by pydantic's own
# `list[str]` type check, not by `_validate_choices`, and both rejections have to keep
# surfacing as a `ConfigError` to the operator. Naming which layer catches which is the point
# -- the previous version of this test read as though the validator handled both (PR #25 review).
@pytest.mark.parametrize(
    ("bad_choices", "rejected_by"),
    [
        (["glm-5.2", ""], "_validate_choices"),
        (["glm-5.2", "   "], "_validate_choices"),
        (["glm-5.2", 3], "pydantic's list[str] type check"),
    ],
)
def test_role_choices_rejects_blank_or_non_string_entries(
    tmp_path, monkeypatch, bad_choices, rejected_by
):
    monkeypatch.setenv("OPENCODE_API_KEY", "opencode-secret")
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    toml_content = VALID_TOML.replace(
        '[roles.head]\nprovider = "opencode"\nmodel = "glm-5.2"\n',
        f'[roles.head]\nprovider = "opencode"\nmodel = "glm-5.2"\nchoices = {bad_choices!r}\n',
    )
    path = _write(tmp_path, toml_content)

    with pytest.raises(ConfigError):
        load_config(path)


def test_shipped_harness_toml_head_role_has_choices_including_current_model(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "any")

    config = load_config()

    assert config.roles["head"].choices is not None
    assert config.roles["head"].model in config.roles["head"].choices
    assert len(config.roles["head"].choices) >= 2


def test_dotenv_value_containing_an_equals_sign_survives_intact(tmp_path, monkeypatch):
    """Split on the FIRST `=` only — base64 and URL-shaped secrets routinely contain more."""
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("CEREBRAS_API_KEY", "cerebras-secret")
    path = _write(tmp_path, VALID_TOML)
    (tmp_path / ".env").write_text("OPENCODE_API_KEY=abc==def=\n", encoding="utf-8")

    config = load_config(path)

    assert config.providers["opencode"].api_key == "abc==def="
