"""Assign stable per-run source IDs and resolve `[Sn]` markers into markdown links.

Citations in synthesized answers reference sources by a short `[Sn]` marker rather than
a raw URL, so the model's output stays readable. This module is the purely mechanical
half of that scheme: minting IDs, deduplicating equivalent URLs, and rewriting markers
into clickable links. No model involvement, no fetching — see D6 for why retrieval is a
separate, later concern.
"""

import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

MARKER_RE = re.compile(r"\[S(\d+)\]")

_DEFAULT_PORTS = {"http": 80, "https": 443}


def marker_ids(text: str) -> list[str]:
    """Every `Sn` ID referenced by `text`, deduplicated, in first-appearance order.

    Pure regex scan, no registry lookup — shared by `SourceRegistry.unresolved_ids`
    (filtered to unknown IDs) and `harness.verify` (which needs every marker a claim
    carries, known or not), so the dedupe-in-first-appearance-order loop lives in exactly
    one place (3F fix pass, Minor finding).
    """
    seen: list[str] = []
    for match in MARKER_RE.finditer(text):
        source_id = f"S{match.group(1)}"
        if source_id not in seen:
            seen.append(source_id)
    return seen


def normalize_url(url: str) -> str:
    """Return a canonical form of `url` so equivalent URLs share one identity.

    Collapses differences that don't change what's fetched: scheme/host case, a
    trailing slash on the path, an explicit default port, and any fragment. Preserves
    everything else verbatim, including the query string — two URLs differing only by
    query are different sources, not duplicates.

    Total by design: a URL too malformed to parse is its own canonical form rather than
    an exception. The fetch tool registers model-supplied URLs, and R2 forbids one bad
    URL from failing the batch.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        # `urlsplit` rejects an unterminated IPv6 literal; `.port` rejects a
        # non-numeric or out-of-range port. Neither is worth guessing a repair for.
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


class SourceRegistry:
    """Mints per-run `S1..Sn` IDs for URLs and resolves `[Sn]` markers into links."""

    def __init__(self, run_id: str | None = None) -> None:
        # Names this run's captured-sources directory (`sources/<run_id>/`, built by
        # `harness.tools.fetch._sources_dir`). `[Sn]` IDs are per-run, but
        # `agent.workspace_dir` is one shared directory reused by every run, so without
        # this a shorter run would read a previous run's `S1.md` (see the plan's
        # `## Reconciliations` 2026-08-12 — Phase 6). The default is a fresh timestamp
        # (never a shared fallback), so an omitted `run_id` still cannot collide.
        self.run_id = run_id or datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self._by_url: dict[str, Source] = {}
        self._by_id: dict[str, Source] = {}

    def add(self, url: str, title: str | None = None) -> str:
        """Register `url`, returning its ID. The same normalized URL is never added twice.

        First write wins: if the URL is already registered, its existing title is kept
        even if a different `title` is passed here.
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
        """Look up a source by ID, or `None` if it isn't registered."""
        return self._by_id.get(source_id)

    def all(self) -> list[Source]:
        """Return every registered source, in insertion order."""
        return list(self._by_id.values())

    def link(self, source_id: str) -> str:
        """Render `source_id` as a `[domain](url)` markdown link.

        Raises `KeyError` if `source_id` isn't registered.
        """
        source = self._by_id.get(source_id)
        if source is None:
            raise KeyError(f"unknown source id {source_id!r}")

        try:
            label = urlsplit(source.url).hostname or source.url
        except ValueError:
            # `add` stores a URL `normalize_url` could not parse verbatim, so the
            # same ValueError surfaces here; the raw URL is the only label there is.
            label = source.url
        return f"[{label}]({source.url})"

    def resolve(self, text: str) -> str:
        """Replace every known `[Sn]` marker in `text` with its markdown link.

        Unknown markers are left untouched. Note `re.sub` does not re-scan its
        replacements, so the brackets inside a rendered link are never re-matched.
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
