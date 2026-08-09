"""Behavioral tests for harness.prompts."""

from string import Template

import pytest

from harness.prompts import PromptError, render, required_variables


@pytest.fixture
def prompt_dir(tmp_path, monkeypatch):
    """Point harness.prompts at a temp directory of .md fixtures."""
    monkeypatch.setattr("harness.prompts._PROMPTS_DIR", tmp_path)
    return tmp_path


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


@pytest.mark.parametrize("name", ["orchestrator", "subagent"])
def test_shipped_prompts_render_with_their_declared_variables(name):
    variables = required_variables(name)

    assert variables

    rendered = render(name, **{v: f"<{v}>" for v in variables})

    # No unsubstituted placeholder survives rendering. Checked via get_identifiers rather
    # than `"$" not in rendered`, because a `$$` escape legitimately renders to a literal `$`.
    assert Template(rendered).get_identifiers() == []
