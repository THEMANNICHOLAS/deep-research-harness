"""Behavioral tests for harness.prompts."""

from string import Template

import pytest

from harness.prompts import PromptError, render, required_variables

# The frozen delegation-tier contracts (R5). Both tiers are wired: `subagent` is the
# researcher's live system prompt, `reader` the reader's.
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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("subagent", {"current_date", "max_urls_per_call", "max_reader_dispatches"}),
        ("reader", {"current_date", "max_urls_per_call"}),
    ],
)
def test_tier_contracts_declare_exactly_their_placeholders(name, expected):
    # A tier receives its task through the delegation call at run time, never by substitution, so
    # neither contract declares a task or facet placeholder. Only `subagent` additionally
    # declares the enforced reader-dispatch cap (R5, Phase 4) so the harness value and the
    # prompt's own budget claim cannot disagree; the reader prompt does not mention the cap.
    assert required_variables(name) == expected


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


def test_subagent_prompt_teaches_reader_delegation_recovery_and_budget():
    """The researcher prompt's reader-delegation instructions, `fetch_raw` recovery rule, and
    budget caps are load-bearing run behavior with no other guard (PR review cleanup): the
    researcher must delegate reading (never fetch pages itself), reach for `fetch_raw` only
    after a failed delegation, and stay inside its search/dispatch budget.
    """
    from harness.prompts import _PROMPTS_DIR

    rendered = render(
        "subagent", current_date="2026-01-01", max_urls_per_call=5, max_reader_dispatches=6
    )

    # Reading is delegated: the reader dispatch carries the per-call URL cap and the facet.
    assert 'subagent_type="reader"' in rendered
    assert "up to 5" in rendered
    assert "fetch_pages" not in rendered

    # `fetch_raw` is recovery only, explicitly ordered AFTER a failed/empty delegation.
    assert "recovery only" in rendered.lower()
    assert "never as a first resort" in rendered

    # The budget caps: bounded searching and dispatching, partial findings over overrun.
    assert "4 searches" in rendered
    assert "6 reader dispatches" in rendered

    # R5 (Phase 4): the reader-dispatch cap is templated from config, not a literal the prompt
    # and the harness-enforced middleware can silently drift apart on.
    raw = (_PROMPTS_DIR / "subagent.md").read_text(encoding="utf-8")
    assert "$max_reader_dispatches" in raw


def test_orchestrator_prompt_teaches_the_full_delegation_protocol():
    """R1's prompt half (Phase 2 Step 3): the lead delegates research angles to the researcher
    subagent rather than researching directly, and knows what to do when that delegation fails.
    """
    rendered = render("orchestrator", current_date="2026-01-01")

    assert 'subagent_type="researcher"' in rendered
    # The concurrent-researcher bound must appear near the delegation instruction, not merely
    # anywhere in the prompt (D5) — a stray "3" elsewhere would pass a looser assertion.
    delegation_pos = rendered.index('subagent_type="researcher"')
    context = rendered[max(0, delegation_pos - 400) : delegation_pos + 400]
    assert "3" in context

    assert "never search or fetch a page yourself" in rendered.lower()

    assert "RESEARCHER FAILED (" in rendered
    assert "empty report" in rendered.lower()

    # The lead no longer searches or fetches directly (R1) — it only delegates.
    assert "search_web" not in rendered
    assert "fetch_pages" not in rendered
    assert "fetch_raw" not in rendered
    assert 'subagent_type="reader"' not in rendered
