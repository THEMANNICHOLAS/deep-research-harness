"""Persistent, TTL-pruned JSON blocklist of hosts that answered 403/401 (D4).

A JSON dict keyed by lowercased host, not SQLite: the harness has no other datastore and
the no-DB constraint (see CLAUDE.md) rules one in for a single small map. Hand-editable
JSON over a binary format, so an operator can inspect or prune it without tooling — the
file is entirely regenerable, re-learned from the next 403/401 if deleted. Concurrent
writers are last-write-wins by design: there is no cross-process locking, and a lost entry
is benign since it is simply re-recorded on the next block. Every timestamp is
timezone-aware UTC; `load` takes an injectable clock so TTL pruning is testable without the
wall clock.
"""

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta


def _read_raw(path: str) -> dict[str, object]:
    """Read the on-disk map verbatim, degrading to `{}` on any read/parse failure."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        # ValueError, not just json.JSONDecodeError: a hand-edited file re-saved in a
        # non-UTF-8 encoding raises UnicodeDecodeError (a ValueError subclass) from the
        # decode, and the "never raises" contract covers that corruption too.
        return {}
    return raw if isinstance(raw, dict) else {}


def load(path: str, ttl_days: int, now: datetime | None = None) -> dict[str, datetime]:
    """Load unexpired blocklist entries, pruning anything older than `ttl_days` in memory.

    Never raises: a missing file, unreadable file, invalid JSON, a non-object top level, or
    an entry with a non-string or unparseable timestamp all degrade to dropping that entry
    (or the whole file) rather than raising.

    A naive timestamp is read as UTC rather than dropped. `datetime.fromisoformat` accepts
    an offset-less `"2026-08-17T09:30:00"` — and a bare `"2026-08-17"` — which a hand-editing
    operator will plausibly write, and comparing one against the aware cutoff would raise
    `TypeError` and sink every subsequent fetch. UTC is the format this file documents, so
    assuming it is the honest reading.
    """
    if now is None:
        now = datetime.now(UTC)
    cutoff = now - timedelta(days=ttl_days)

    entries: dict[str, datetime] = {}
    for host, timestamp in _read_raw(path).items():
        if not isinstance(timestamp, str):
            continue
        try:
            recorded = datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=UTC)
        if recorded >= cutoff:
            entries[host] = recorded
    return entries


def record(path: str, host: str, ttl_days: int, now: datetime | None = None) -> None:
    """Record `host` as blocked at `now` (default: current UTC time).

    Merges into the CURRENTLY-IN-FORCE entries — `load`'s pruned view, not the raw file — so
    expired hosts leave the file instead of being rewritten forever. Without that the file
    grows without bound and shows an operator stale hosts that are no longer in effect.

    Writes go through a temp file in the same directory followed by `os.replace`, which is
    atomic on both POSIX and Windows for same-volume replaces — a reader never observes a
    partial file. The parent directory is created if missing, since the blocklist path may
    not exist on a first run.

    Never raises. `record` runs after a batch of fetches has already succeeded, and the
    blocklist is entirely regenerable (D4) — so a read-only workspace or a full disk must
    degrade to "this host is re-learned on the next 403" rather than discarding the batch.
    This mirrors the degrade `load` already applies to the same `OSError`s.
    """
    if now is None:
        now = datetime.now(UTC)

    entries = {h: t.isoformat() for h, t in load(path, ttl_days, now=now).items()}
    entries[host] = now.isoformat()

    directory = os.path.dirname(path) or "."
    tmp_path: str | None = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".blocklist-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp_path, path)
    except OSError:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)
