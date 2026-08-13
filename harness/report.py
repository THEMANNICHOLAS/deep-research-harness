"""Assemble a finished run into a report file.

Pure string work: no model, no network — this module never calls a model itself. The
verification pass's one model call per (claim x source) happens in `harness.verify`,
before `write_report` is ever called; by the time a `RunOutcome` reaches this module its
`verification` field is already-computed data. The only I/O here is writing the one
report file and reading the captured `<workspace_dir>/sources/<run_id>/S<n>.md` files to
judge which registered sources are usable evidence (3F fix pass, Major finding), so this
module stays fully offline-testable (Phase 3 plan, `## Execution order` step 5).
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import UsageMetadata
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.sources import Source, SourceRegistry
from harness.tools.fetch import _sources_dir, is_failed_capture
from harness.verify import ClaimCheck, VerificationResult

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
    Phase 6 needs to call `registry.resolve()` from inside `report.py`, and
    `write_report`'s signature is frozen as `(outcome, config)` — there is no other route
    for the registry to get there. `arbitrary_types_allowed=True` is required because
    `SourceRegistry` is a plain class, not a pydantic model. Keep fields additive: Phase 5
    has landed cut-short state; Phase 6 adds verification results.
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
    # `None` means the verification pass did not run, and renders exactly the
    # pre-Phase-6 report — no markers, no "## Conflicting sources"/"## Gaps and
    # disclosures" sections.
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
    captured `<workspace_dir>/sources/<run_id>/S<n>.md` file is Phase 2's frozen record of
    what actually came back, so usability is judged from it, not from registry
    membership. A missing file counts as NOT usable, per the Phase 2 handoff note:
    absence is treated exactly like a stub.
    """
    path = _sources_dir(config, registry) / f"{source.id}.md"
    if not path.exists():
        return False
    try:
        source_text = path.read_text(encoding="utf-8")
    except OSError:
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


