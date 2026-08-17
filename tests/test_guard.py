"""Behavioral tests for harness.guard over real attack/benign fixtures."""

from pathlib import Path

import pytest

from harness.guard import scan

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "injection"

# Stable family order per the plan's frozen contract (Phase 2 `## Contracts`).
FAMILIES = [
    "instruction_override",
    "role_spoofing",
    "ai_directed",
    "obfuscation",
    "exfil_markup",
]


def _load(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _attack_fixtures(family: str) -> list[Path]:
    return sorted(FIXTURES_DIR.glob(f"attack_{family}_*"))


@pytest.mark.parametrize("family", FAMILIES)
def test_each_family_blocks_its_attack_fixtures(family: str) -> None:  # R1
    paths = _attack_fixtures(family)
    assert paths, f"no attack fixture found for family {family!r}"
    for path in paths:
        result = scan(path.read_text(encoding="utf-8"))
        assert result.blocked is True, f"{path.name} should be blocked"
        assert family in result.signals, f"{path.name} should name {family!r} in signals"


def test_benign_article_fixtures_pass() -> None:  # R1
    paths = sorted(FIXTURES_DIR.glob("benign_article_*"))
    assert paths
    for path in paths:
        result = scan(path.read_text(encoding="utf-8"))
        assert result.blocked is False, f"{path.name} should not be blocked"
        assert result.signals == []


def test_benign_security_blog_quoting_override_phrase_is_blocked() -> None:  # R1
    # Accepted cost per the plan's R1 line: a security article that merely QUOTES an
    # override phrase inside a fenced code block still blocks — thinner coverage,
    # disclosed, rather than trying to parse markdown structure to exempt it.
    paths = sorted(FIXTURES_DIR.glob("benign_security_blog_*"))
    assert paths
    for path in paths:
        result = scan(path.read_text(encoding="utf-8"))
        assert result.blocked is True, f"{path.name} should be blocked (accepted cost)"


def test_empty_and_whitespace_only_text_passes() -> None:  # R1
    assert scan("").blocked is False
    assert scan("   \n\t  ").blocked is False


def test_scanning_the_same_fixture_twice_is_deterministic() -> None:  # R1
    text = _load("attack_instruction_override_ignore.txt")
    assert scan(text) == scan(text)


def test_signals_are_drawn_only_from_the_stable_family_names() -> None:  # R1
    for family in FAMILIES:
        for path in _attack_fixtures(family):
            result = scan(path.read_text(encoding="utf-8"))
            assert set(result.signals) <= set(FAMILIES)


def test_multi_family_attack_sample_lists_each_fired_family_once() -> None:  # R1
    # No dedicated fixture file: combine two single-family attack fixtures into one
    # text and assert each fired family is named exactly once, never duplicated.
    combined = (
        _load("attack_instruction_override_ignore.txt")
        + "\n"
        + _load("attack_role_spoofing_dan.txt")
    )
    result = scan(combined)
    assert result.signals.count("instruction_override") == 1
    assert result.signals.count("role_spoofing") == 1
