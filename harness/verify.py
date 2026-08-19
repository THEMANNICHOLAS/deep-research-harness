"""Sequential, pooled-per-paragraph verification of a finished answer against its sources.

One model call per PARAGRAPH, with every usable source that paragraph cites pooled into it,
so the model judges the paragraph as a whole (D3 — contradiction detection needs the sources
compared against each other, not each one against the paragraph in isolation). Reads only
captured files under `harness.sources.sources_dir` and never refetches (D10/R8): the
agent loop is finished and the wall clock stopped (R7) by the time this runs.

Calls are made ONE AT A TIME, in the input list's order — see `verify_paragraphs`.
"""

import json
from collections.abc import Callable
from typing import Literal

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.guard import fence
from harness.paragraphs import Paragraph, renders_content, strip_markers
from harness.prompts import render
from harness.sources import SourceRegistry, sources_dir

Verdict = Literal[
    "supported", "partially_supported", "not_supported", "no_sources_cited", "not_verified"
]

# The other two are assigned by this module, never returned by the model: `no_sources_cited`
# when a paragraph cites nothing registered, `not_verified` when no check could run (no usable
# source, malformed reply, unknown verdict, raised exception). Public because `report.py` gates
# its bullet rollup on the same distinction, and this is where that line is drawn.
MODEL_VERDICTS = {"supported", "partially_supported", "not_supported"}

# The reader-facing `Verdict:` detail for a check that could not run. The exception text stays
# in `check_failures`, which `## Gaps and disclosures` prints, instead of reaching the answer.
CHECK_FAILED_DETAIL = "This paragraph could not be checked because the verification step failed."


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
    # A consolidated, prose reviewer-analysis paragraph over ALL per-paragraph verdicts
    # (Phase 2 Step 5). Additive: `None` means the consolidation pass never ran, was skipped
    # (zero verdicts), or failed — the per-paragraph verdicts above are unaffected either way.
    reviewer_summary: str | None = None


class VerifyError(Exception):
    """Raised for a verification-pass-level failure (this module's one `<Domain>Error`)."""


def _parse_reply(content: str) -> tuple[Verdict, str, bool, list[int]]:
    """Parse a model reply into `(verdict, detail, sources_conflict, unsupported_items)`.

    Raises `VerifyError` (or lets a `json.JSONDecodeError` propagate) on anything malformed —
    the caller treats both as a per-paragraph failure, never a pass-ending one.
    """
    # First `{` to last `}`, which absorbs every wrapper shape at once: a markdown fence,
    # prose before the object, prose after it. Unconditional on purpose — gating it on a
    # leading `{` let a reply that OPENED with the object and then trailed prose reach
    # `json.loads` whole, where "Extra data" turned a genuine verdict into `not_verified`. A
    # reply with no `{` raises `ValueError`, already handled as a per-paragraph failure.
    text = content[content.index("{") : content.rindex("}") + 1]
    data = json.loads(text)
    verdict = data["verdict"]
    detail = data["detail"]
    if verdict not in MODEL_VERDICTS:
        raise VerifyError(f"model returned an unknown verdict: {verdict!r}")
    if not isinstance(detail, str):
        raise VerifyError("model's 'detail' field is not a string")
    # A real boolean only, matching how `unsupported_items` drops non-ints: `bool("false")` is
    # True, so a quoted boolean would file the paragraph under `## Conflicting sources` against
    # its own reply. Anything else reads as "no conflict claimed" rather than failing the check.
    sources_conflict = data.get("sources_conflict", False) is True
    raw_items = data.get("unsupported_items", [])
    unsupported_items = [
        item for item in raw_items if isinstance(item, int) and not isinstance(item, bool)
    ]
    return verdict, detail, sources_conflict, unsupported_items


def _paragraph_excerpt(paragraph: Paragraph) -> str:
    """The opening words of `paragraph` as one flattened line, capped at 80 characters.

    Marker-stripped for prose (what the report's reader actually sees); verbatim for a fenced
    block, whose text the report renders untouched. Feeds `_format_verdicts_block` so the
    consolidator can QUOTE each claim it names — `## Answer` renders no paragraph numbers, so
    a number alone gives the reader nothing to search for.
    """
    text = paragraph.text if paragraph.is_code else strip_markers(paragraph.text)
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= 80 else flattened[:77] + "..."


