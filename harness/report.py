"""Assemble a finished run into a report file.

Pure string work: no model, no network — this module never calls a model itself. The
verification pass's one pooled model call per paragraph happens in `harness.verify`,
before `write_report` is ever called; by the time a `RunOutcome` reaches this module its
`verification` field is already-computed data. The only I/O here is writing the one
report file and reading the captured `<workspace_dir>/<run_id>/sources/S<n>.md` files to
judge which registered sources are usable evidence (3F fix pass, Major finding), so this
module stays fully offline-testable (Phase 3 plan, `## Execution order` step 5).
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import UsageMetadata
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig, run_workspace_dir
from harness.paragraphs import LIST_ITEM_RE, Paragraph, strip_markers
from harness.sources import Source, SourceRegistry
from harness.tools.fetch import _sources_dir, is_failed_capture
from harness.verify import MODEL_VERDICTS, ParagraphVerdict, VerificationResult

_SLUG_MAX_LENGTH = 60
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_NO_SOURCES_TEXT = "No usable sources were found for this run."
_UNUSABLE_HEADING = "Not usable as evidence (fetch failed or capture missing):"

# Mirrors `harness/tools/fetch.py`'s `FetchOutcome` — a typed value, not an exception, for
# why a run ended early (the phase's Reuse pattern).
CutShortReason = Literal["round_cap", "wall_clock", "error"]

_CUT_SHORT_HEADING = "## Run cut short"
_NOTES_HEADING = "## Working notes"
_CONFLICTS_HEADING = "## Conflicting sources"
_GAPS_HEADING = "## Gaps and disclosures"
_NO_NOTES_TEXT = "No working notes were written before the cutoff."
_NO_ANSWER_TEXT = "The run produced no final answer."
_NO_UNFINISHED_TODOS_TEXT = "No planned todos remained unfinished."
_DEAD_BRANCHES_HEADING = "Planned steps that were never completed (dead branches):"

# Workspace subdirectories that hold machine-written bulk, not the agent's own notes:
# `sources/` is Phase 2's captured page text (already summarized under `## Sources`), and
# the other two are the summarizer's evicted history. See `_notes_section`.
_NOTES_EXCLUDED_DIRS = frozenset({"sources", "conversation_history", "large_tool_results"})

# Slack allowed when deciding whether a workspace note belongs to THIS run. Filesystem
# mtime granularity is coarser than `datetime.now()` (2s on FAT32, and Windows can report
# a stamp fractionally behind the clock), so a note written moments AFTER the run started
# can carry an mtime moments before it. Erring by two seconds is harmless — runs are
# minutes apart at least — while erring the other way silently drops this run's findings.
_MTIME_TOLERANCE_SECONDS = 2.0

# One distinct phrase per reason. Each must be a substring of none of the others — the
# tests assert one is present AND another absent, to prove the right bound was named
# rather than a swapped `except` label in `__main__` slipping past a looser check.
_ROUND_CAP_TEXT = "the round cap"
_WALL_CLOCK_TEXT = "the wall clock"
_ERROR_TEXT = "an unrecoverable error"


class RunOutcome(BaseModel):
    """The seam between a finished run and report assembly.

    `registry` rides on `RunOutcome` rather than being passed alongside it, because
    `report.py` needs it to render each paragraph's `Sources:` links, and `write_report`'s
    signature is frozen as `(outcome, config)` — there is no other route for the registry
    to get there. `arbitrary_types_allowed=True` is required because `SourceRegistry` is a
    plain class, not a pydantic model. Keep fields additive: Phase 5 landed cut-short
    state; Phase 2+3 add paragraph boundaries and pooled verification results.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    question: str
    answer: str
    registry: SourceRegistry
    usage: UsageMetadata
    cut_short: CutShortReason | None = None
    cut_short_detail: str | None = None  # only meaningful when cut_short == "error"
    todos: list[dict[str, Any]] = Field(default_factory=list)
    # When the run began, used to keep a PREVIOUS run's workspace notes out of this
    # report — see `_notes_section`. `None` means "unknown", which keeps every note.
    started_at: datetime | None = None
    # The ONLY source of paragraph boundaries for `## Answer` (D2) — `report.py` never
    # re-splits `answer`. `harness/__main__.py` calls `split_paragraphs` exactly once and
    # hands the result straight through.
    paragraphs: list[Paragraph] = Field(default_factory=list)
    # `None` means the verification pass did not run. A paragraph that cites a
    # REGISTERED source still renders a `Verdict: not verified - ...` line in that case
    # (item 4) — only a NON-citing paragraph, or the absence of `## Conflicting
    # sources`/`## Gaps and disclosures`, is unaffected by `verification` being unset.
    verification: VerificationResult | None = None


