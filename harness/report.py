"""Assemble a finished run into a report file.

Pure string work: no model, no network — the only I/O is writing the one report file and
reading the captured `<workspace_dir>/sources/S<n>.md` files to judge which registered
sources are usable evidence (3F fix pass, Major finding), so this module stays fully
offline-testable (Phase 3 plan, `## Execution order` step 5).
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import UsageMetadata
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.sources import Source, SourceRegistry
from harness.tools.fetch import FETCH_FAILED_PREFIX, _sources_dir

_SLUG_MAX_LENGTH = 60
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_NO_SOURCES_TEXT = "No usable sources were found for this run."
_UNUSABLE_HEADING = "Not usable as evidence (fetch failed or capture missing):"

# Mirrors `harness/tools/fetch.py`'s `FetchOutcome` — a typed value, not an exception, for
# why a run ended early (the phase's Reuse pattern).
CutShortReason = Literal["round_cap", "wall_clock", "error"]

_CUT_SHORT_HEADING = "## Run cut short"
_NOTES_HEADING = "## Working notes"
_NO_NOTES_TEXT = "No working notes were written before the cutoff."
_NO_ANSWER_TEXT = "The run produced no final answer."
_NO_UNFINISHED_TODOS_TEXT = "No planned todos remained unfinished."

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


def _is_usable(config: HarnessConfig, source: Source) -> bool:
    """A registered source is usable evidence iff its captured file exists and isn't a stub.

    `harness/tools/fetch.py` registers every attempted URL, including 404s, blocked pages,
    and empty ones — the registry alone cannot tell a real finding from a dead URL. The
    captured `<workspace_dir>/sources/S<n>.md` file is Phase 2's frozen record of what
    actually came back, so usability is judged from it, not from registry membership. A
    missing file counts as NOT usable, per the Phase 2 handoff note: absence is treated
    exactly like a stub.
    """
    path = _sources_dir(config) / f"{source.id}.md"
    if not path.exists():
        return False
    try:
        first_line = path.read_text(encoding="utf-8").split("\n", 1)[0]
    except OSError:
        return False
    return not first_line.startswith(FETCH_FAILED_PREFIX)


def _sources_section(config: HarnessConfig, registry: SourceRegistry) -> str:
    usable: list[Source] = []
    unusable: list[Source] = []
    for source in registry.all():
        (usable if _is_usable(config, source) else unusable).append(source)

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


def _notes_section(config: HarnessConfig, started_at: datetime | None) -> str:
    """Render this run's top-level `*.md` files under the workspace root, sorted by name.

    A top-level glob naturally excludes `sources/` — no recursion, so a captured page's
    full text never leaks into this section. A missing workspace dir, or no matches,
    yields the "no notes" line. Unreadable files are skipped, matching `_is_usable`'s
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
    for path in sorted(workspace_dir.glob("*.md"), key=lambda p: p.name):
        try:
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        sections.append(f"### {path.name}\n\n{text}")

    return "\n\n".join(sections) if sections else _NO_NOTES_TEXT


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

    lines += [
        "## Answer",
        "",
        # A cut-short run often has no prose answer at all. Say so, rather than rendering
        # an empty section that reads as "the answer is nothing" (3F Major).
        outcome.answer or _NO_ANSWER_TEXT,
        "",
    ]

    if outcome.cut_short:
        lines += [_NOTES_HEADING, "", _notes_section(config, outcome.started_at), ""]

    lines += [
        "## Sources",
        "",
        _sources_section(config, outcome.registry),
        "",
    ]
    return "\n".join(lines)


def write_report(outcome: RunOutcome, config: HarnessConfig) -> Path:
    """Render `outcome` and write it to `<reports_dir>/YYYY-MM-DD-HHMMSS-<slug>.md`.

    Does NOT call `registry.resolve()` — Phase 6 owns citation resolution. Sources are
    listed; any `[Sn]` marker in the answer is left exactly as the model wrote it.
    """
    config.agent.reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = config.agent.reports_dir / _filename(outcome.question, now)
    model_label = config.roles["head"].model
    path.write_text(_render_body(outcome, config, model_label, now), encoding="utf-8")
    return path
