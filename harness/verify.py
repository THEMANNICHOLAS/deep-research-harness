"""Sequential, per-claim verification of a finished answer against its cited sources.

One model call per (sentence x cited source), each seeing only that source's captured
text — the focused-context shape D3 chose over a verifier agent that could see everything
at once and be tempted to adjudicate between sources. Reads only captured files under
`harness.tools.fetch._sources_dir`, never refetches (D10/R8): by the time this runs the
agent loop is finished and the wall clock has stopped (R7), so no network call belongs
here at all.

Calls are made ONE AT A TIME, in document order (D4) — see `verify_claims`.
"""

import json
import re
from pathlib import Path
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.models import build_chat_model
from harness.prompts import render
from harness.sources import SourceRegistry, marker_ids
from harness.tools.fetch import _sources_dir, is_failed_capture

Verdict = Literal[
    "supported", "unsupported", "not_addressed", "uncited", "unresolved", "unverifiable"
]

# `unsupported` means the source CONTRADICTS the claim; `not_addressed` means it is silent
# on it. Collapsing the two (the Phase 6 live check found them collapsed) made every
# synthesized sentence — which cites several sources, each covering part of it — look both
# unsupported and disputed, because a source that said nothing was read as disagreeing.
_MODEL_VERDICTS = {"supported", "unsupported", "not_addressed"}

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•–]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^#{1,6}\s")


class ClaimCheck(BaseModel):
    """One claim's outcome against one source (or the absence of one)."""

    model_config = ConfigDict(extra="forbid")

    claim: str
    source_id: str | None = None
    verdict: Verdict
    detail: str | None = None


class Conflict(BaseModel):
    """One claim where cited sources disagree. Nothing here ranks or adjudicates (D3)."""

    model_config = ConfigDict(extra="forbid")

    claim: str
    positions: list[ClaimCheck]


class VerificationResult(BaseModel):
    """The full outcome of one verification pass over one answer."""

    model_config = ConfigDict(extra="forbid")

    checks: list[ClaimCheck] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    check_failures: list[str] = Field(default_factory=list)


class VerifyError(Exception):
    """Raised for a verification-pass-level failure (this module's one `<Domain>Error`)."""


def _block_units(lines: list[str]) -> list[str]:
    """Split one block's non-blank lines into claim-sized units, line by line.

    Decided PER LINE, never per block (PR #4 review, Blocker). The previous rule —
    "every line in the block is a bullet, or the whole block is one unit" — merged a
    heading or a `Key findings:` lead-in into the first bullet, and merged a whole
    unpunctuated bullet list into ONE unit carrying several `[Sn]` markers. That unit
    was then checked against each cited source in turn, so every source was asked to
    support some other source's fact: `verify_claims` returned mixed verdicts, and
    `report.py` rendered a "the cited sources disagree" section between sources that
    never disagreed. A lead-in above a list is ordinary markdown and needs no blank line,
    so it was the common case, not the exotic one.

    Four line kinds:
    - a markdown heading — dropped, and only the LINE is dropped. Dropping the whole
      block (the previous behavior) silently discarded every claim under a `## Findings`
      heading that carried no blank line beneath it — never checked, never disclosed.
    - a list lead-in (`Key findings:` immediately above a list item) — dropped: it
      introduces assertions rather than making one.
    - a list item — starts a new unit, marker stripped.
    - anything else — continues the open unit (a hard-wrapped line), or starts a prose
      unit when none is open.
    """
    units: list[str] = []
    open_unit = False
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()

        if _HEADING_RE.match(line):
            open_unit = False
            continue

        next_line = lines[index + 1] if index + 1 < len(lines) else ""
        if line.endswith(":") and _LIST_ITEM_RE.match(next_line):
            open_unit = False
            continue

        if _LIST_ITEM_RE.match(raw_line):
            units.append(_LIST_ITEM_RE.sub("", raw_line, count=1).strip())
            open_unit = True
        elif open_unit:
            units[-1] = f"{units[-1]} {line}"
        else:
            units.append(line)
            open_unit = True

    return units


def extract_claims(answer: str) -> list[str]:
    """Split `answer` into sentence-shaped claims, offline and deterministic.

    A claim is a SENTENCE (plan `## Discoveries` 2026-08-12) carrying, at most, the
    citations of the one assertion it makes — see `_block_units` for how a block's lines
    become units and why the split is per line.

    A returned sentence is NOT guaranteed to be a verbatim substring of `answer` — a
    wrapped line's continuation is joined with a single space, so a hard-wrapped claim
    collapses whitespace that the original answer did not have. `report.py` locates it
    back whitespace-tolerantly (3F fix pass, Major finding), not by a literal substring
    match.
    """
    # 1. Strip fenced code blocks entirely — code is not a claim.
    without_code = re.sub(r"```.*?```", "", answer, flags=re.DOTALL)

    claims: list[str] = []
    for block in re.split(r"\n\s*\n", without_code):
        block = block.strip("\n")
        if not block.strip():
            continue

        lines = [line for line in block.split("\n") if line.strip()]

        for unit in _block_units(lines):
            for sentence in re.split(r"(?<=[.!?])\s+", unit):
                sentence = sentence.strip()
                if not sentence:
                    continue
                if not re.search(r"[a-zA-Z0-9]", sentence):
                    continue
                claims.append(sentence)

    return claims


