"""Behavioral tests for harness.sources."""

import re
from datetime import datetime

import pytest

from harness.sources import (
    SourceRegistry,
    normalize_url,
    note_digest_candidate,
    pending_digest_scope,
)

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


# --- Phase 2: per-source read mode (digested / fallback / unread), R5's recording seam ---


def test_a_freshly_added_source_defaults_to_unread():
    registry = SourceRegistry()
    source_id = registry.add("https://example.com/a")

    source = registry.get(source_id)
    assert source is not None
    assert source.read_mode == "unread"


def test_mark_read_sets_digested_and_fallback_modes_independently():
    registry = SourceRegistry()
    digested_id = registry.add("https://example.com/a")
    fallback_id = registry.add("https://example.com/b")

    registry.mark_read(digested_id, "digested")
    registry.mark_read(fallback_id, "fallback")

    assert registry.get(digested_id).read_mode == "digested"
    assert registry.get(fallback_id).read_mode == "fallback"


def test_re_marking_a_source_overwrites_its_read_mode():
    # Last write wins: `mark_read` itself carries no precedence — fallback.py's own guard is
    # what keeps `fetch_raw` from downgrading a digested source (see test_fallback.py).
    registry = SourceRegistry()
    source_id = registry.add("https://example.com/a")

    registry.mark_read(source_id, "digested")
    registry.mark_read(source_id, "fallback")

    assert registry.get(source_id).read_mode == "fallback"


def test_note_digest_candidate_collects_only_inside_a_pending_scope():
    """The delegation-boundary seam: a nomination outside any scope is a silent no-op (a
    directly-invoked fetch tool must not mark anything), and one inside lands in that scope's
    own list.
    """
    note_digest_candidate("S1")  # no scope active: swallowed, never leaks into a later scope

    with pending_digest_scope() as pending:
        note_digest_candidate("S2")

    assert pending == ["S2"]


# --- Source hygiene Step 1: canonical-URL dedup (R1) ---


@pytest.mark.parametrize(
    ("url_a", "url_b"),
    [
        ("https://arxiv.org/abs/2405.11111", "https://arxiv.org/pdf/2405.11111"),
        ("https://arxiv.org/abs/2405.11111", "https://arxiv.org/html/2405.11111v2"),
        ("https://arxiv.org/abs/2405.11111v1", "https://arxiv.org/abs/2405.11111v3"),
        ("https://arxiv.org/pdf/2405.11111v2.pdf", "https://arxiv.org/abs/2405.11111"),
        ("https://www.arxiv.org/abs/2405.11111", "https://arxiv.org/abs/2405.11111"),
    ],
)
def test_arxiv_url_variants_share_an_id(url_a, url_b):
    registry = SourceRegistry()

    id_a = registry.add(url_a)
    id_b = registry.add(url_b)

    assert id_a == id_b
    assert len(registry.all()) == 1


def test_registering_the_2026_08_15_dup_pair_shapes_yields_4_ids_not_6():
    """The exact live-run URLs (from a homelab report) were not retrievable locally, so these
    are the observed shapes that produced duplicate S21/S25 and S23/S26 entries in that
    report — abs-vs-pdf and abs-vs-html`vN` — not the literal URLs.
    """
    registry = SourceRegistry()

    ids = [
        registry.add("https://arxiv.org/abs/2405.11111"),
        registry.add("https://arxiv.org/pdf/2405.11111"),
        registry.add("https://arxiv.org/abs/2405.22222"),
        registry.add("https://arxiv.org/html/2405.22222v3"),
        registry.add("https://example.com/paper-a"),
        registry.add("https://example.org/paper-b"),
    ]

    assert len(set(ids)) == 4
    assert len(registry.all()) == 4


@pytest.mark.parametrize(
    "tracking_param",
    ["utm_source", "utm_campaign", "fbclid", "gclid", "ref"],
)
def test_tracking_params_are_stripped_and_share_an_id_with_the_bare_url(tracking_param):
    registry = SourceRegistry()

    id_bare = registry.add("https://example.com/a")
    id_tracked = registry.add(f"https://example.com/a?{tracking_param}=xyz")

    assert id_bare == id_tracked
    assert len(registry.all()) == 1


def test_mixed_query_keeps_meaningful_key_and_drops_only_the_tracking_key():
    registry = SourceRegistry()

    id_bare = registry.add("https://example.com/a?id=7")
    id_mixed = registry.add("https://example.com/a?id=7&utm_source=x")

    assert id_bare == id_mixed
    assert normalize_url("https://example.com/a?id=7&utm_source=x") == "https://example.com/a?id=7"


def test_surviving_query_keys_preserve_original_order():
    assert (
        normalize_url("https://example.com/a?b=2&utm_source=x&a=1")
        == "https://example.com/a?b=2&a=1"
    )


@pytest.mark.parametrize(
    ("url_a", "url_b"),
    [
        ("https://arxiv.org/abs/2405.11111", "https://arxiv.org/abs/2405.22222"),
        ("https://example.com/abs/123", "https://example.com/pdf/123"),
        ("https://example.com/a?refid=3", "https://example.com/a"),
    ],
)
def test_urls_that_must_not_collapse_stay_distinct(url_a, url_b):
    registry = SourceRegistry()

    id_a = registry.add(url_a)
    id_b = registry.add(url_b)

    assert id_a != id_b
    assert len(registry.all()) == 2
