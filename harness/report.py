"""Assemble a finished run into a report file.

Pure string work: no model, no network. Verification runs in `harness.verify` before
`write_report` is called, so a `RunOutcome`'s `verification` field is already-computed data
by the time it arrives here. The only I/O is writing the report file and reading the captured
`<workspace_dir>/<run_id>/sources/S<n>.md` files to judge which sources are usable evidence,
which keeps this module fully offline-testable.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import UsageMetadata
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig, run_workspace_dir
from harness.paragraphs import LIST_ITEM_RE, Paragraph, strip_markers
from harness.runlog import Incident
from harness.sources import Source, SourceRegistry, is_failed_capture, sources_dir
from harness.verify import MODEL_VERDICTS, ParagraphVerdict, VerificationResult

_SLUG_MAX_LENGTH = 60
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
# Matches a markdown heading marker at the start of any line (per-line, not per-paragraph).
_HEADING_RE = re.compile(r"^(#{1,6}) ", re.MULTILINE)

# The one spelling of each per-paragraph label. Bold, and preceded by a blank line, because
# an unstyled `Sources:` on the line right after the prose read as another sentence of the
# paragraph rather than as machinery about it. Public so tests assert the rendered label
# rather than re-spelling it.
SOURCES_LABEL = "**Sources:**"
VERDICT_LABEL = "**Verdict:**"
# Two trailing spaces: markdown's hard line break. Without it `Sources:` and `Verdict:`
# are one paragraph and a renderer joins them onto a single line; a blank line between
# them instead would space the pair further apart than the paragraph it belongs to.
_HARD_BREAK = "  "
_NO_SOURCES_TEXT = "No usable sources were found for this run."
_UNUSABLE_HEADING = "Not usable as evidence (fetch failed or capture missing):"

# R5's rendering half — see `_read_modes_section` for the D4 bucketing rule.
_READ_MODES_HEADING = "## Source reading"
_ALL_DIGESTED_TEMPLATE = "All {count} sources were read via reader digests."
_DIGESTED_HEADING = "Digested via the reader:"
_FALLBACK_HEADING = "Read raw (fallback, digestion failed or was skipped):"
_UNREAD_HEADING = "Not read at all (fetch never succeeded):"

# Mirrors `harness/tools/fetch.py`'s `FetchOutcome`: a typed value, not an exception, for why
# a run ended early.
CutShortReason = Literal["round_cap", "wall_clock", "error"]

_INCIDENTS_HEADING = "Tool failures during the run:"

_CUT_SHORT_HEADING = "## Run cut short"
_NOTES_HEADING = "## Working notes"
_CONFLICTS_HEADING = "## Conflicting sources"
_GAPS_HEADING = "## Gaps and disclosures"
_NO_NOTES_TEXT = "No working notes were written before the cutoff."
_NO_ANSWER_TEXT = "The run produced no final answer."
_NO_UNFINISHED_TODOS_TEXT = "No planned todos remained unfinished."
_DEAD_BRANCHES_HEADING = "Planned steps that were never completed (dead branches):"

# Machine-written bulk, not the agent's own notes: `sources/` is captured page text (already
# under `## Sources`) and the other two are the summarizer's evicted history.
_NOTES_EXCLUDED_DIRS = frozenset({"sources", "conversation_history", "large_tool_results"})

# Slack allowed when deciding whether a workspace note belongs to THIS run. Filesystem mtime
# granularity is coarser than `datetime.now()` (2s on FAT32, and Windows can report a stamp
# fractionally behind the clock), so a note written just after the run started can carry an
# mtime just before it. Erring by two seconds is harmless — runs are minutes apart — while
# erring the other way silently drops this run's findings.
_MTIME_TOLERANCE_SECONDS = 2.0

# One distinct phrase per reason, none a substring of another: the tests assert one present
# AND another absent, so a swapped `except` label in `__main__` cannot slip past.
_ROUND_CAP_TEXT = "the round cap"
_WALL_CLOCK_TEXT = "the wall clock"
_ERROR_TEXT = "an unrecoverable error"


class RunOutcome(BaseModel):
    """The seam between a finished run and report assembly.

    `registry` rides along rather than being passed alongside because `write_report`'s
    signature is frozen as `(outcome, config)` and rendering each paragraph's `Sources:`
    links needs it. `arbitrary_types_allowed=True` is required because `SourceRegistry` is a
    plain class, not a pydantic model. Keep fields additive.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    question: str
    answer: str
    registry: SourceRegistry
    usage: UsageMetadata
    cut_short: CutShortReason | None = None
    cut_short_detail: str | None = None  # only meaningful when cut_short == "error"
    todos: list[dict[str, Any]] = Field(default_factory=list)
    # When the run began, used to keep a PREVIOUS run's workspace notes out of this report
    # (`_notes_section`). `None` means "unknown", which keeps every note.
    started_at: datetime | None = None
    # The ONLY source of paragraph boundaries for `## Answer` (D2); `report.py` never
    # re-splits `answer`.
    paragraphs: list[Paragraph] = Field(default_factory=list)
    # `None` means the pass did not run. A paragraph citing a REGISTERED source still gets a
    # `Verdict: not verified - ...` line then; only a non-citing paragraph and the two
    # disclosure sections are unaffected by this being unset.
    verification: VerificationResult | None = None
    # The run's degraded-coverage incidents (`harness.runlog.RunLog.incidents()`), disclosed
    # under `## Gaps and disclosures` even when verification never ran.
    incidents: list[Incident] = Field(default_factory=list)


