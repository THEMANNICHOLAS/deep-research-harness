"""Sequential, pooled-per-paragraph verification of a finished answer against its sources.

One model call per PARAGRAPH, seeing every one of that paragraph's usable cited sources
pooled together — the model judges the paragraph as a whole rather than one source at a
time (D3 reversed the earlier per-(sentence x source) isolation once contradiction
detection needed to compare sources against each other, not just each source against the
paragraph). Reads only captured files under `harness.tools.fetch._sources_dir`, never
refetches (D10/R8): by the time this runs the agent loop is finished and the wall clock has
stopped (R7), so no network call belongs here at all.

Calls are made ONE AT A TIME, in the input list's order — see `verify_paragraphs`.
"""

import json
from typing import Literal

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.models import build_chat_model
from harness.paragraphs import Paragraph
from harness.prompts import render
from harness.sources import SourceRegistry
from harness.tools.fetch import _sources_dir, is_failed_capture

Verdict = Literal[
    "supported", "partially_supported", "not_supported", "no_sources_cited", "not_verified"
]

# The last two are assigned deterministically by this module and are NEVER returned by the
# model — `no_sources_cited` when a paragraph cites nothing registered, `not_verified` when
# a check could not be run at all (no usable source, a malformed reply, an unknown verdict,
# or a raised exception). Public because `report.py` gates its bullet rollup on the same
# distinction, and this is the one place that line is drawn.
MODEL_VERDICTS = {"supported", "partially_supported", "not_supported"}


class ParagraphVerdict(BaseModel):
    """One paragraph's verification outcome."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    detail: str
    sources_conflict: bool = False
    unsupported_items: list[int] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """The full outcome of one verification pass over one answer's paragraphs."""

    model_config = ConfigDict(extra="forbid")

    verdicts: list[ParagraphVerdict] = Field(default_factory=list)
    check_failures: list[str] = Field(default_factory=list)


class VerifyError(Exception):
    """Raised for a verification-pass-level failure (this module's one `<Domain>Error`)."""


def _parse_reply(content: str) -> tuple[Verdict, str, bool, list[int]]:
    """Parse a model reply into `(verdict, detail, sources_conflict, unsupported_items)`.

    Raises `VerifyError` (or lets a `json.JSONDecodeError` propagate) on anything malformed
    — the caller treats both as a per-paragraph failure, never a pass-ending one.
    """
    # Take the substring from the first `{` to the last `}`, which absorbs every wrapper
    # risk #1 predicted at once: a markdown fence, prose before the object, and prose after
    # it. Unconditional on purpose — gating this on a leading `{` let a reply that OPENED
    # with the object and then trailed prose reach `json.loads` whole, where "Extra data"
    # turned a genuine verdict into `not_verified`. A reply with no `{` raises `ValueError`,
    # which the caller already treats as a per-paragraph failure.
    text = content[content.index("{") : content.rindex("}") + 1]
    data = json.loads(text)
    verdict = data["verdict"]
    detail = data["detail"]
    if verdict not in MODEL_VERDICTS:
        raise VerifyError(f"model returned an unknown verdict: {verdict!r}")
    if not isinstance(detail, str):
        raise VerifyError("model's 'detail' field is not a string")
    sources_conflict = bool(data.get("sources_conflict", False))
    raw_items = data.get("unsupported_items", [])
    unsupported_items = [
        item for item in raw_items if isinstance(item, int) and not isinstance(item, bool)
    ]
    return verdict, detail, sources_conflict, unsupported_items


async def verify_paragraphs(
    paragraphs: list[Paragraph], config: HarnessConfig, registry: SourceRegistry
) -> VerificationResult:
    """Check every paragraph against its cited source(s), one pooled call at a time.

    Sequential only (D4) — a plain `for` loop with `await` inside. No `asyncio.gather`, no
    `TaskGroup`, no concurrency of any kind. One failed check never fails the whole pass:
    the loop always continues (same independent-per-item stance as `fetch.py`'s batch
    handling).

    A paragraph that cites no REGISTERED source is `no_sources_cited` without a model call.
    A paragraph whose sources are all unreadable (missing capture, or a `FETCH FAILED`
    stub) is `not_verified` without a model call, naming which sources were skipped and
    why. Anything else pools every usable source's captured text into one prompt.
    """
    model = build_chat_model(config, "head")
    sources_dir = _sources_dir(config, registry)

    verdicts: list[ParagraphVerdict] = []
    check_failures: list[str] = []

    for paragraph in paragraphs:  # strictly sequential
        registered = [sid for sid in paragraph.source_ids if registry.get(sid) is not None]
        if not registered:
            verdicts.append(
                ParagraphVerdict(
                    verdict="no_sources_cited",
                    detail=(
                        "This paragraph does not cite any source, so there was nothing to "
                        "check it against."
                    ),
                    source_ids=[],
                    unsupported_items=[],
                )
            )
            continue

        pooled: list[tuple[str, str, str]] = []
        skipped: list[str] = []
        for sid in registered:
            source = registry.get(sid)
            assert source is not None  # guaranteed by `registered`'s filter above
            path = sources_dir / f"{sid}.md"
            # `UnicodeDecodeError` (a `ValueError`, not an `OSError`) alongside the
            # missing-file case: a capture whose write died mid-flush can end
            # mid-character, and an unreadable source is a paragraph we cannot settle,
            # not a pass we should abandon (PR #4 review, carried onto the pooled read).
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                skipped.append(f"{sid}: no readable captured content exists")
                continue
            if is_failed_capture(text):
                skipped.append(f"{sid}: {text.split(chr(10), 1)[0]}")
                continue
            pooled.append((sid, source.url, text))

        if not pooled:
            verdicts.append(
                ParagraphVerdict(
                    verdict="not_verified",
                    detail=(
                        "The cited sources could not be read, so this paragraph was not "
                        f"checked ({'; '.join(skipped)})."
                    ),
                    source_ids=[],
                    unsupported_items=[],
                )
            )
            continue

        pooled_ids = [sid for sid, _, _ in pooled]
        try:
            sources_block = "\n\n".join(f"[{sid}] {url}\n{text}" for sid, url, text in pooled)
            rendered = render("verify", paragraph=paragraph.text, sources=sources_block)
            reply = await model.ainvoke([HumanMessage(content=rendered)])
            verdict, detail, sources_conflict, unsupported_items = _parse_reply(str(reply.content))
            verdicts.append(
                ParagraphVerdict(
                    verdict=verdict,
                    detail=detail,
                    sources_conflict=sources_conflict,
                    unsupported_items=unsupported_items,
                    source_ids=pooled_ids,
                )
            )
        except Exception as exc:  # noqa: BLE001 — one failed check must not fail the pass
            verdicts.append(
                ParagraphVerdict(
                    verdict="not_verified",
                    detail=f"{type(exc).__name__}: {exc}",
                    source_ids=pooled_ids,
                    unsupported_items=[],
                )
            )
            check_failures.append(f"{type(exc).__name__}: {exc}")

    return VerificationResult(verdicts=verdicts, check_failures=check_failures)