def format_todos(todos: list[dict[str, Any]]) -> str:
    """Render todo entries as `- [status] content` lines.

    Public and living here, not in `harness/__main__.py`, because both the terminal echo
    (R10) and the cut-short report's unfinished-steps list must show the same shape — one
    home for the format, per CLAUDE.md. `__main__` imports `report`, never the reverse.
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

    Finding 9: `kimi-k3` is a reasoning model whose output tokens are mostly reasoning —
    folding that into a bare total would misprice the pyramid.
    """
    reasoning = usage.get("output_token_details", {}).get("reasoning", 0)
    return [
        f"- Input tokens: {usage['input_tokens']}",
        f"- Output tokens: {usage['output_tokens']} (of which {reasoning} reasoning)",
        f"- Total tokens: {usage['total_tokens']}",
    ]


def _is_usable(config: HarnessConfig, registry: SourceRegistry, source: Source) -> bool:
    """A registered source is usable evidence iff its captured file exists and isn't a stub.

    `harness/tools/fetch.py` registers every attempted URL, including 404s, blocked pages,
    and empty ones — the registry alone cannot tell a real finding from a dead URL. The
    captured `<workspace_dir>/<run_id>/sources/S<n>.md` file is Phase 2's frozen record of
    what actually came back, so usability is judged from it, not from registry
    membership. A missing file counts as NOT usable, per the Phase 2 handoff note:
    absence is treated exactly like a stub — and so does an unreadable or non-UTF-8 one.
    A `write_text` that dies mid-flush (ENOSPC, EIO) leaves a byte prefix that can end
    mid-character, and `UnicodeDecodeError` is a `ValueError`, so catching `OSError`
    alone let it escape all the way out of `write_report`, losing the whole report of an
    otherwise finished run (PR #4 review, Major).
    """
    path = _sources_dir(config, registry) / f"{source.id}.md"
    if not path.exists():
        return False
    try:
        source_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return not is_failed_capture(source_text)


def _sources_section(config: HarnessConfig, registry: SourceRegistry) -> str:
    usable: list[Source] = []
    unusable: list[Source] = []
    for source in registry.all():
        (usable if _is_usable(config, registry, source) else unusable).append(source)

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