def format_todos(todos: list[dict[str, Any]]) -> str:
    """Render todo entries as `- [status] content` lines.

    Lives here, not in `__main__.py`, because the terminal echo (R10) and the cut-short
    report's unfinished-steps list must show the same shape, and `__main__` imports `report`
    rather than the reverse.
    """
    return "\n".join(f"- [{todo['status']}] {todo['content']}" for todo in todos)


def _slugify(question: str) -> str:
    """Lowercase `question`, collapse non-alphanumerics to `-`, trim to a sane length."""
    slug = _NON_ALPHANUMERIC.sub("-", question.lower()).strip("-")
    if not slug:
        slug = "question"
    return slug[:_SLUG_MAX_LENGTH].strip("-")


def _filename(question: str, now: datetime) -> str:
    timestamp = now.strftime("%Y-%m-%d-%H%M%S")
    return f"{timestamp}-{_slugify(question)}.md"


def _usage_lines(usage: UsageMetadata) -> list[str]:
    """Render input/output/total tokens, keeping the reasoning split visible on its own.

    A reasoning model's output tokens are mostly reasoning, so a bare total misprices the run.
    """
    reasoning = usage.get("output_token_details", {}).get("reasoning", 0)
    return [
        f"- Input tokens: {usage['input_tokens']}",
        f"- Output tokens: {usage['output_tokens']} (of which {reasoning} reasoning)",
        f"- Total tokens: {usage['total_tokens']}",
    ]


def _is_usable(config: HarnessConfig, registry: SourceRegistry, source: Source) -> bool:
    """A registered source is usable evidence iff its captured file exists and isn't a stub.

    `fetch.py` registers every attempted URL, including 404s and blocked pages, so the
    registry alone cannot tell a real finding from a dead one. The captured file is the frozen
    record of what came back, so usability is judged from it. A missing, unreadable or
    non-UTF-8 file counts as NOT usable, exactly like a stub: a `write_text` dying mid-flush
    leaves a byte prefix that can end mid-character, and `UnicodeDecodeError` is a
    `ValueError`, so catching `OSError` alone let it escape `write_report` and lose the whole
    report of an otherwise finished run.
    """
    path = sources_dir(config, registry) / f"{source.id}.md"
    if not path.exists():
        return False
    try:
        source_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return not is_failed_capture(source_text)


