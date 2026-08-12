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
from typing import Literal

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.models import build_chat_model
from harness.prompts import render
from harness.sources import SourceRegistry, marker_ids
from harness.tools.fetch import FETCH_FAILED_PREFIX, _sources_dir

Verdict = Literal["supported", "unsupported", "uncited", "unresolved", "unverifiable"]

_MODEL_VERDICTS = {"supported", "unsupported"}

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


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


def extract_claims(answer: str) -> list[str]:
    """Split `answer` into sentence-shaped claims, offline and deterministic.

    A claim is a SENTENCE (plan `## Discoveries` 2026-08-12). A returned sentence is NOT
    guaranteed to be a verbatim substring of `answer` — a block's lines are joined with a
    single space, so a hard-wrapped or bulleted claim collapses whitespace that the
    original answer did not have. `report.py` locates it back whitespace-tolerantly (3F
    fix pass, Major finding), not by a literal substring match.
    """
    # 1. Strip fenced code blocks entirely — code is not a claim.
    without_code = re.sub(r"```.*?```", "", answer, flags=re.DOTALL)

    claims: list[str] = []
    for block in re.split(r"\n\s*\n", without_code):
        block = block.strip("\n")
        if not block.strip():
            continue
        # 3. Drop blocks that are markdown headings.
        if re.match(r"^#{1,6}\s", block.strip()):
            continue

        lines = block.split("\n")
        is_list = all(_LIST_ITEM_RE.match(line) for line in lines if line.strip())

        units: list[str] = []
        if is_list:
            for line in lines:
                if not line.strip():
                    continue
                units.append(_LIST_ITEM_RE.sub("", line, count=1))
        else:
            joined = " ".join(line.strip() for line in lines if line.strip())
            if joined:
                units.append(joined)

        for unit in units:
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

    for claim in claims if claims is not None else extract_claims(answer):
        ids = marker_ids(claim)
        if not ids:
            checks.append(ClaimCheck(claim=claim, source_id=None, verdict="uncited"))
            continue

        for source_id in ids:
            source = registry.get(source_id)
            if source is None:
                checks.append(ClaimCheck(claim=claim, source_id=source_id, verdict="unresolved"))
                continue

            path = sources_dir / f"{source_id}.md"
            try:
                source_text = path.read_text(encoding="utf-8")
            except OSError:
                checks.append(
                    ClaimCheck(
                        claim=claim,
                        source_id=source_id,
                        verdict="unverifiable",
                        detail=f"no captured content exists for {source_id}",
                    )
                )
                continue

            first_line = source_text.split("\n", 1)[0]
            if first_line.startswith(FETCH_FAILED_PREFIX):
                checks.append(
                    ClaimCheck(
                        claim=claim,
                        source_id=source_id,
                        verdict="unverifiable",
                        detail=first_line,
                    )
                )
                continue

            try:
                rendered = render(
                    "verify", claim=claim, source_id=source_id, source_text=source_text
                )
                reply = await model.ainvoke([HumanMessage(content=rendered)])
                verdict, detail = _parse_reply(str(reply.content))
            except Exception as exc:  # noqa: BLE001 — one failed check must not fail the pass
                checks.append(
                    ClaimCheck(
                        claim=claim,
                        source_id=source_id,
                        verdict="unverifiable",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                check_failures.append(f"{source_id}: {type(exc).__name__}: {exc}")
                continue

            checks.append(
                ClaimCheck(claim=claim, source_id=source_id, verdict=verdict, detail=detail)
            )

    conflicts: list[Conflict] = []
    by_claim: dict[str, list[ClaimCheck]] = {}
    for check in checks:
        by_claim.setdefault(check.claim, []).append(check)

    for claim, claim_checks in by_claim.items():
        positions = [c for c in claim_checks if c.verdict in _MODEL_VERDICTS]
        verdicts_seen = {c.verdict for c in positions}
        if "supported" in verdicts_seen and "unsupported" in verdicts_seen:
            conflicts.append(Conflict(claim=claim, positions=positions))

    return VerificationResult(checks=checks, conflicts=conflicts, check_failures=check_failures)