def _notes_section(config: HarnessConfig, started_at: datetime | None) -> str:
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
    that are not UTF-8 text are skipped alongside unreadable ones, matching `_is_usable`'s
    existing `OSError` stance.

    Filtered by `started_at`, and that filter is load-bearing: `agent.workspace_dir` is one
    fixed directory that nothing in `harness/` ever clears, so an unfiltered glob would
    present a PREVIOUS run's notes as this run's findings — the exact overstatement R3
    forbids, in the one report where the reader is least able to catch it (3F Major).
    `None` means "no run start known" (a directly-constructed `RunOutcome` in a test) and
    keeps every file.
    """
    workspace_dir = config.agent.workspace_dir
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


def _place_marker(text: str, claim: str, marker: str) -> str | None:
    """Locate `claim` in `text` whitespace-tolerantly and insert `marker` right after it.

    `extract_claims` joins a block's lines with a single space, so a hard-wrapped or
    bulleted claim is NOT always a verbatim substring of the answer — a literal
    `str.replace` then silently no-ops (3F fix pass, Major finding). Matching instead
    against a regex built from the claim's whitespace-split tokens, joined by `\\s+`,
    tolerates exactly that collapsing while still requiring the claim's words to appear
    in order and adjacent. Returns `None` if the claim cannot be located at all, so the
    caller can disclose it instead of dropping it.
    """
    tokens = claim.split()
    if not tokens:
        return None
    pattern = re.compile(r"\s+".join(re.escape(token) for token in tokens))
    match = pattern.search(text)
    if match is None:
        return None
    # Known limitation, accepted: if the same sentence appears twice, `pattern.search`
    # always finds the first occurrence — both markers would land there. Not worth
    # building an index to fix.
    return text[: match.end()] + marker + text[match.end() :]


def _annotate(outcome: RunOutcome) -> tuple[str, list[ClaimCheck]]:
    """Mark every non-`supported` claim in the answer, then resolve `[Sn]` markers.

    Order is load-bearing: markers are inserted while the claim text can still be found
    in the answer, and `registry.resolve()` runs LAST over the whole thing. Resolving
    first would rewrite `[S1]` into a link and no claim would match.

    Returns `(annotated_text, unplaced)` — `unplaced` holds every check whose claim could
    not be located in the answer at all. A verdict that cannot be shown in place must
    still be disclosed somewhere; the caller renders `unplaced` into
    `## Gaps and disclosures` (3F fix pass, Major finding).
    """
    text = outcome.answer
    unplaced: list[ClaimCheck] = []
    if outcome.verification is not None:
        for check in outcome.verification.checks:
            if check.verdict == "supported":
                continue
            # A leading space, never a paragraph break: the marker has to stay on the line
            # of the sentence it judges. Broken onto its own line it reads as a label on
            # whatever text follows, which attributes the verdict to the wrong claim
            # whenever a paragraph holds more than one sentence (Phase 5 live check).
            if check.source_id is None:
                marker = " **[uncited]**"
            else:
                marker = f" **[{check.verdict} — {check.source_id}]**"
            updated = _place_marker(text, check.claim, marker)
            if updated is None:
                unplaced.append(check)
                continue
            text = updated
    return outcome.registry.resolve(text), unplaced


def _conflicts_section(verification: VerificationResult) -> str:
    """One block per claim where cited sources disagree. Adjudicates nothing (D3)."""
    if not verification.conflicts:
        return ""

    blocks: list[str] = []
    for conflict in verification.conflicts:
        lines = [
            conflict.claim,
            "",
            "The cited sources disagree on this claim. The harness does not decide "
            "between them — both positions are given below so you can judge for "
            "yourself.",
        ]
        lines.extend(
            f"- [{position.source_id}] {position.detail}" for position in conflict.positions
        )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _gaps_section(
    outcome: RunOutcome, verification: VerificationResult, unplaced: list[ClaimCheck]
) -> str:
    """Unresolved citation markers, per-check failures, unplaceable markers, the
    uncited-claim count, and — on a run that was NOT cut short — its dead branches.

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

    # The RAW answer, not the annotated one: annotated markers never introduce or
    # remove an `[Sn]` marker, but reading the raw text keeps this independent of that
    # rendering step.
    unresolved = outcome.registry.unresolved_ids(outcome.answer)
    if unresolved:
        lines.append("Unresolved citation markers (no matching source was registered):")
        lines.extend(f"- {source_id}" for source_id in unresolved)

    if verification.check_failures:
        if lines:
            lines.append("")
        lines.append("Verification checks that failed to run:")
        lines.extend(f"- {failure}" for failure in verification.check_failures)

    if unplaced:
        if lines:
            lines.append("")
        lines.append("Verification results whose marker could not be placed in the answer text:")
        lines.extend(
            f"- {check.verdict} — {check.source_id or 'no source cited'}: the marker "
            "could not be positioned in the answer text."
            for check in unplaced
        )

    uncited_count = sum(1 for check in verification.checks if check.verdict == "uncited")
    if uncited_count:
        if lines:
            lines.append("")
        lines.append(
            f"{uncited_count} claim(s) in the answer carried no citation marker (uncited)."
        )

    return "\n".join(lines)


def _render_body(
    outcome: RunOutcome, config: HarnessConfig, model_label: str, now: datetime
) -> str:
    lines = [
        f"# {outcome.question}",
        "",
        "## Run metadata",
        f"- Timestamp: {now.isoformat()}",
        f"- Model: {model_label}",
        *_usage_lines(outcome.usage),
        "",
    ]

    if outcome.cut_short:
        lines += [_CUT_SHORT_HEADING, "", _cut_short_section(outcome, config), ""]

    annotated_answer, unplaced_checks = _annotate(outcome)
    lines += [
        "## Answer",
        "",
        # A cut-short run often has no prose answer at all. Say so, rather than rendering
        # an empty section that reads as "the answer is nothing" (3F Major).
        annotated_answer or _NO_ANSWER_TEXT,
        "",
    ]

    if outcome.cut_short:
        lines += [_NOTES_HEADING, "", _notes_section(config, outcome.started_at), ""]

    if outcome.verification is not None:
        conflicts_text = _conflicts_section(outcome.verification)
        if conflicts_text:
            lines += [_CONFLICTS_HEADING, "", conflicts_text, ""]

        gaps_text = _gaps_section(outcome, outcome.verification, unplaced_checks)
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

    Citation resolution is UNCONDITIONAL: `_render_body` → `_annotate` calls
    `registry.resolve()` on every run, whether or not `outcome.verification` is set, so
    no bare `[Sn]` marker survives into `## Answer` (R1). What `verification` gates is the
    claim MARKING that happens first, inside the same `_annotate` call.
    """
    config.agent.reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = config.agent.reports_dir / _filename(outcome.question, now)
    model_label = config.roles["head"].model
    path.write_text(_render_body(outcome, config, model_label, now), encoding="utf-8")
    return path