def partition_sources(
    config: HarnessConfig, registry: SourceRegistry
) -> tuple[list[Source], list[Source]]:
    """Split this run's registered sources into `(usable, unusable)`.

    The one public entry to `_is_usable`: the report body and the CLI's end-of-run summary
    both need this split, and computing it twice let the summary's counts drift from what the
    report lists once usability semantics changed.
    """
    usable: list[Source] = []
    unusable: list[Source] = []
    for source in registry.all():
        (usable if _is_usable(config, registry, source) else unusable).append(source)
    return usable, unusable


def _sources_section(config: HarnessConfig, registry: SourceRegistry) -> str:
    usable, unusable = partition_sources(config, registry)

    lines: list[str] = []
    if usable:
        lines.extend(f"- [{source.id}] {registry.link(source.id)}" for source in usable)
    else:
        lines.append(_NO_SOURCES_TEXT)

    if unusable:
        if lines:
            lines.append("")
        lines.append(_UNUSABLE_HEADING)
        lines.extend(f"- [{source.id}] {registry.link(source.id)}" for source in unusable)

    return "\n".join(lines)


def _read_modes_section(registry: SourceRegistry) -> str:
    """Disclose how each registered source was actually read (R5, D4).

    Read STRICTLY from `Source.read_mode` -- never from parsing `<undigested>` markers out of
    a capture body, since those bodies are unescaped page text a report render must not trust
    to parse. Empty registry renders nothing at all; an all-digested run still renders one
    summary line, since digestion is the thing R5 wants observable, not only its exceptions.
    """
    sources = registry.all()
    if not sources:
        return ""

    by_mode: dict[str, list[Source]] = {"digested": [], "fallback": [], "unread": []}
    for source in sources:
        by_mode[source.read_mode].append(source)

    if len(by_mode["digested"]) == len(sources):
        return _ALL_DIGESTED_TEMPLATE.format(count=len(sources))

    lines: list[str] = []
    for mode, heading in (
        ("digested", _DIGESTED_HEADING),
        ("fallback", _FALLBACK_HEADING),
        ("unread", _UNREAD_HEADING),
    ):
        bucket = by_mode[mode]
        if not bucket:
            continue
        if lines:
            lines.append("")
        lines.append(heading)
        lines.extend(f"- [{source.id}] {registry.link(source.id)}" for source in bucket)

    return "\n".join(lines)


def _cut_short_section(outcome: RunOutcome, config: HarnessConfig) -> str:
    """One sentence naming the bound that ended the run, then the unfinished todos."""
    if outcome.cut_short == "round_cap":
        # A run-level total: `__main__` counts model turns itself across every pass, so the
        # configured number is exactly what the run was allowed.
        bound_line = (
            f"The run was cut short by {_ROUND_CAP_TEXT} "
            f"(configured at {config.agent.max_rounds} rounds)."
        )
    elif outcome.cut_short == "wall_clock":
        bound_line = (
            f"The run was cut short by {_WALL_CLOCK_TEXT} "
            f"(configured at {config.agent.wall_clock_seconds} seconds)."
        )
    else:  # "error"
        bound_line = f"The run ended due to {_ERROR_TEXT}: {outcome.cut_short_detail}"

    unfinished = [todo for todo in outcome.todos if todo.get("status") != "completed"]
    todo_lines = format_todos(unfinished) if unfinished else _NO_UNFINISHED_TODOS_TEXT

    return f"{bound_line}\n\n{todo_lines}"


