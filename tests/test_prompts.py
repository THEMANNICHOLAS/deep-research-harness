"""Behavioral tests for harness.prompts."""

from string import Template

import pytest

from harness.prompts import PromptError, render, required_variables

# The frozen delegation-tier contracts (R5). The reader tier is wired; the subagent
# contract is not.
TIER_CONTRACTS = ["subagent", "reader"]


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    """Point harness.prompts at a temp directory of .md fixtures."""
    monkeypatch.setattr("harness.prompts._PROMPTS_DIR", tmp_path)
    return tmp_path


def _render_shipped(name):
    """Render a shipped prompt, stubbing every declared variable with its own name."""
    return render(name, **{v: f"<{v}>" for v in required_variables(name)})


def test_render_substitutes_all_variables(prompt_dir):
    (prompt_dir / "greet.md").write_text(
        "Hello $alpha, hello again $alpha. Beta is $beta.", encoding="utf-8"
    )

    result = render("greet", alpha="Ann", beta="Bee")

    assert result.count("Ann") == 2
    assert "Bee" in result
    assert "$" not in result


def test_missing_variable_raises_prompt_error_naming_prompt_and_variable(prompt_dir):
    (prompt_dir / "greet.md").write_text("Hello $alpha, and $beta.", encoding="utf-8")

    with pytest.raises(PromptError) as exc_info:
        render("greet", alpha="Ann")

    message = str(exc_info.value)
    assert "greet" in message
    assert "beta" in message


def test_unknown_prompt_name_raises_prompt_error_naming_it(prompt_dir):
    with pytest.raises(PromptError) as exc_info:
        render("does_not_exist")

    assert "does_not_exist" in str(exc_info.value)


@pytest.mark.parametrize("name", ["../escape", "sub/dir", "sub\\dir"])
def test_path_traversal_shaped_names_raise_prompt_error(prompt_dir, name):
    with pytest.raises(PromptError) as exc_info:
        render(name)

    assert "invalid prompt name" in str(exc_info.value)


def test_malformed_placeholder_in_the_file_raises_prompt_error_not_value_error(prompt_dir):
    (prompt_dir / "broken.md").write_text("Ends with a bare $", encoding="utf-8")

    with pytest.raises(PromptError) as exc_info:
        render("broken")

    message = str(exc_info.value)
    assert "broken" in message
    assert "placeholder" in message


def test_required_variables_reports_exactly_the_placeholders(prompt_dir):
    (prompt_dir / "vars.md").write_text(
        "First $alpha, second $beta, third $alpha again. Literal $$ sign.",
        encoding="utf-8",
    )

    assert required_variables("vars") == {"alpha", "beta"}


def test_required_variables_unknown_prompt_raises(prompt_dir):
    with pytest.raises(PromptError) as exc_info:
        required_variables("nope")

    assert "nope" in str(exc_info.value)


def test_json_braces_and_dollar_escape_render_unchanged(prompt_dir):
    (prompt_dir / "json_example.md").write_text(
        'Example call: {"tool": "search_web", "args": {"query": "$topic"}}. Price: $$100.',
        encoding="utf-8",
    )

    result = render("json_example", topic="x")

    assert '{"tool": "search_web"' in result
    assert "$topic" not in result
    assert "$100" in result


@pytest.mark.parametrize("name", ["orchestrator", "verify", *TIER_CONTRACTS])
def test_shipped_prompts_render_with_their_declared_variables(name):
    assert required_variables(name)

    rendered = _render_shipped(name)

    # No unsubstituted placeholder survives. Checked via `get_identifiers` rather than
    # `"$" not in rendered`, because a `$$` escape legitimately renders a literal `$`.
    assert Template(rendered).get_identifiers() == []


@pytest.mark.parametrize("name", TIER_CONTRACTS)
def test_tier_contracts_declare_exactly_their_placeholders(name):
    # A tier receives its task through the delegation call at run time, never by substitution, so
    # neither contract declares a task or facet placeholder.
    assert required_variables(name) == {"current_date", "max_urls_per_call"}


@pytest.mark.parametrize("name", TIER_CONTRACTS)
def test_tier_contract_missing_variable_raises_prompt_error_naming_both(name):
    supplied = {v: f"<{v}>" for v in required_variables(name) if v != "current_date"}

    with pytest.raises(PromptError) as exc_info:
        render(name, **supplied)

    message = str(exc_info.value)
    assert name in message
    assert "current_date" in message


@pytest.mark.parametrize("name", TIER_CONTRACTS)
def test_tier_contracts_do_not_reference_ask_user(name):
    # D1: a tier that can interrupt the developer would stall the run mid-fan-out.
    assert "ask_user" not in _render_shipped(name)


@pytest.mark.parametrize("name", TIER_CONTRACTS)
@pytest.mark.parametrize(
    "field",
    ["Objective", "Output format", "Tools", "Boundaries", "Findings", "Source IDs", "Conflicts"],
)
def test_tier_contracts_name_their_frozen_fields(name, field):
    # R5: the next round builds subagent definitions against exactly these names — four a task
    # must carry, three a tier must return. Anchored to the bolded bullet, since a bare "tools"
    # would also match the `# Tools` heading and let a renamed field slip through.
    assert f"**{field}**" in _render_shipped(name)


def test_orchestrator_prompt_teaches_the_full_delegation_protocol():
    """R1's prompt half (Phase 3): the lead delegates reading to the reader subagent rather
    than fetching directly, and knows what to do when that delegation fails.
    """
    rendered = render("orchestrator", current_date="2026-01-01", max_urls_per_call=7)

    assert 'subagent_type="reader"' in rendered
    # The batching bound must appear near the delegation instruction, not merely anywhere in
    # the prompt (D5) — a stray "7" elsewhere would pass a looser assertion.
    delegation_pos = rendered.index('subagent_type="reader"')
    context = rendered[max(0, delegation_pos - 400) : delegation_pos + 400]
    assert "7" in context

    assert "never quote raw page text" in rendered.lower()

    assert "fetch_raw" in rendered
    assert "READER FAILED (" in rendered
    assert "empty digest" in rendered.lower()

    # The lead no longer calls a fetch tool directly (R1) — it only delegates.
    assert "fetch_pages" not in rendered