def _format_verdicts_block(paragraphs: list[Paragraph], verdicts: list[ParagraphVerdict]) -> str:
    """One line per verdict, naming its paragraph number, opening words, verdict, detail, and
    cited sources.

    The consolidator must be able to NAME each not-fully-supported claim (the whole point of
    this step), so every field it would need to do that is here — not just the verdict value.
    The excerpt is what makes the name findable: `## Answer` renders paragraphs as plain
    prose with no numbers, so the reviewer paragraph quotes each claim's opening words and the
    reader searches for those.

    Numbered by `renders_content` (D1), NOT by raw list position: `report.py`'s `_answer_section`
    drops an empty-rendering paragraph's block entirely (a citation-only paragraph strips to no
    visible text), so counting it here would misattribute the reviewer's number to whatever
    paragraph follows it — the reader's only pointer to a claim now that no per-paragraph
    `Verdict:` line survives in `## Answer`.
    """
    lines = []
    number = 0
    for paragraph, verdict in zip(paragraphs, verdicts, strict=True):
        if not renders_content(paragraph):
            continue
        number += 1
        sources = ", ".join(verdict.source_ids) if verdict.source_ids else "none"
        lines.append(
            f'Paragraph {number} (starts: "{_paragraph_excerpt(paragraph)}"): '
            f"{verdict.verdict} - {verdict.detail} (sources: {sources})"
        )
    return "\n".join(lines)


async def verify_paragraphs(
    paragraphs: list[Paragraph],
    config: HarnessConfig,
    registry: SourceRegistry,
    on_paragraph: Callable[[int, int], None] | None = None,
) -> VerificationResult:
    """Check every paragraph against its cited source(s), one pooled call at a time.

    Sequential only (D4): a plain `for` loop with `await` inside, no `gather`, no `TaskGroup`.
    One failed check never fails the pass — the loop always continues, the same
    independent-per-item stance as `fetch.py`'s batch handling.

    `on_paragraph(index, total)` (1-based) fires as each paragraph's check begins — a callback
    rather than a renderer so this module stays display-free; `__main__` maps it to an
    `Activity` line. Each pooled model call can take minutes, so without this the whole pass
    is silent and indistinguishable from a hang at the terminal.

    A paragraph citing no REGISTERED source is `no_sources_cited` with no model call. One whose
    sources are all unreadable (missing capture or a `FETCH FAILED` stub) is `not_verified`
    with no model call, naming what was skipped. Anything else pools every usable source's
    captured text into one prompt.
    """
    # Deferred, and via the module: importing `harness.models` pulls in `openai` (~2s), which
    # would land on every CLI startup through `report.py`'s import of this module; the module
    # attribute lookup also keeps `harness.models.build_chat_model` the single patch target.
    from harness import models

    model = models.build_chat_model(config, "verifier")
    captures_dir = sources_dir(config, registry)

    verdicts: list[ParagraphVerdict] = []
    check_failures: list[str] = []

    total = len(paragraphs)
    for index, paragraph in enumerate(paragraphs, start=1):  # strictly sequential
        if on_paragraph is not None:
            on_paragraph(index, total)
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
            path = captures_dir / f"{sid}.md"
            # `UnicodeDecodeError` (a `ValueError`, not an `OSError`) alongside the
            # missing-file case: a capture whose write died mid-flush can end mid-character,
            # and an unreadable source costs one paragraph, not the whole pass.
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                skipped.append(f"{sid}: no readable captured content exists")
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
            # D5's capture gating guarantees this text — body markdown and title line alike —
            # was scan-passed and invisible-stripped before it reached disk; fencing (Phase 5,
            # D1) is the second layer, so a page whose provenance was clean still cannot steer
            # the verifier model via untrusted content read straight off disk.
            rendered = render("verify", paragraph=paragraph.text, sources=fence(sources_block))
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
                    detail=CHECK_FAILED_DETAIL,
                    source_ids=pooled_ids,
                    unsupported_items=[],
                )
            )
            check_failures.append(f"{type(exc).__name__}: {exc}")

    reviewer_summary: str | None = None
    if verdicts:  # D-E: zero verdicts makes no consolidation call at all
        try:
            block = _format_verdicts_block(paragraphs, verdicts)
            rendered = render("verify_summary", verdicts=block)
            reply = await model.ainvoke([HumanMessage(content=rendered)])
            summary = str(reply.content).strip()
            if summary:  # D-C: empty/whitespace-only reads as "no summary"
                reviewer_summary = summary
        except Exception as exc:  # noqa: BLE001 — consolidation is best-effort (D-D)
            # Prefixed, unlike a per-paragraph failure's bare message: both land in the same
            # `check_failures` list (`## Gaps and disclosures` prints them identically), and an
            # unprefixed one here would read as a paragraph that went unchecked when none did.
            check_failures.append(f"consolidated summary: {type(exc).__name__}: {exc}")

    return VerificationResult(
        verdicts=verdicts, check_failures=check_failures, reviewer_summary=reviewer_summary
    )