def _notes_section(
    config: HarnessConfig, registry: SourceRegistry, started_at: datetime | None
) -> str:
    """Render this run's workspace files, at any depth, sorted by relative path.

    Recursive and NOT restricted to `*.md`: the lead is told only "Write findings into your
    workspace as you go" — no directory, no extension — and deepagents' `FilesystemBackend`
    creates parents for a nested path like `notes/pricing.md`. A top-level `*.md` glob
    therefore printed "no working notes were written" while this run's findings sat on disk,
    on exactly the cut-short run that has nothing else to show.

    `_NOTES_EXCLUDED_DIRS` keeps captured page text and evicted history out. Non-UTF-8 files
    are skipped alongside unreadable ones, matching `_is_usable`.

    Scans THIS run's workspace subdirectory alone, which is what keeps another run's notes
    out — including a run still in flight, whose files are newer than this run's start and so
    survive the `started_at` filter. That filter is the second line of defense: it still
    catches an explicitly reused `run_id`. `None` means "no run start known" and keeps
    every file.
    """
    workspace_dir = run_workspace_dir(config, registry.run_id)
    if not workspace_dir.exists():
        return _NO_NOTES_TEXT

    cutoff = started_at.timestamp() - _MTIME_TOLERANCE_SECONDS if started_at is not None else None
    sections: list[str] = []
    candidates = [path for path in workspace_dir.rglob("*") if path.is_file()]
    for path in sorted(candidates, key=lambda p: p.relative_to(workspace_dir).as_posix()):
        relative = path.relative_to(workspace_dir)
        if relative.parts[0] in _NOTES_EXCLUDED_DIRS:
            continue
        try:
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Demoted like answer prose: a note is model-authored too, and embedding it verbatim
        # put its `# `/`## ` headings at the report's own title and section depths.
        sections.append(f"### {relative.as_posix()}\n\n{_demote_headings(text)}")

    return "\n\n".join(sections) if sections else _NO_NOTES_TEXT


def _demote_headings(text: str) -> str:
    """Demote every markdown heading in `text` by two levels, capped at `######`.

    Model `# ` -> `### `, `## ` -> `#### `, so nothing the model writes can collide with the
    report's own `# <question>` title or its `## `-depth section headings. `#####`/`######`
    both land at `######` (the cap), which loses distinction only at those two deepest,
    unrealistic-for-an-answer levels; relative ordering is preserved everywhere else.
    """

    def _bump(match: re.Match[str]) -> str:
        return "#" * min(len(match.group(1)) + 2, 6) + " "

    return _HEADING_RE.sub(_bump, text)


def _verdict_label(verdict: str) -> str:
    """The reader-facing spelling of a verdict."""
    return verdict.replace("_", " ")


def _paragraph_prose(paragraph: Paragraph, verdict: ParagraphVerdict | None) -> list[str]:
    """Marker-stripped, heading-demoted prose lines for `paragraph`, with a trailing ` *` on
    each bullet whose zero-based index is in `verdict.unsupported_items` (an out-of-range
    index is ignored, D4).

    Bullets are identified by `LIST_ITEM_RE`, the same test `split_paragraphs` uses to build
    `items`, so the Nth list line IS `items[N]`. Matching on rendered text instead let a
    lead-in ending in the first bullet's wording consume that bullet's slot, putting the `*`
    on prose and leaving the failing bullet unmarked.

    Only reached for non-code paragraphs (`_paragraph_block` early-returns on `is_code`), so
    `_demote_headings` here is the one place a model-authored `#`/`##` heading is pushed below
    the report's own `# `/`## ` depths before `## Answer` is assembled.
    """
    unsupported = set(verdict.unsupported_items) if verdict is not None else set()
    valid = {i for i in unsupported if 0 <= i < len(paragraph.items)}

    lines: list[str] = []
    item_index = 0
    for raw_line in _demote_headings(paragraph.text).split("\n"):
        rendered = strip_markers(raw_line)
        if LIST_ITEM_RE.match(raw_line):
            # A citation-only bullet renders no line, so a bare ` *` would mark nothing.
            if rendered and item_index in valid:
                rendered = f"{rendered} *"
            item_index += 1
        if rendered:
            lines.append(rendered)
    return lines


