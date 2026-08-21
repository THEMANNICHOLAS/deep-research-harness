"""Cross-session domain blocklist (R3/R4, D3).

This is the project's FIRST cross-session persisted state: a plain, hand-editable JSON file
recording hostnames observed to deliberately refuse bots (401/403/challenge page), fed by
`harness/tools/fetch.py` and consulted as a backstop there and as the primary filter in
`harness/tools/search.py`. It lives here — a module, not a tool under `harness/tools/*` —
because cross-session persistence is a persistence concern, not a tool call the model makes.

Accepted race (D3, single-operator homelab): two concurrent runs can each read the file, add a
different hostname, and write back — read-merge-`os.replace` means the loser's write still
merges on-disk entries in, so at most one entry from the OTHER run is lost, and that entry
self-heals on the next refusal of the same host.

No imports from `harness.*`: this stays leaf-level so `harness/sources.py` and
`harness/tools/*` can import it without a cycle.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

_CHALLENGE_MARKERS = (
    "just a moment",
    "verifying you are human",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
)
# Deliberately NOT markers: "ray id" and "performance & security by cloudflare" appear on
# ordinary Cloudflare-fronted error pages (e.g. a plain 404), so they would blocklist hosts
# that merely served a normal error rather than a challenge/interstitial.


def hostname_of(url: str) -> str | None:
    """The lowercased hostname of `url`, or None if it has none / cannot be parsed.

    Total by design (same contract as `sources.py::normalize_url`): these URLs are
    model-supplied, so a malformed one must degrade to `None`, never raise.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    # `.hostname` is already bracket-stripped for IPv6 (`[::1]` -> `::1`) — that bare form is
    # exactly the blocklist key we want. `normalize_url` instead keeps the bracketed *netloc*
    # because it needs that shape for its own purposes (see the parent plan's Out of scope);
    # this is the one shared definition for callers that want a bare hostname.
    return parts.hostname.lower() if parts.hostname else None


def fires_challenge_marker(text: str) -> bool:
    """Whether `text` looks like an anti-bot interstitial rather than a page.

    Callers check this ONLY when the fetch outcome is `"blocked"` (risk #2 / the 2026-08-20
    reconciliation), not merely non-`"fetched"` — `non_html`, `error`, and `timeout` can all
    carry genuine extracted page text, so only `blocked` (refusal-shaped by construction) is
    safe to scan with no body-length threshold; real prose that happens to quote a marker
    phrase always classifies `fetched` (or a non-`blocked` failure) and never reaches this
    check.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


class Blocklist:
    """Hostnames observed to deliberately refuse bots, persisted across runs."""

    def __init__(self, path: Path, entries: dict[str, dict[str, object]]) -> None:
        self.path = path
        self.entries = entries

    def contains(self, hostname: str) -> bool:
        # Callers pass an already-lowercased hostname from `hostname_of`; lowercase
        # defensively anyway since a hand-edited file may carry mixed case. `_read_entries`
        # already lowercases every key it loads, so this holds regardless of how `entries`
        # was populated.
        return hostname.lower() in self.entries

    def add(self, hostname: str, reason: str) -> None:
        """Record `hostname` as blocklisted, unless already present.

        No-op if already present: `first_seen` and `reason` must survive — the operator's
        record of WHEN a host first walled us is the useful bit, not the most recent refusal.

        Otherwise: re-read the file from disk (another run may have added entries since this
        `Blocklist` was loaded), merge with on-disk entries winning where they exist, write to
        a sibling temp path, then `os.replace` onto `path` (atomic on POSIX).

        A write that raises `OSError` is swallowed and this returns without persisting: this is
        best-effort disclosure machinery, not evidence, and the caller (fetch) already renders
        and logs the URL's own failure regardless. The filesystem is a boundary (CLAUDE.md), so
        nothing else is swallowed here.
        """
        hostname = hostname.lower()
        if hostname in self.entries:
            return

        on_disk = _read_entries(self.path)
        # On-disk entries win where they exist — merge copies whole per-hostname dicts
        # verbatim rather than rebuilding them field by field, which is how an unknown
        # hand-edited key (e.g. a "note") survives this rewrite untouched.
        merged = dict(self.entries)
        merged.update(on_disk)
        if hostname not in merged:
            merged[hostname] = {
                "reason": reason,
                "first_seen": datetime.now(UTC).isoformat(),
            }

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Per-process-unique, not a fixed `.json.tmp`: two concurrent runs racing this
            # write could otherwise interleave writes to the SAME temp path, and `os.replace`
            # would then promote a truncated file — degrading (via `_read_entries`) to the
            # WHOLE blocklist vanishing, which is strictly worse than D3's accepted "read-merge-
            # replace loses at most one entry". A unique name keeps this process's rename atomic
            # regardless of what another concurrent writer is doing.
            tmp_path = self.path.parent / f"{self.path.name}.{os.getpid()}.tmp"
            tmp_path.write_text(
                json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            os.replace(tmp_path, self.path)
        except OSError:
            return

        self.entries = merged


def _read_entries(path: Path) -> dict[str, dict[str, object]]:
    """Read `path` into a hostname->entry dict, degrading to empty on any problem.

    Missing file, unreadable file, invalid JSON, or valid JSON that is not an object all
    degrade to empty rather than raising — a corrupt hand-edit should leave the run with
    "nothing blocked" rather than aborting it. No logging here (this module has no `RunLog`);
    the silence is deliberate.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    # Drop non-dict values (a hand-edited `"example.com": "403"` shorthand) rather than
    # letting them reach `contains`/`add`. Lowercase every key here, the one read site, so the
    # invariant holds for every consumer rather than being re-applied piecemeal (e.g. a
    # hand-edited "Walled.TEST" key still matches `contains("walled.test")`).
    return {
        hostname.lower(): entry
        for hostname, entry in data.items()
        if isinstance(hostname, str) and isinstance(entry, dict)
    }


def load_blocklist(path: Path) -> Blocklist:
    """Load the blocklist at `path`, or an empty one if it does not exist / is corrupt.

    A missing file is the normal first-run case: this NEVER creates the file — a read must
    not have a write side effect.
    """
    return Blocklist(path, _read_entries(path))


def resolve_blocklist(blocklist: Blocklist | None, path: Path) -> Blocklist:
    """The shared instance if one was passed, otherwise a fresh load from `path`.

    Mirrors `harness.runlog.or_default` (read its docstring, same policy): every
    `build_*_tool` factory and `_fetch`/`_search`/`_fetch_raw` was resolving `blocklist if
    blocklist is not None else load_blocklist(config.blocklist.path)` inline. One home, so a
    change to the policy is one edit, not five that can drift apart. Named `resolve_blocklist`,
    not `or_default` — every caller already imports `or_default` from `harness.runlog`, and a
    second same-named import would collide.
    """
    return blocklist if blocklist is not None else load_blocklist(path)
