"""Behavioral tests for harness.blocklist."""

import json
import os
from datetime import UTC, datetime, timedelta

from harness import blocklist

_NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC)
_TTL_DAYS = 30


def _write_raw(path: str, data: object) -> None:
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f)


def test_a_recorded_host_is_present_an_unrecorded_one_is_not(tmp_path):
    path = str(tmp_path / "blocklist.json")
    blocklist.record(path, "blocked.test", _TTL_DAYS, now=_NOW)

    entries = blocklist.load(path, _TTL_DAYS, now=_NOW)

    assert "blocked.test" in entries
    assert "other.test" not in entries


def test_entries_older_than_the_ttl_are_pruned_entries_inside_survive(tmp_path):
    path = str(tmp_path / "blocklist.json")
    cutoff = _NOW - timedelta(days=_TTL_DAYS)
    _write_raw(
        path,
        {
            # One second past the cutoff (older than the TTL) — must be pruned.
            "expired.test": (cutoff - timedelta(seconds=1)).isoformat(),
            # Exactly at the cutoff — the boundary is inclusive, must survive.
            "at-boundary.test": cutoff.isoformat(),
            # One second inside the TTL — must survive.
            "fresh.test": (cutoff + timedelta(seconds=1)).isoformat(),
        },
    )

    entries = blocklist.load(path, _TTL_DAYS, now=_NOW)

    assert "expired.test" not in entries
    assert "at-boundary.test" in entries
    assert "fresh.test" in entries


def test_a_missing_file_loads_as_empty_rather_than_raising(tmp_path):
    path = str(tmp_path / "does-not-exist.json")

    entries = blocklist.load(path, _TTL_DAYS, now=_NOW)

    assert entries == {}


def test_invalid_json_loads_as_empty_rather_than_raising(tmp_path):
    path = str(tmp_path / "blocklist.json")
    _write_raw(path, "{not valid json")

    entries = blocklist.load(path, _TTL_DAYS, now=_NOW)

    assert entries == {}


def test_a_non_object_top_level_loads_as_empty_rather_than_raising(tmp_path):
    path = str(tmp_path / "blocklist.json")
    _write_raw(path, ["a.test", "b.test"])

    entries = blocklist.load(path, _TTL_DAYS, now=_NOW)

    assert entries == {}


def test_an_entry_with_an_unparseable_timestamp_is_dropped_the_rest_survive(tmp_path):
    path = str(tmp_path / "blocklist.json")
    _write_raw(
        path,
        {
            "bad.test": "not-a-timestamp",
            "also-bad.test": 12345,
            "good.test": _NOW.isoformat(),
        },
    )

    entries = blocklist.load(path, _TTL_DAYS, now=_NOW)

    assert "bad.test" not in entries
    assert "also-bad.test" not in entries
    assert "good.test" in entries


def test_a_recorded_write_leaves_a_complete_parseable_file_and_no_leftover_temp_file(tmp_path):
    path = str(tmp_path / "blocklist.json")

    blocklist.record(path, "blocked.test", _TTL_DAYS, now=_NOW)

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    assert raw == {"blocked.test": _NOW.isoformat()}
    assert os.listdir(tmp_path) == ["blocklist.json"]


def test_recording_into_a_missing_parent_directory_works(tmp_path):
    # `record` creates the parent directory rather than failing softly — the blocklist
    # path lives under a workspace directory that may not exist on first run.
    path = str(tmp_path / "nested" / "dir" / "blocklist.json")

    blocklist.record(path, "blocked.test", _TTL_DAYS, now=_NOW)

    entries = blocklist.load(path, _TTL_DAYS, now=_NOW)
    assert "blocked.test" in entries


def test_record_preserves_other_hosts_and_overwrites_its_own(tmp_path):
    path = str(tmp_path / "blocklist.json")
    blocklist.record(path, "first.test", _TTL_DAYS, now=_NOW)
    later = _NOW + timedelta(days=1)

    blocklist.record(path, "first.test", _TTL_DAYS, now=later)
    blocklist.record(path, "second.test", _TTL_DAYS, now=later)

    entries = blocklist.load(path, _TTL_DAYS, now=later)
    assert entries["first.test"] == later
    assert entries["second.test"] == later


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing(tmp_path):
    # The module docstring and the config comment both advertise this file as hand-editable,
    # so a human-written "2026-08-17T09:30:00" (no offset) is expected input. It parses fine
    # but compares as offset-naive, which would raise TypeError against the aware cutoff and
    # sink every subsequent fetch_pages call.
    path = str(tmp_path / "blocklist.json")
    _write_raw(path, {"naive.test": "2026-08-17T09:30:00", "bare.test": "2026-08-17"})

    entries = blocklist.load(path, _TTL_DAYS, now=_NOW)

    assert entries["naive.test"].tzinfo is not None
    assert "bare.test" in entries


def test_record_never_raises_when_the_path_cannot_be_written(tmp_path):
    # The blocklist is regenerable (D4), and `record` runs after the gather — so a full disk
    # or a read-only workspace must not discard a batch of already-successful fetches.
    # `load` already swallows OSError; `record` has to be symmetric.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("i am a file, not a directory")
    path = str(blocker / "blocklist.json")

    blocklist.record(path, "blocked.test", _TTL_DAYS, now=_NOW)

    assert not os.path.exists(path)


def test_record_drops_expired_entries_from_the_file(tmp_path):
    # Pruning is in-memory on load (D4), but rewriting expired entries forever means the file
    # grows without bound and shows an operator stale hosts that are not actually in force.
    path = str(tmp_path / "blocklist.json")
    stale = (_NOW - timedelta(days=_TTL_DAYS + 1)).isoformat()
    _write_raw(path, {"stale.test": stale, "fresh.test": _NOW.isoformat()})

    blocklist.record(path, "new.test", _TTL_DAYS, now=_NOW)

    with open(path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert set(on_disk) == {"fresh.test", "new.test"}