def _parse_reply(content: str) -> tuple[Verdict, str]:
    """Parse a model reply into `(verdict, detail)`, raising on anything malformed.

    The return type is the `Verdict` literal, not a bare `str`: the guard below rejects
    anything outside `_MODEL_VERDICTS` before returning, so by the time a value leaves
    this function it really is one of the two model-decidable verdicts.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json") :]
        text = text.strip()
    data = json.loads(text)
    verdict = data["verdict"]
    detail = data["detail"]
    if verdict not in _MODEL_VERDICTS:
        raise VerifyError(f"model returned an unknown verdict: {verdict!r}")
    if not isinstance(detail, str):
        raise VerifyError("model's 'detail' field is not a string")
    return verdict, detail


async def _check_one(
    claim: str,
    source_id: str,
    model: BaseChatModel,
    registry: SourceRegistry,
    sources_dir: Path,
) -> tuple[ClaimCheck, str | None]:
    """Check one claim against one source, reading only that source's captured file.

    Returns `(check, failure)` — `failure` is a `check_failures` line when the check
    could not be run at all, `None` otherwise. Extracted from `verify_claims`'s loop so
    the result can be memoized per `(claim, source_id)`.
    """
    source = registry.get(source_id)
    if source is None:
        return ClaimCheck(claim=claim, source_id=source_id, verdict="unresolved"), None

    path = sources_dir / f"{source_id}.md"
    # `UnicodeDecodeError` (a `ValueError`, not an `OSError`) alongside the missing-file
    # case: a capture whose write died mid-flush can end mid-character, and an
    # unreadable source is a claim we cannot settle, not a pass we should abandon.
    try:
        source_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (
            ClaimCheck(
                claim=claim,
                source_id=source_id,
                verdict="unverifiable",
                detail=f"no readable captured content exists for {source_id}",
            ),
            None,
        )

    if is_failed_capture(source_text):
        return (
            ClaimCheck(
                claim=claim,
                source_id=source_id,
                verdict="unverifiable",
                detail=source_text.split("\n", 1)[0],
            ),
            None,
        )

    try:
        rendered = render("verify", claim=claim, source_id=source_id, source_text=source_text)
        reply = await model.ainvoke([HumanMessage(content=rendered)])
        verdict, detail = _parse_reply(str(reply.content))
    except Exception as exc:  # noqa: BLE001 — one failed check must not fail the pass
        return (
            ClaimCheck(
                claim=claim,
                source_id=source_id,
                verdict="unverifiable",
                detail=f"{type(exc).__name__}: {exc}",
            ),
            f"{source_id}: {type(exc).__name__}: {exc}",
        )

    return ClaimCheck(claim=claim, source_id=source_id, verdict=verdict, detail=detail), None


async def verify_claims(
    answer: str,
    config: HarnessConfig,
    registry: SourceRegistry,
    claims: list[str] | None = None,
) -> VerificationResult:
    """Check every claim in `answer` against its cited source(s), one call at a time.

    Sequential only (D4) — a plain `for` loop with `await` inside. No `asyncio.gather`, no
    `TaskGroup`, no concurrency of any kind. One failed check never fails the whole pass:
    the loop always continues (same independent-per-item stance as `fetch.py`'s batch
    handling).

    `claims` lets a caller that already ran `extract_claims(answer)` (e.g. `__main__`'s
    progress line) pass the same list through instead of this function recomputing it
    (3F fix pass, simplification). Defaults to `None`, which computes it here exactly as
    before — every existing caller is unaffected.
    """
    model = build_chat_model(config, "head")
    sources_dir = _sources_dir(config, registry)

    checks: list[ClaimCheck] = []
    check_failures: list[str] = []
    # One check per (claim x source), even when the same sentence appears twice in the
    # answer (PR #4 review, Major). Re-checking a repeated sentence spent a second model
    # call on an already-settled pair, and — because the model is called with neither
    # `temperature` nor `seed` — the two calls could legitimately disagree, which
    # `by_claim` below then read as two SOURCES disagreeing. `_place_marker` only ever
    # marks a claim's first occurrence anyway, so a second identical check had nowhere
    # to render.
    checked: dict[tuple[str, str], ClaimCheck] = {}

    for claim in claims if claims is not None else extract_claims(answer):
        ids = marker_ids(claim)
        if not ids:
            checks.append(ClaimCheck(claim=claim, source_id=None, verdict="uncited"))
            continue

        for source_id in ids:
            if (claim, source_id) in checked:
                continue

            check, failure = await _check_one(claim, source_id, model, registry, sources_dir)
            checked[(claim, source_id)] = check
            checks.append(check)
            if failure is not None:
                check_failures.append(failure)

    conflicts: list[Conflict] = []
    by_claim: dict[str, list[ClaimCheck]] = {}
    for check in checks:
        by_claim.setdefault(check.claim, []).append(check)

    for claim, claim_checks in by_claim.items():
        positions = [c for c in claim_checks if c.verdict in _MODEL_VERDICTS]
        # Two DISTINCT sources, not merely two disagreeing verdicts (PR #4 review,
        # Major). `_conflicts_section` states "the cited sources disagree on this claim"
        # in the harness's own voice; asserting that on one source's behalf, to a reader
        # who cannot check, is the one thing D3 refused to do.
        if len({c.source_id for c in positions}) < 2:
            continue
        verdicts_seen = {c.verdict for c in positions}
        # A conflict needs one source that CONTRADICTS the claim, not merely one that
        # fails to establish it (Phase 6 live check). A silent source is still listed in
        # `positions` when a real conflict exists — the reader wants the whole picture —
        # but it can never be what triggers the section.
        if "supported" in verdicts_seen and "unsupported" in verdicts_seen:
            conflicts.append(Conflict(claim=claim, positions=positions))

    return VerificationResult(checks=checks, conflicts=conflicts, check_failures=check_failures)
