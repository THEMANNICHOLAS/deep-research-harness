"""Assign stable per-run source IDs and resolve `[Sn]` markers into markdown links.

The mechanical half of the citation scheme: minting IDs, deduplicating equivalent URLs, and
rewriting markers into links. No model involvement, no fetching (D6).
"""

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

MARKER_RE = re.compile(r"\[S(\d+)\]")

_DEFAULT_PORTS = {"http": 80, "https": 443}

# R5's recording seam: "unread" until content actually reaches the lead, "digested" when a
# reader delegation returned a non-empty digest of the page — promoted at the task boundary
# by agent.py's `_ReaderDigestMiddleware` from the candidates fetch.py nominates via
# `note_digest_candidate`, never at fetch time — and "fallback" when fetch_raw's raw-capture
# recovery path succeeded (written by fallback.py's tool closure, which never downgrades an
# existing "digested").
ReadMode = Literal["unread", "digested", "fallback"]

_PENDING_DIGESTS: ContextVar[list[str] | None] = ContextVar("pending_digests", default=None)


def note_digest_candidate(source_id: str) -> None:
    """Record that the reader captured `source_id` during the current delegation, if any.

    No-op outside a `pending_digest_scope` (e.g. a directly-invoked fetch tool in tests):
    marking `digested` is the delegation boundary's job, not the fetch's.
    """
    pending = _PENDING_DIGESTS.get()
    if pending is not None:
        pending.append(source_id)


@contextmanager
def pending_digest_scope() -> Iterator[list[str]]:
    """Collect the source IDs the reader captures during one `task` attempt.

    Context-local, so two concurrent delegations cannot claim each other's fetches. The
    caller (agent.py's `_ReaderDigestMiddleware`) promotes the collected IDs to `digested`
    only when the attempt actually returns a non-empty digest — a crashed or empty delegation
    leaves them "unread" (R5: the report must not claim a digest the lead never received).
    """
    pending: list[str] = []
    token = _PENDING_DIGESTS.set(pending)
    try:
        yield pending
    finally:
        _PENDING_DIGESTS.reset(token)


def marker_ids(text: str) -> list[str]:
    """Every `Sn` ID referenced by `text`, deduplicated, in first-appearance order.

    Pure regex scan, no registry lookup, shared by `SourceRegistry.unresolved_ids` (filtered to
    unknown IDs) and `harness.verify` (every marker, known or not), so the ordering loop lives
    in one place.
    """
    seen: list[str] = []
    for match in MARKER_RE.finditer(text):
        source_id = f"S{match.group(1)}"
        if source_id not in seen:
            seen.append(source_id)
    return seen


def normalize_url(url: str) -> str:
    """Return a canonical form of `url` so equivalent URLs share one identity.

    Collapses what does not change the fetch: scheme/host case, trailing slash, default port,
    fragment. Everything else is verbatim, including the query — two URLs differing only by
    query are different sources.

    Total by design: an unparseable URL is its own canonical form, never an exception, because
    these URLs are model-supplied and R2 forbids one bad URL failing the batch.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        # `urlsplit` rejects an unterminated IPv6 literal, `.port` a bad port number.
        # Neither is worth guessing a repair for.
        return url

    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower() if parts.hostname else ""
    if ":" in hostname:
        # `.hostname` strips the brackets an IPv6 literal needs to stay parseable.
        hostname = f"[{hostname}]"

    userinfo = ""
    if parts.username or parts.password:
        # An empty-but-present username (`http://:pw@host`) must still keep its
        # password — dropping it would conflate URLs differing only by credential.
        userinfo = parts.username or ""
        if parts.password:
            userinfo += f":{parts.password}"
        userinfo += "@"

    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    netloc = f"{userinfo}{hostname}" + (f":{port}" if port is not None else "")

    path = parts.path.rstrip("/")

    return urlunsplit((scheme, netloc, path, parts.query, ""))


class Source(BaseModel):
    """A single cited source, identified by a stable per-run `Sn` ID."""

    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    title: str | None = None
    read_mode: ReadMode = "unread"


class SourceRegistry:
    """Mints per-run `S1..Sn` IDs for URLs and resolves `[Sn]` markers into links."""

    def __init__(self, run_id: str | None = None) -> None:
        # Names this run's whole workspace subdirectory (`harness.config.run_workspace_dir`) —
        # notes, captures and evicted history all hang off it. `[Sn]` IDs are per-run but
        # `agent.workspace_dir` is shared, so without this a shorter run would read a previous
        # run's `S1.md`. The timestamp alone is second-resolution, so two runs launched in the
        # same second shared a directory and overwrote each other's captures mid-run; the
        # random suffix makes the default collision-free while keeping the stamp sortable.
        self.run_id = run_id or (
            f"{datetime.now().strftime('%Y-%m-%d-%H%M%S')}-{secrets.token_hex(4)}"
        )
        self._by_url: dict[str, Source] = {}
        self._by_id: dict[str, Source] = {}

    def add(self, url: str, title: str | None = None) -> str:
        """Register `url` and return its ID; the same normalized URL is never added twice.

        First write wins — an already-registered URL keeps its existing title.
        """
        normalized = normalize_url(url)
        existing = self._by_url.get(normalized)
        if existing is not None:
            return existing.id

        source_id = f"S{len(self._by_id) + 1}"
        source = Source(id=source_id, url=normalized, title=title)
        self._by_url[normalized] = source
        self._by_id[source_id] = source
        return source_id

    def get(self, source_id: str) -> Source | None:
        return self._by_id.get(source_id)

    def mark_read(self, source_id: str, mode: ReadMode) -> None:
        """Record how `source_id` was captured. Last write wins if called more than once.

        `source_id` is expected to already be registered — an unknown id raises `KeyError`,
        which is fine because every caller is an internal tool closure that just minted it.
        """
        self._by_id[source_id].read_mode = mode

    def all(self) -> list[Source]:
        """Return every registered source, in insertion order."""
        return list(self._by_id.values())

    def link(self, source_id: str) -> str:
        """Render `source_id` as a `[domain](url)` link; `KeyError` if unregistered."""
        source = self._by_id.get(source_id)
        if source is None:
            raise KeyError(f"unknown source id {source_id!r}")

        try:
            label = urlsplit(source.url).hostname or source.url
        except ValueError:
            # `add` stored a URL `normalize_url` could not parse, so it fails here too;
            # the raw URL is the only label available.
            label = source.url
        return f"[{label}]({source.url})"

    def resolve(self, text: str) -> str:
        """Replace every known `[Sn]` marker in `text` with its markdown link.

        Unknown markers are left untouched. `re.sub` does not re-scan replacements, so
        brackets inside a rendered link are never re-matched.
        """

        def _replace(match: re.Match[str]) -> str:
            source_id = f"S{match.group(1)}"
            if source_id in self._by_id:
                return self.link(source_id)
            return match.group(0)

        return MARKER_RE.sub(_replace, text)

    def unresolved_ids(self, text: str) -> list[str]:
        """Return every `[Sn]`-shaped marker in `text` with no registry entry.

        IDs are returned bare (e.g. `"S9"`), deduplicated, in first-appearance order.
        """
        return [source_id for source_id in marker_ids(text) if source_id not in self._by_id]
