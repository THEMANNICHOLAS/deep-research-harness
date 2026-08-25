"""Assign stable per-run source IDs and resolve `[Sn]` markers into markdown links.

The mechanical half of the citation scheme: minting IDs, deduplicating equivalent URLs, and
rewriting markers into links. No model involvement, no fetching (D6). Also the home of the
captured-file policy (`sources_dir`): it used to live in `harness/tools/fetch.py`, but
`report.py`, `verify.py` and the test fixtures all need it, and importing it from there dragged
all of crawl4ai (~1.2s) into every pure-rendering module and every test session.

R5's identity model: a `source_id` is minted only for a successful (`fetched`) page, and only
a `fetched` page ever gets a captures-dir file (`harness/tools/fetch.py`'s `_write_source_file`).
So "a capture file exists" is now equivalent to "content is real page text" with no further
convention needed — there is no more failure-stub shape for `report.py`/`verify.py` to detect.
"""

import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict

from harness.config import HarnessConfig, run_workspace_dir
from harness.guard import strip_invisibles

MARKER_RE = re.compile(r"\[S(\d+)\]")


def sources_dir(config: HarnessConfig, registry: "SourceRegistry") -> Path:
    """The one place the `<workspace_dir>/<run_id>/sources` layout is built."""
    return run_workspace_dir(config, registry.run_id) / "sources"


_DEFAULT_PORTS = {"http": 80, "https": 443}

# Tracking keys stripped from every query string in `normalize_url`, regardless of host — a
# key equal to a member OR starting with `utm_` is dropped; everything else survives verbatim.
_TRACKING_PARAMS = {"fbclid", "gclid", "ref"}

_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org"}
# Canonical form is `/abs/<work>`: the abs/pdf/html variant segment, an optional version
# suffix, and a trailing `.pdf` all collapse. `<work>` is kept verbatim otherwise — old-style
# IDs like `cs/0112017` contain a slash, hence the non-greedy `.+?` rather than `[^/]+`.
_ARXIV_PATH_RE = re.compile(r"^/(?:abs|pdf|html)/(?P<work>.+?)(?:v\d+)?(?:\.pdf)?$")

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
    fragment, known tracking query params (`utm_*`, `fbclid`, `gclid`, `ref`), and arxiv's
    abs/pdf/html/version variants. A meaningful query otherwise still distinguishes sources.

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

    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        kept = [
            (key, value)
            for key, value in pairs
            if key not in _TRACKING_PARAMS and not key.startswith("utm_")
        ]
        # Re-encode unconditionally, not just when a param was dropped: a conditional
        # rebuild makes the canonical form depend on which branch ran, so the same page
        # with and without a tracking param normalized to `q=hello%20world` vs
        # `q=hello+world` and minted two IDs. `quote_via=quote` (not the `quote_plus`
        # default) keeps a space as `%20`, matching how an untouched query already reads.
        query = urlencode(kept, quote_via=quote)

    if hostname in _ARXIV_HOSTS:
        match = _ARXIV_PATH_RE.match(path)
        if match is not None:
            hostname = "arxiv.org"
            netloc = f"{userinfo}{hostname}" + (f":{port}" if port is not None else "")
            path = f"/abs/{match['work']}"

    return urlunsplit((scheme, netloc, path, query, ""))


# A comma ends the match: two URLs pasted back-to-back (`https://a.com,https://b.com`) are a
# far more common input than a URL with a literal comma, and the unsplit blob approved neither.
_URL_RE = re.compile(r"https?://[^\s,]+")
_TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"


def _strip_trailing_punctuation(url: str) -> str:
    """Strip sentence punctuation from the end of a matched URL, keeping balanced parens.

    Char-by-char rather than one `rstrip`: a `)` is kept whenever the URL still contains at
    least as many `(` — `https://en.wikipedia.org/wiki/Foo_(bar)` keeps its close-paren, while
    the wrapping paren of `(see https://a.com/x)` is stripped.
    """
    while url:
        last = url[-1]
        if last not in _TRAILING_PUNCTUATION:
            break
        if last == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def extract_urls(text: str) -> list[str]:
    """Return every http(s) URL found in `text`, trailing punctuation stripped.

    Phase 4's strict-provenance seam (D2/R2): a question's pasted "read this page" URL is the
    only other sanctioned way (besides a search result) a URL becomes fetchable, so `__main__`
    extracts here and `registry.approve`s each one at run start. Non-http(s) schemes
    (`javascript:`, `ftp:`) are never matched — approving one would widen fetchability beyond
    what `_fetch`'s crawler even attempts.
    """
    return [_strip_trailing_punctuation(match.group(0)) for match in _URL_RE.finditer(text)]