def _paragraph_block(
    paragraph: Paragraph, verdict: ParagraphVerdict | None, registry: SourceRegistry
) -> str:
    """Render one paragraph: marker-stripped prose, a blank line, then the bold
    `**Sources:**`/`**Verdict:**` pair when the paragraph cites at least one REGISTERED
    source. Gated on citation alone, never on the verdict value, so a `supported`
    paragraph gets a line like any other.

    A fenced block is emitted verbatim — stripping markers would corrupt the code — and it
    cites nothing, so it carries no pair anyway.
    """
    if paragraph.is_code:
        return paragraph.text

    lines = _paragraph_prose(paragraph, verdict)
    registered = [sid for sid in paragraph.source_ids if registry.get(sid) is not None]
    if not registered:
        return "\n".join(lines)

    links = " ".join(registry.link(sid) for sid in registered)
    # Guarded, not unconditional: a paragraph that is nothing but a citation marker
    # (`[S1]`) strips to no prose at all, and a separator with nothing above it opens the
    # block with a blank line the joined answer does not need.
    if lines:
        lines.append("")
    lines.append(f"{SOURCES_LABEL} {links}{_HARD_BREAK}")

    if verdict is None:
        label = "not verified"
        detail = "verification did not run for this paragraph."
    else:
        label = _verdict_label(verdict.verdict)
        detail = verdict.detail
        # Only a MODEL verdict can carry a rollup: `not_verified` means no check ran, so
        # "n/m bullets verified" would assert a count nothing measured. Counted over the
        # bullets the reader can SEE — a citation-only bullet renders no line, so counting it
        # would inflate the denominator past the list.
        counted = [i for i, item in enumerate(paragraph.items) if strip_markers(item)]
        if counted and verdict.verdict in MODEL_VERDICTS:
            unsupported_count = len(set(verdict.unsupported_items) & set(counted))
            detail = f"{len(counted) - unsupported_count}/{len(counted)} bullets verified. {detail}"
    lines.append(f"{VERDICT_LABEL} {label} - {detail}")
    return "\n".join(lines)


def _answer_section(outcome: RunOutcome) -> str:
    """Render every paragraph in `outcome.paragraphs`, in order.

    Never re-splits `outcome.answer` (D2): `__main__.py` splits once and hands the list here.
    """
    verdicts = outcome.verification.verdicts if outcome.verification is not None else []
    blocks = [
        _paragraph_block(paragraph, verdicts[i] if i < len(verdicts) else None, outcome.registry)
        for i, paragraph in enumerate(outcome.paragraphs)
    ]
    return "\n\n".join(blocks)


def _conflicts_section(outcome: RunOutcome, verification: VerificationResult) -> str:
    """One block per paragraph whose verdict carries `sources_conflict`. Adjudicates nothing
    (D3): contradiction is MODEL-REPORTED, never derived here.
    """
    blocks: list[str] = []
    for i, verdict in enumerate(verification.verdicts):
        if not verdict.sources_conflict or i >= len(outcome.paragraphs):
            continue
        paragraph = outcome.paragraphs[i]
        # Guard the STRIPPED result: a block that is nothing but a marker is truthy raw and
        # empty once stripped, so `[0]` would raise.
        stripped_lines = strip_markers(paragraph.text).splitlines()
        excerpt = stripped_lines[0] if stripped_lines else ""
        lines = [
            excerpt,
            "",
            # The model's own statement of WHAT they disagree about — without it the block
            # names the sources but never the disagreement.
            verdict.detail,
            "",
            "The cited sources disagree on this paragraph. The harness does not decide "
            "between them — the sources it read are listed below so you can judge for "
            "yourself.",
        ]
        lines.extend(f"- {outcome.registry.link(sid)}" for sid in verdict.source_ids)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _gaps_section(outcome: RunOutcome, verification: VerificationResult | None) -> str:
    """Unresolved citation markers, run incidents, per-check failures, and — on a run that was
    NOT cut short — its dead branches.

    R4 requires dead branches on every run, not only a cut-short one. `_cut_short_section`
    already lists unfinished todos when a bound ended the run, so this renders them only when
    it did not: an agent that simply stops with steps still `pending` has abandoned those
    branches just as surely.

    `verification` may be `None` (the pass never ran): incidents and unresolved markers must
    still be disclosed on exactly those runs.
    """
    lines: list[str] = []

    if not outcome.cut_short:
        unfinished = [todo for todo in outcome.todos if todo.get("status") != "completed"]
        if unfinished:
            lines.append(_DEAD_BRANCHES_HEADING)
            lines.append(format_todos(unfinished))

    # The RAW answer: `## Answer` strips every marker (D4), so reading the raw text keeps this
    # independent of that rendering step.
    unresolved = outcome.registry.unresolved_ids(outcome.answer)
    if unresolved:
        lines.append("Unresolved citation markers (no matching source was registered):")
        lines.extend(f"- {source_id}" for source_id in unresolved)

    if outcome.incidents:
        if lines:
            lines.append("")
        lines.append(_INCIDENTS_HEADING)
        lines.extend(f"- {incident.detail}" for incident in outcome.incidents)

    if verification is not None and verification.check_failures:
        if lines:
            lines.append("")
        lines.append("Verification checks that failed to run:")
        lines.extend(f"- {failure}" for failure in verification.check_failures)

    # A count mismatch means `## Answer` silently rendered the overflow paragraphs as
    # "not verified" — say so rather than letting that read as a deliberate verdict. Zero
    # verdicts is "the pass did not run", which the sections above already cover.
    if (
        verification is not None
        and verification.verdicts
        and len(verification.verdicts) != len(outcome.paragraphs)
    ):
        if lines:
            lines.append("")
        lines.append(
            f"Verification returned {len(verification.verdicts)} verdict(s) for "
            f"{len(outcome.paragraphs)} paragraph(s); paragraphs without a matching verdict "
            "are shown as not verified."
        )

    return "\n".join(lines)