def _cut_short_section(outcome: RunOutcome, config: HarnessConfig) -> str:
    """One sentence naming the bound that ended the run, then the unfinished todos."""
    if outcome.cut_short == "round_cap":
        # "per pass", not a flat run total: the cap is a LangGraph `recursion_limit`, and
        # langgraph recomputes the budget from the resumed step on every `astream` call,
        # so each clarification resume grants a fresh allowance (plan `## Discoveries`
        # 2026-08-12 — Phase 5; PR #4 review, Major). Naming it as a run-level bound
        # overstated a number the reader could not reconcile with a clarified run.
        bound_line = (
            f"The run was cut short by {_ROUND_CAP_TEXT} "
            f"(configured at {config.agent.max_rounds} rounds per pass)."
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

    Recursive, and NOT restricted to `*.md` (PR #4 review, Major). The lead agent is told
    only "Write findings into your workspace as you go" — `harness/prompts/orchestrator.md`
    pins neither a directory nor an extension — and deepagents' `FilesystemBackend.write`
    creates parent directories for a nested path like `notes/pricing.md` rather than
    rejecting or flattening it. A top-level `*.md` glob therefore printed "no working
    notes were written" while this run's findings sat on disk: an affirmatively false
    disclosure on exactly the path D2 exists to protect, since it is a cut-short run that
    has nothing else to show.

    `_NOTES_EXCLUDED_DIRS` does explicitly what the old glob's lack of recursion did
    accidentally — keeps captured page text (`sources/`) and the summarizer's evicted
    history (`conversation_history/`, `large_tool_results/`) out of the report. Files
    that are not UTF-8 text are skipped alongside unreadable ones, matching `_is_usable`.

    Scans THIS run's workspace subdirectory alone (`run_workspace_dir`), which is what
    keeps another run's notes out — including a run still in flight, whose files the
    `started_at` filter below cannot exclude because they are newer than this run's start
    (PR #4 review). That filter is now the second line rather than the only one: it still
    catches an explicitly reused `run_id`, which `SourceRegistry(run_id=...)` permits.
    `None` means "no run start known" (a directly-constructed `RunOutcome` in a test) and
    keeps every file.
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
        sections.append(f"### {relative.as_posix()}\n\n{text}")

    return "\n\n".join(sections) if sections else _NO_NOTES_TEXT


def _verdict_label(verdict: str) -> str:
    """The reader-facing spelling of a verdict — reports are read by non-technical people."""
    return verdict.replace("_", " ")


def _paragraph_prose(paragraph: Paragraph, verdict: ParagraphVerdict | None) -> list[str]:
    """Marker-stripped prose lines for `paragraph`, with a trailing ` *` appended to each
    bullet line whose zero-based index is in `verdict.unsupported_items` (D4: an index
    outside `range(len(paragraph.items))` is ignored rather than raising).

    A bullet is identified by `LIST_ITEM_RE` — the same test `split_paragraphs` uses to
    build `items` — so the Nth list line IS `items[N]`. Matching on rendered text instead
    let a lead-in line ending in the first bullet's wording consume that bullet's slot,
    putting the `*` on prose and leaving the failing bullet unmarked.
    """
    unsupported = set(verdict.unsupported_items) if verdict is not None else set()
    valid = {i for i in unsupported if 0 <= i < len(paragraph.items)}

    lines: list[str] = []
    item_index = 0
    for raw_line in paragraph.text.split("\n"):
        rendered = strip_markers(raw_line)
        if LIST_ITEM_RE.match(raw_line):
            if item_index in valid:
                rendered = f"{rendered} *"
            item_index += 1
        if rendered:
            lines.append(rendered)
    return lines


def _paragraph_block(
    paragraph: Paragraph, verdict: ParagraphVerdict | None, registry: SourceRegistry
) -> str:
    """Render one paragraph: marker-stripped prose, then `Sources:`/`Verdict:` when the
    paragraph cites at least one REGISTERED source — gated on citation alone, never on
    the verdict value, so a `supported` paragraph gets a line exactly like any other.

    A fenced block is emitted verbatim: stripping markers or re-wrapping it would corrupt
    the code, and it cites nothing, so it carries no `Sources:`/`Verdict:` pair anyway.
    """
    if paragraph.is_code:
        return paragraph.text

    lines = _paragraph_prose(paragraph, verdict)
    registered = [sid for sid in paragraph.source_ids if registry.get(sid) is not None]
    if not registered:
        return "\n".join(lines)

    lines.append(f"Sources: {' '.join(registry.link(sid) for sid in registered)}")

    if verdict is None:
        label = "not verified"
        detail = "verification did not run for this paragraph."
    else:
        label = _verdict_label(verdict.verdict)
        detail = verdict.detail
        # Only a verdict the MODEL returned can carry a bullet rollup. `not_verified` means
        # no check ran, so "n/m bullets verified" would assert a count nothing measured.
        if paragraph.items and verdict.verdict in MODEL_VERDICTS:
            total = len(paragraph.items)
            unsupported_count = len({i for i in verdict.unsupported_items if 0 <= i < total})
            detail = f"{total - unsupported_count}/{total} bullets verified. {detail}"
    lines.append(f"Verdict: {label} - {detail}")
    return "\n".join(lines)


def _answer_section(outcome: RunOutcome) -> str:
    """Render every paragraph in `outcome.paragraphs`, in order.

    The ONLY source of paragraph boundaries (D2) — this never re-splits `outcome.answer`;
    `harness/__main__.py` calls `split_paragraphs` exactly once and hands the list here.
    """
    verdicts = outcome.verification.verdicts if outcome.verification is not None else []
    blocks = [
        _paragraph_block(paragraph, verdicts[i] if i < len(verdicts) else None, outcome.registry)
        for i, paragraph in enumerate(outcome.paragraphs)
    ]
    return "\n\n".join(blocks)


def _conflicts_section(outcome: RunOutcome, verification: VerificationResult) -> str:
    """One block per paragraph whose verdict carries `sources_conflict`. Adjudicates
    nothing (D3): contradiction is MODEL-REPORTED, never derived here.
    """
    blocks: list[str] = []
    for i, verdict in enumerate(verification.verdicts):
        if not verdict.sources_conflict or i >= len(outcome.paragraphs):
            continue
        paragraph = outcome.paragraphs[i]
        # Guard the STRIPPED result, not the raw text: a block that is nothing but a
        # marker (`[S1]`) is truthy raw and empty once stripped, and `[0]` would raise.
        stripped_lines = strip_markers(paragraph.text).splitlines()
        excerpt = stripped_lines[0] if stripped_lines else ""
        lines = [
            excerpt,
            "",
            "The cited sources disagree on this paragraph. The harness does not decide "
            "between them — the sources it read are listed below so you can judge for "
            "yourself.",
        ]
        lines.extend(f"- {outcome.registry.link(sid)}" for sid in verdict.source_ids)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _gaps_section(outcome: RunOutcome, verification: VerificationResult) -> str:
    """Unresolved citation markers, per-check failures, and — on a run that was NOT cut
    short — its dead branches.

    R4 requires dead branches to be disclosed on every run, not only a cut-short one
    (PR #4 review, Major). `_cut_short_section` already lists unfinished todos when a
    bound ended the run, so this renders them only when it did not: an agent that simply
    stops with steps still `pending` has abandoned those branches just as surely, and the
    reader was previously told nothing at all.
    """
    lines: list[str] = []

    if not outcome.cut_short:
        unfinished = [todo for todo in outcome.todos if todo.get("status") != "completed"]
        if unfinished:
            lines.append(_DEAD_BRANCHES_HEADING)
            lines.append(format_todos(unfinished))

    # The RAW answer: `## Answer` strips every marker (registered or not, D4), so reading
    # the raw text keeps this independent of that rendering step.
    unresolved = outcome.registry.unresolved_ids(outcome.answer)
    if unresolved:
        lines.append("Unresolved citation markers (no matching source was registered):")
        lines.extend(f"- {source_id}" for source_id in unresolved)

    if verification.check_failures:
        if lines:
            lines.append("")
        lines.append("Verification checks that failed to run:")
        lines.extend(f"- {failure}" for failure in verification.check_failures)

    return "\n".join(lines)


def _render_body(outcome: RunOutcome, config: HarnessConfig, now: datetime) -> str:
    lines = [
        f"# {outcome.question}",
        "",
        "## Run metadata",
        f"- Timestamp: {now.isoformat()}",
        # Reports what is CONFIGURED for each role, not whether it was ever invoked — the
        # subagent tier is configured but not yet wired (R6).
        f"- Lead Model: {config.roles['head'].model}",
        f"- Subagent Model: {config.roles['subagent'].model}",
        *_usage_lines(outcome.usage),
        "",
    ]

    if outcome.cut_short:
        lines += [_CUT_SHORT_HEADING, "", _cut_short_section(outcome, config), ""]

    answer_text = _answer_section(outcome)
    lines += [
        "## Answer",
        "",
        # A cut-short run often has no prose answer at all. Say so, rather than rendering
        # an empty section that reads as "the answer is nothing" (3F Major).
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

        gaps_text = _gaps_section(outcome, outcome.verification)
        if gaps_text:
            lines += [_GAPS_HEADING, "", gaps_text, ""]

    lines += [
        "## Sources",
        "",
        _sources_section(config, outcome.registry),
        "",
    ]
    return "\n".join(lines)


def write_report(outcome: RunOutcome, config: HarnessConfig) -> Path:
    """Render `outcome` and write it to `<reports_dir>/YYYY-MM-DD-HHMMSS-<slug>.md`.

    Marker stripping is UNCONDITIONAL: `_render_body` → `_answer_section` strips every
    `[Sn]` marker out of each paragraph's prose, registered or not, on every run — so no
    bare marker survives into `## Answer` (R1). A REGISTERED source's link moves onto its
    paragraph's own `Sources:` line instead; an unregistered one is disclosed in `## Gaps
    and disclosures` rather than left inline. What `outcome.verification` gates is only
    the `Verdict:` line's content (a real verdict, or the deterministic "not verified").
    """
    config.agent.reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = config.agent.reports_dir / _filename(outcome.question, now)
    path.write_text(_render_body(outcome, config, now), encoding="utf-8")
    return path