def names_a_different_document(url: str, other: str) -> bool:
    """Whether two URLs that share a canonical form still name different documents.

    Trailing slash, fragment, case and tracking params rewrite the same page's address, so
    collapsing them costs nothing. The arxiv rule is the one that merges genuinely different
    documents — `/abs/<work>` is an abstract, `/pdf/<work>.pdf` the full text — and a caller
    that asked for both gets only one. Path is the discriminator: everything else this
    function's callers merge leaves the path alone.
    """
    try:
        path = urlsplit(url).path.rstrip("/").lower()
        other_path = urlsplit(other).path.rstrip("/").lower()
    except ValueError:
        return False
    return path != other_path


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
        # Phase 4 strict provenance (R2): a URL is fetchable only once it lands here — from a
        # `search_web` result surviving Phase 3's guard, or a user-supplied URL `__main__`
        # approves at run start. Never from a page's own in-body links (D2).
        self._approved: set[str] = set()
        # Every URL whose fetch did not end `fetched` — policy rejection or genuine failure —
        # keeps its rendered verdict here for the rest of the run, so a re-request replays it
        # instead of re-crawling. One attempt per URL per run (D2).
        self._failed: dict[str, str] = {}

    def approve(self, url: str) -> None:
        """Mark `url` fetchable (Phase 4, R2) -- the only sanctioned way to widen fetchability.

        Approving a URL for the FIRST time also clears any verdict standing against it. A
        provenance rejection is the one verdict that can stop being true, and this call is what
        stops it: the URL was unfetchable only because nothing had approved it yet. Without
        this, a URL the model guessed at from memory (rejected) and then legitimately found via
        `search_web` (`_approve_survivors` calls straight through to here) would replay its
        rejection forever and be lost for the run.

        Every other verdict is recorded downstream of `_fetch`'s provenance check and so only
        ever for an ALREADY-approved URL, which this branch cannot reach — guard blocks and
        genuine failures stay sticky for the whole run, as D2 requires. Phase 3's blocklist
        rejections are self-healing either way: a cleared one is re-rejected by the next
        pre-crawl blocklist check.
        """
        normalized = normalize_url(url)
        if normalized not in self._approved:
            self._failed.pop(normalized, None)
        self._approved.add(normalized)

    def is_approved(self, url: str) -> bool:
        """Whether `url` (any `normalize_url`-equivalent spelling) has been approved."""
        return normalize_url(url) in self._approved

    def record_failure(self, url: str, rendered_block: str) -> None:
        """Store `rendered_block` as `url`'s verdict for the rest of the run (D2).

        First write wins, mirroring `add`: the block that answered the URL the first time is the
        one every re-request replays.
        """
        self._failed.setdefault(normalize_url(url), rendered_block)

    def failed_block(self, url: str) -> str | None:
        """`url`'s stored verdict block, or `None` if it has not failed this run."""
        return self._failed.get(normalize_url(url))

    def add(self, url: str, title: str | None = None) -> str:
        """Register `url` and return its ID; the same normalized URL is never added twice.

        First write wins — an already-registered URL keeps its existing title. `title` is run
        through `strip_invisibles` (Phase 5, R3 hygiene) before storage: a page's own title is
        untrusted content like anything else it carries.
        """
        normalized = normalize_url(url)
        existing = self._by_url.get(normalized)
        if existing is not None:
            return existing.id

        source_id = f"S{len(self._by_id) + 1}"
        clean_title = strip_invisibles(title) if title is not None else None
        source = Source(id=source_id, url=normalized, title=clean_title)
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

    def count(self) -> int:
        """How many sources are registered -- one per `[Sn]` minted (R5's live counter).

        Cheaper than `len(self.all())`, which materializes a list on every poll.
        """
        return len(self._by_id)

    def link(self, source_id: str) -> str:
        """Render `source_id` as a `[domain](url)` link; `KeyError` if unregistered.

        Emits a markdown link ONLY when the URL's scheme is http/https (Phase 5, R3): any other
        scheme (`javascript:`, `data:`, ...) renders as plain text instead, so a hostile title or
        URL can never become a clickable non-http(s) action in the report. A URL `normalize_url`
        itself could not parse (`ValueError` from `urlsplit`) has no scheme to check either way
        and still renders as a link with itself as the label, matching `add`'s own tolerance for
        unparseable input.
        """
        source = self._by_id.get(source_id)
        if source is None:
            raise KeyError(f"unknown source id {source_id!r}")

        try:
            parts = urlsplit(source.url)
        except ValueError:
            return f"[{source.url}]({source.url})"

        if parts.scheme.lower() not in ("http", "https"):
            return source.url

        label = parts.hostname or source.url
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
