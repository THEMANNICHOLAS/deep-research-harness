"""Assemble a finished run into a report file.

Pure string work: no model, no network — the only I/O is writing the one report file and
reading the captured `<workspace_dir>/sources/S<n>.md` files to judge which registered
sources are usable evidence (3F fix pass, Major finding), so this module stays fully
offline-testable (Phase 3 plan, `## Execution order` step 5).
"""

import re
from datetime import datetime
from pathlib import Path

from langchain_core.messages import UsageMetadata
from pydantic import BaseModel, ConfigDict

from harness.config import HarnessConfig
from harness.sources import Source, SourceRegistry
from harness.tools.fetch import FETCH_FAILED_PREFIX, _sources_dir

_SLUG_MAX_LENGTH = 60
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_NO_SOURCES_TEXT = "No usable sources were found for this run."
_UNUSABLE_HEADING = "Not usable as evidence (fetch failed or capture missing):"


class RunOutcome(BaseModel):
    """The seam between a finished run and report assembly.

    `registry` rides on `RunOutcome` rather than being passed alongside it, because
    Phase 6 needs to call `registry.resolve()` from inside `report.py`, and
    `write_report`'s signature is frozen as `(outcome, config)` — there is no other route
    for the registry to get there. `arbitrary_types_allowed=True` is required because
    `SourceRegistry` is a plain class, not a pydantic model. Keep fields additive: Phase 5
    adds cut-short state, Phase 6 adds verification results.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    question: str
    answer: str
    registry: SourceRegistry
    usage: UsageMetadata


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
        "## Answer",
        "",
        outcome.answer,
        "",
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
