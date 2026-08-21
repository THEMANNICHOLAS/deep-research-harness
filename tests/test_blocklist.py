"""Behavioral tests for harness.blocklist (R3/R4, D3)."""

import json

import pytest

from harness.blocklist import fires_challenge_marker, hostname_of, load_blocklist
from tests.conftest import _challenge_fixtures

# --- Round trip: load / add / persist ----------------------------------------------------


def test_load_on_a_missing_path_is_empty_and_creates_no_file(tmp_path):
    path = tmp_path / "blocked-domains.json"

    blocklist = load_blocklist(path)

    assert blocklist.contains("example.com") is False
    assert not path.exists()


def test_add_writes_the_file_and_a_fresh_load_contains_the_hostname(tmp_path):
    path = tmp_path / "blocked-domains.json"
    blocklist = load_blocklist(path)

    blocklist.add("walled.test", "403")

    assert path.exists()
    reloaded = load_blocklist(path)
    assert reloaded.contains("walled.test") is True


def test_the_written_file_is_an_object_of_objects_with_reason_and_first_seen(tmp_path):
    path = tmp_path / "blocked-domains.json"
    blocklist = load_blocklist(path)

    blocklist.add("walled.test", "403")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert set(data) == {"walled.test"}
    entry = data["walled.test"]
    assert isinstance(entry, dict)
    assert entry["reason"] == "403"
    assert "first_seen" in entry


def test_a_hand_edited_unknown_key_survives_an_add_of_a_different_hostname(tmp_path):
    path = tmp_path / "blocked-domains.json"
    path.write_text(
        json.dumps(
            {
                "existing.test": {
                    "reason": "403",
                    "first_seen": "2026-01-01T00:00:00+00:00",
                    "note": "walled since the API change",
                }
            }
        ),
        encoding="utf-8",
    )
    blocklist = load_blocklist(path)

    blocklist.add("new.test", "401")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["existing.test"]["note"] == "walled since the API change"
    assert data["new.test"]["reason"] == "401"


def test_a_concurrent_writers_entry_survives_a_later_add(tmp_path):
    """D3's accepted race: read-merge-`os.replace` means a concurrent run's own write, made
    behind this `Blocklist`'s back after it was loaded, is not clobbered by a later `add` —
    the on-disk entry is merged in, not overwritten."""
    path = tmp_path / "blocked-domains.json"
    blocklist = load_blocklist(path)
    blocklist.add("first.test", "403")

    # A DIFFERENT writer (a concurrent run) appends an entry behind this instance's back, by
    # writing the JSON directly rather than going through this `Blocklist`'s `add`.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["second.test"] = {"reason": "401", "first_seen": "2026-01-01T00:00:00+00:00"}
    path.write_text(json.dumps(on_disk), encoding="utf-8")

    blocklist.add("third.test", "challenge")

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"first.test", "second.test", "third.test"}
    assert data["second.test"]["reason"] == "401"
    assert data["third.test"]["reason"] == "challenge"


def test_adding_an_already_present_hostname_does_not_change_its_record(tmp_path):
    path = tmp_path / "blocked-domains.json"
    blocklist = load_blocklist(path)
    blocklist.add("walled.test", "403")
    first_write = json.loads(path.read_text(encoding="utf-8"))["walled.test"]

    blocklist.add("walled.test", "challenge")

    second_write = json.loads(path.read_text(encoding="utf-8"))["walled.test"]
    assert second_write == first_write
    assert second_write["reason"] == "403"


def test_a_hand_edited_mixed_case_hostname_key_is_matched_lowercased(tmp_path):
    path = tmp_path / "blocked-domains.json"
    path.write_text(
        json.dumps({"Walled.TEST": {"reason": "403", "first_seen": "2026-01-01T00:00:00+00:00"}}),
        encoding="utf-8",
    )

    blocklist = load_blocklist(path)

    assert blocklist.contains("walled.test") is True


@pytest.mark.parametrize(
    "corrupt_content",
    [
        "not json at all",
        json.dumps(["a", "list", "not", "an", "object"]),
        json.dumps({"walled.test": "403"}),
    ],
    ids=["not-json", "json-list", "hostname-to-string"],
)
def test_a_corrupt_file_loads_empty_and_does_not_raise(tmp_path, corrupt_content):
    path = tmp_path / "blocked-domains.json"
    path.write_text(corrupt_content, encoding="utf-8")

    blocklist = load_blocklist(path)

    assert blocklist.contains("walled.test") is False


# --- hostname_of ---------------------------------------------------------------------------


def test_hostname_of_a_normal_url_is_lowercased():
    assert hostname_of("https://Example.COM/path") == "example.com"


def test_hostname_of_strips_a_port():
    assert hostname_of("https://example.com:8443/path") == "example.com"


def test_hostname_of_an_unparseable_url_is_none():
    # An unterminated IPv6 literal: `urlsplit` raises `ValueError` on this shape.
    assert hostname_of("http://[::1") is None


def test_hostname_of_a_relative_path_is_none():
    assert hostname_of("/just/a/path") is None


def test_hostname_of_an_ipv6_literal_returns_the_bracket_free_host():
    assert hostname_of("http://[2001:db8::1]:8080/") == "2001:db8::1"


# --- fires_challenge_marker ------------------------------------------------------------


def test_the_challenge_fixture_glob_is_non_empty():
    """Guard against an empty fixture directory silently producing zero parametrized cases
    below — a glob with no matches would make `test_each_challenge_fixture_fires_the_marker_
    detector` vacuously pass by collecting nothing."""
    assert _challenge_fixtures() != []


@pytest.mark.parametrize("path", _challenge_fixtures(), ids=lambda p: p.stem)
def test_each_challenge_fixture_fires_the_marker_detector(path):
    text = path.read_text(encoding="utf-8")
    assert fires_challenge_marker(text) is True


def test_ordinary_prose_with_no_marker_does_not_fire():
    text = "This is an ordinary page about houseplants and how often to water them."
    assert fires_challenge_marker(text) is False