def _render_body(outcome: RunOutcome, config: HarnessConfig, now: datetime) -> str:
    lines = [
        f"# {outcome.question}",
        "",
        "## Run metadata",
        f"- Timestamp: {now.isoformat()}",
        # What is CONFIGURED for each role, not whether it was invoked this run: the subagent
        # tier is wired as the reader (R6).
        f"- Lead Model: {config.roles['head'].model}",
        f"- Subagent Model: {config.roles['subagent'].model}",
        f"- Verifier Model: {config.roles['verifier'].model}",
        *_usage_lines(outcome.usage),
        "",
    ]

    if outcome.cut_short:
        lines += [_CUT_SHORT_HEADING, "", _cut_short_section(outcome, config), ""]

    answer_text = _answer_section(outcome)
    lines += [
        "## Answer",
        "",
        # A cut-short run often has no prose answer. Say so, rather than leaving an empty
        # section that reads as "the answer is nothing".
        answer_text or _NO_ANSWER_TEXT,
        "",
    ]

    if outcome.cut_short:
        lines += [
            _NOTES_HEADING,
            "",
            _notes_section(config, outcome.registry, outcome.started_at),
            "",
        ]

    if outcome.verification is not None:
        conflicts_text = _conflicts_section(outcome, outcome.verification)
        if conflicts_text:
            lines += [_CONFLICTS_HEADING, "", conflicts_text, ""]

    # NOT gated on verification: incidents and unresolved markers belong to the run itself,
    # and the runs that skip verification are exactly the ones with the most to disclose.
    gaps_text = _gaps_section(outcome, outcome.verification)
    if gaps_text:
        lines += [_GAPS_HEADING, "", gaps_text, ""]

    lines += [
        "## Sources",
        "",
        _sources_section(config, outcome.registry),
        "",
    ]

    read_modes_text = _read_modes_section(outcome.registry)
    if read_modes_text:
        lines += [_READ_MODES_HEADING, "", read_modes_text, ""]

    return "\n".join(lines)


def write_report(outcome: RunOutcome, config: HarnessConfig) -> Path:
    """Render `outcome` and write it to `<reports_dir>/YYYY-MM-DD-HHMMSS-<slug>.md`.

    Marker stripping is UNCONDITIONAL: every `[Sn]` leaves the prose on every run, registered
    or not, so no bare marker survives into `## Answer` (R1). A registered source's link moves
    onto its paragraph's `Sources:` line; an unregistered one is disclosed under `## Gaps and
    disclosures`. `outcome.verification` gates only the `Verdict:` line's content — a real
    verdict, or the deterministic "not verified".
    """
    config.agent.reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = config.agent.reports_dir / _filename(outcome.question, now)
    path.write_text(_render_body(outcome, config, now), encoding="utf-8")
    return path
