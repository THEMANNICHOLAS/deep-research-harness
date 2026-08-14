"""Behavioral tests for harness.sources."""

import re
from datetime import datetime

import pytest

from harness.sources import SourceRegistry, normalize_url

RESOLVE_TEXT = "[S1] Some claim here [S1] and another [S2] in the same sentence. End [S1]"


def test_ids_are_assigned_sequentially_from_s1_in_insertion_order():
    registry = SourceRegistry()

    id_a = registry.add("https://example.com/a")
    id_b = registry.add("https://example.com/b")
    id_c = registry.add("https://example.com/c")

    assert (id_a, id_b, id_c) == ("S1", "S2", "S3")
    assert [s.id for s in registry.all()] == ["S1", "S2", "S3"]


def test_same_url_added_twice_returns_same_id_and_one_entry():
    registry = SourceRegistry()

    first_id = registry.add("https://example.com/a")
    second_id = registry.add("https://example.com/a")

    assert first_id == second_id
    assert len(registry.all()) == 1


@pytest.mark.parametrize(
    ("url_a", "url_b"),
    [
        ("https://example.com/a", "https://example.com/a/"),
        ("https://example.com/a", "https://example.com:443/a"),
        ("http://example.com/a", "http://example.com:80/a"),
        ("https://example.com/a", "https://example.com/a#section"),
    ],
)
def test_urls_differing_only_by_slash_port_or_fragment_share_an_id(url_a, url_b):
    registry = SourceRegistry()

    id_a = registry.add(url_a)
    id_b = registry.add(url_b)

    assert id_a == id_b
    assert len(registry.all()) == 1


def test_urls_differing_by_query_string_get_different_ids():
    registry = SourceRegistry()

    id_a = registry.add("https://example.com/a?q=1")
    id_b = registry.add("https://example.com/a?q=2")

    assert id_a != id_b
    assert len(registry.all()) == 2


def test_link_renders_domain_and_url_and_raises_for_unknown_id():
    registry = SourceRegistry()
    registry.add("https://example.com/a")

    assert registry.link("S1") == "[example.com](https://example.com/a)"

    with pytest.raises(KeyError) as excinfo:
        registry.link("S9")
    assert "S9" in str(excinfo.value)


def test_resolve_replaces_every_known_marker():
    registry = SourceRegistry()
    registry.add("https://example.com/a")
    registry.add("https://example.org/b")

    result = registry.resolve(RESOLVE_TEXT)

    assert "[S" not in result
    assert result.count("[example.com](https://example.com/a)") == 3
    assert result.count("[example.org](https://example.org/b)") == 1


def test_resolve_leaves_unknown_marker_verbatim_and_reports_it():
    registry = SourceRegistry()
    registry.add("https://example.com/a")
    text = "Known [S1] and unknown [S9] markers."

    result = registry.resolve(text)

    assert "[example.com](https://example.com/a)" in result
    assert "[S9]" in result
    assert registry.unresolved_ids(text) == ["S9"]


def test_resolve_on_text_without_markers_returns_it_unchanged():
    registry = SourceRegistry()
    registry.add("https://example.com/a")
    text = "No markers in this text at all."

    assert registry.resolve(text) == text


def test_get_returns_the_registered_source_and_none_for_an_unknown_id():
    registry = SourceRegistry()
    registry.add("https://example.com/a", title="An Article")

    source = registry.get("S1")

    assert source is not None
    assert (source.id, source.url, source.title) == ("S1", "https://example.com/a", "An Article")
    assert registry.get("S9") is None


def test_re_adding_a_url_keeps_the_first_title():
    registry = SourceRegistry()
    registry.add("https://example.com/a", title="First")
    registry.add("https://example.com/a", title="Second")

    source = registry.get("S1")

    assert source is not None
    assert source.title == "First"


def test_ipv6_host_keeps_its_brackets_when_normalized():
    assert normalize_url("http://[::1]:8080/x") == "http://[::1]:8080/x"
    assert normalize_url("HTTP://[::1]/x#frag") == "http://[::1]/x"


@pytest.mark.parametrize(
    "malformed",
    [
        "http://example.com:notaport/x",
        "http://example.com:99999/x",
        "http://[::1/x",
    ],
)
def test_malformed_url_normalizes_to_itself_instead_of_raising(malformed):
    assert normalize_url(malformed) == malformed

    registry = SourceRegistry()
    source = registry.get(registry.add(malformed))

    assert source is not None
    assert source.url == malformed


@pytest.mark.parametrize(
    "malformed",
    [
        "http://example.com:notaport/x",
        "http://example.com:99999/x",
        "http://[::1/x",
    ],
)
def test_link_and_resolve_never_raise_for_a_registered_malformed_url(malformed):
    registry = SourceRegistry()
    source_id = registry.add(malformed)

    link = registry.link(source_id)

    assert link.endswith(f"]({malformed})")
    assert registry.resolve(f"See [{source_id}].") == f"See {link}."


def test_urls_differing_only_by_password_stay_distinct_sources():
    registry = SourceRegistry()

    assert normalize_url("http://:secret@example.com/x") == "http://:secret@example.com/x"
    assert registry.add("http://:a@example.com/x") != registry.add("http://:b@example.com/x")
    assert len(registry.all()) == 2


# --- Phase 6: per-run `run_id`, so source captures never collide across runs -----------


def test_run_id_defaults_to_a_sortable_stamp_carrying_a_collision_suffix():
    registry = SourceRegistry()

    stamp, _, suffix = registry.run_id.rpartition("-")
    datetime.strptime(stamp, "%Y-%m-%d-%H%M%S")  # must not raise
    assert re.fullmatch(r"[0-9a-f]{8}", suffix)


def test_two_default_registries_never_share_a_run_id():
    """Two runs launched in the same second shared one captured-sources directory and overwrote
    each other's `S<n>.md` files mid-run. The suffix is random rather than time-derived, so this
    assertion is deterministic, not flaky.
    """
    assert SourceRegistry().run_id != SourceRegistry().run_id


def test_run_id_explicit_value_is_used_verbatim():
    registry = SourceRegistry(run_id="2026-08-12-093000")

    assert registry.run_id == "2026-08-12-093000"
