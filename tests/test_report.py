"""Behavioral tests for harness.report. Pure string work: no model, no network.

`RunOutcome`'s field names (`question`, `answer`, `registry`, `usage`) are not pinned by
the plan's Contracts section — only the class name, `write_report`'s signature, and the
frozen filename format are. This suite fixes those field names as part of writing the
tests first; `harness/report.py` must match them.

`outcome.paragraphs` is the ONLY source of paragraph boundaries for the `## Answer`
section (D2) — `report.py` never re-splits `answer`. Every test here that checks Answer
content passes `paragraphs=split_paragraphs(answer)` explicitly, exactly as
`harness/__main__.py` will (Phase 2+3 plan, PART B).
"""

import os
import re
from datetime import datetime

import pytest
from pydantic import ValidationError

from harness.config import AgentSettings
from harness.paragraphs import split_paragraphs
from harness.report import (
    _CUT_SHORT_HEADING,
    _ERROR_TEXT,
    _NO_ANSWER_TEXT,
    _NO_NOTES_TEXT,
    _NOTES_HEADING,
    _ROUND_CAP_TEXT,
    _UNUSABLE_HEADING,
    _WALL_CLOCK_TEXT,
    RunOutcome,
    write_report,
)
from harness.sources import SourceRegistry
from harness.verify import ParagraphVerdict, VerificationResult
from tests.conftest import write_failed_capture, write_source_capture

_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}-[a-z0-9-]+\.md$")

# An mtime far enough back that no test run can straddle it — stands in for "written by a
# previous run of the harness".
_LONG_AGO = 1_000_000_000.0


def _usage(reasoning: int = 0, input_tokens: int = 100, output_tokens: int = 50) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "output_token_details": {"reasoning": reasoning},
    }


def test_write_report_produces_a_frozen_filename_under_reports_dir(make_config):
    config = make_config()
    registry = SourceRegistry()
    registry.add("https://example.test/a", title="Example A")
    outcome = RunOutcome(
        question="What is the melting point of tungsten?",
        answer="Tungsten melts at 3422 C [S1].",
        registry=registry,
        usage=_usage(reasoning=12),
    )

    path = write_report(outcome, config)

    assert path.parent == config.agent.reports_dir
    assert _FILENAME_RE.match(path.name), path.name
    assert path.exists()
    assert path.read_text(encoding="utf-8")
    # Guards against a stub emitting a constant/placeholder slug: the question's own
    # words must survive into the filename, not just satisfy the shape regex.
    assert "melting" in path.name
    assert "tungsten" in path.name


def test_write_report_run_metadata_names_both_configured_models(make_config):
    """Even when head and subagent are configured to the same model, both roles still get
    their own named line (R6) — this asserts on the two distinctly-labeled lines, not just
    that the model string appears somewhere in the body.
    """
    config = make_config()
    outcome = RunOutcome(
        question="What is the boiling point of water?",
        answer="100 C at sea level.",
        registry=SourceRegistry(),
        usage=_usage(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "- Lead Model: test-model" in body
    assert "- Subagent Model: test-model" in body


def test_write_report_run_metadata_reads_each_role_from_its_own_config_entry(make_config):
    """The falsifiable half of R6: with head and subagent configured to DIFFERENT models,
    each line must carry its own role's model. Rendering the head model on both lines
    passes the same-model test above and fails this one.
    """
    config = make_config(head_model="lead-model-x", subagent_model="worker-model-y")
    outcome = RunOutcome(
        question="What is the boiling point of water?",
        answer="100 C at sea level.",
        registry=SourceRegistry(),
        usage=_usage(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "- Lead Model: lead-model-x" in body
    assert "- Subagent Model: worker-model-y" in body
    assert "- Subagent Model: lead-model-x" not in body


def test_write_report_returns_a_path_whose_body_is_the_answer_actually_written(make_config):
    """`[S1]` is unregistered here, so it is stripped from the prose like any other marker
    (D4) rather than left bare — the prose itself must still survive verbatim otherwise.
    """
    config = make_config()
    answer = "Yes, due to Rayleigh scattering [S1]."
    outcome = RunOutcome(
        question="Is the sky blue?",
        answer=answer,
        registry=SourceRegistry(),
        usage=_usage(),
        paragraphs=split_paragraphs(answer),
    )

    path = write_report(outcome, config)

    assert path.is_file()
    assert "Yes, due to Rayleigh scattering." in path.read_text(encoding="utf-8")
    assert "[S1]" not in path.read_text(encoding="utf-8")


def test_write_report_with_no_usable_sources_still_writes_a_report_saying_so(make_config):
    config = make_config()
    outcome = RunOutcome(
        question="What is the airspeed velocity of an unladen swallow?",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
    )

    path = write_report(outcome, config)

    assert path.exists()
    body = path.read_text(encoding="utf-8")
    # Asserted on the rendered text, per the plan, not on a flag anywhere in RunOutcome.
    assert "no usable sources" in body.lower()


def test_write_report_with_usable_sources_does_not_claim_it_has_none(make_config):
    """The paired negative for the test above.

    Without this, a stub that always prints "no usable sources" regardless of input would
    pass the no-sources test in isolation. Same `RunOutcome` shape, a registry that is NOT
    empty AND whose source has a real captured (non-stub) file: the phrase must be absent
    and the source must be listed instead.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/tungsten", title="Tungsten facts")
    write_source_capture(config, registry, source_id)
    outcome = RunOutcome(
        question="What is the airspeed velocity of an unladen swallow?",
        answer="African or European? [S1]",
        registry=registry,
        usage=_usage(),
    )

    path = write_report(outcome, config)

    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert "no usable sources" not in body.lower()
    assert "example.test" in body


def test_write_report_treats_registered_stub_sources_as_not_usable(make_config):
    """A registered source whose captured file is a `FETCH FAILED:` stub is not evidence.

    3F Major finding: `fetch.py` registers every attempted URL, including 404s and blocked
    pages. A registry whose sources are all stubs must NOT list them as usable sources, and
    must still state plainly that no usable sources were found.
    """
    config = make_config()
    registry = SourceRegistry()
    dead_id = registry.add("https://example.test/dead-link", title=None)
    write_failed_capture(config, registry, dead_id, outcome="blocked")
    outcome = RunOutcome(
        question="What killed the link?",
        answer="Unable to determine — the only lead was unreachable.",
        registry=registry,
        usage=_usage(),
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert "no usable sources" in body.lower()
    # The stub is mentioned (plainly, under the "not usable" heading) but never as an
    # unqualified usable-source bullet ahead of that heading — position, not just
    # presence, is what proves it was never counted as evidence.
    no_sources_pos = body.lower().index("no usable sources")
    heading_pos = body.index(_UNUSABLE_HEADING)
    dead_bullet_pos = body.index(f"[{dead_id}]")
    assert no_sources_pos < heading_pos < dead_bullet_pos


def test_write_report_lists_usable_sources_and_marks_stubs_separately(make_config):
    """Mixed case: one real capture, one stub. Each must be judged on its own file."""
    config = make_config()
    registry = SourceRegistry()
    good_id = registry.add("https://good.example.test/page", title="Good page")
    bad_id = registry.add("https://bad.example.test/page", title=None)
    write_source_capture(config, registry, good_id)
    write_failed_capture(config, registry, bad_id, outcome="timeout")
    outcome = RunOutcome(
        question="Mixed source usability",
        answer="Partial answer [S1].",
        registry=registry,
        usage=_usage(),
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert "no usable sources" not in body.lower()
    assert "good.example.test" in body
    assert "bad.example.test" in body
    assert _UNUSABLE_HEADING in body
    # Position, not just presence: the usable source is listed normally, before the
    # "not usable" heading; the stub is listed after it, under that heading.
    good_pos = body.index(f"[{good_id}]")
    bad_pos = body.index(f"[{bad_id}]")
    heading_pos = body.index(_UNUSABLE_HEADING)
    assert good_pos < heading_pos < bad_pos


def test_write_report_creates_reports_dir_if_missing(make_config, tmp_path):
    agent = AgentSettings(
        workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports" / "nested"
    )
    config = make_config(agent=agent)
    assert not config.agent.reports_dir.exists()
    outcome = RunOutcome(
        question="Does write_report create its own directory?",
        answer="Yes.",
        registry=SourceRegistry(),
        usage=_usage(),
    )

    path = write_report(outcome, config)

    assert path.exists()
    assert config.agent.reports_dir.exists()


def test_write_report_body_carries_answer_reasoning_split_and_sources(make_config):
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a", title="Example A")
    write_source_capture(config, registry, source_id)
    answer = f"Answer text with a marker [{source_id}]."
    outcome = RunOutcome(
        question="reasoning split check",
        answer=answer,
        registry=registry,
        usage=_usage(reasoning=37, input_tokens=200, output_tokens=80),
        paragraphs=split_paragraphs(answer),
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    # Phase 6 resolves every REGISTERED `[Sn]` marker into a clickable link (R1), so the
    # prose survives but the raw marker must not — resolution is mechanical (substrate
    # D4) and runs whether or not the verification pass did.
    answer_section = _section(body, "## Answer")
    assert "Answer text with a marker" in answer_section
    assert "[S1]" not in answer_section
    assert "example.test" in answer_section
    # The reasoning-token count must be visible on its own, not folded silently into a
    # bare total (finding 9: a plain total misprices the pyramid for a reasoning model).
    assert "37" in body
    assert "280" in body  # total_tokens = input + output
    assert "example.test" in body  # the ## Sources list, built from the registry


def test_run_outcome_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RunOutcome(
            question="q",
            answer="a",
            registry=SourceRegistry(),
            usage=_usage(),
            unexpected_field="nope",
        )


# --- Phase 5: cut-short disclosure -----------------------------------------------------


def _section(body: str, heading: str) -> str:
    """Return the text between `heading` and the next top-level `## ` heading (or EOF).

    Isolates a section by content, not a fixed line offset, so these tests stay correct
    regardless of exactly where `## Run cut short` / `## Working notes` land relative to
    the surrounding sections.
    """
    start = body.index(heading) + len(heading)
    rest = body[start:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_write_report_discloses_the_round_cap_bound(make_config, tmp_path):
    """A hardcoded default cap could never reveal a NON-default configured value."""
    agent = AgentSettings(
        max_rounds=17, workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports"
    )
    config = make_config(agent=agent)
    outcome = RunOutcome(
        question="How far did the run get before the round cap hit?",
        answer="Partial answer only.",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="round_cap",
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert _CUT_SHORT_HEADING in body
    section = _section(body, _CUT_SHORT_HEADING)
    assert "17" in section
    # The untouched AgentSettings default (20) must never appear here — that would mean
    # the configured value was ignored in favor of a hardcoded default.
    assert "20" not in section


def test_write_report_discloses_the_wall_clock_bound(make_config, tmp_path):
    """Same shape as the round-cap test, for the other bound."""
    agent = AgentSettings(
        wall_clock_seconds=93,
        workspace_dir=tmp_path / "workspace",
        reports_dir=tmp_path / "reports",
    )
    config = make_config(agent=agent)
    outcome = RunOutcome(
        question="How long did the run get before the wall clock hit?",
        answer="Partial answer only.",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="wall_clock",
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert _CUT_SHORT_HEADING in body
    section = _section(body, _CUT_SHORT_HEADING)
    assert "93" in section
    assert "1800" not in section  # the untouched AgentSettings default


def test_write_report_names_the_error_when_a_run_dies_mid_flight(make_config):
    config = make_config()
    outcome = RunOutcome(
        question="What happened to the run?",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="error",
        cut_short_detail="APIConnectionError: getaddrinfo failed",
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert _CUT_SHORT_HEADING in body
    assert "APIConnectionError: getaddrinfo failed" in body


def test_write_report_lists_only_planned_todos_not_completed(make_config):
    config = make_config()
    todos = [
        {"content": "Search for pricing sources", "status": "pending"},
        {"content": "Summarize the vendor comparison", "status": "completed"},
        {"content": "Cross-check the delivery claim", "status": "in_progress"},
    ]
    outcome = RunOutcome(
        question="Which vendor is cheapest?",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="round_cap",
        todos=todos,
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert "Search for pricing sources" in body
    assert "Cross-check the delivery claim" in body
    assert "Summarize the vendor comparison" not in body


def test_write_report_includes_workspace_notes_when_cut_short(make_config):
    config = make_config()
    config.agent.workspace_dir.mkdir(parents=True, exist_ok=True)
    (config.agent.workspace_dir / "notes.md").write_text(
        "Vendor Acme quoted $4.20/unit, confirmed by two listings.", encoding="utf-8"
    )
    # A captured source file, written directly to sources/<run_id>/ the way
    # harness/tools/fetch.py does — never via the registry, so nothing else could surface
    # its text.
    registry = SourceRegistry()
    write_source_capture(config, registry, "S1")
    outcome = RunOutcome(
        question="What pricing was found before the cutoff?",
        answer="",
        registry=registry,
        usage=_usage(),
        cut_short="wall_clock",
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert _NOTES_HEADING in body
    assert "Vendor Acme quoted $4.20/unit" in body
    # The notes section reads the workspace root only, never recursing into sources/ — if
    # it did, this exact capture body would leak into the notes section.
    assert "Some captured body text." not in body
    # Guards against a stub that always claims "no notes" regardless of what is on disk.
    assert _NO_NOTES_TEXT not in body


def test_write_report_says_so_when_no_notes_were_written(make_config):
    config = make_config()
    config.agent.workspace_dir.mkdir(parents=True, exist_ok=True)
    outcome = RunOutcome(
        question="Did anything get written before the cut?",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="round_cap",
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert _NOTES_HEADING in body
    assert _NO_NOTES_TEXT in body


def test_write_report_excludes_notes_left_by_a_previous_run(make_config):
    """`agent.workspace_dir` is one fixed directory nothing in `harness/` ever clears, so
    an unfiltered glob would present a PREVIOUS run's notes as this run's findings — an
    overstatement of evidence (R3) that the reader has no way to catch. Only files touched
    at or after `started_at` belong to this run.
    """
    config = make_config()
    config.agent.workspace_dir.mkdir(parents=True, exist_ok=True)
    stale = config.agent.workspace_dir / "old-run.md"
    stale.write_text("Acme was cheapest, from last week's run.", encoding="utf-8")
    os.utime(stale, (_LONG_AGO, _LONG_AGO))

    started_at = datetime.now()
    fresh = config.agent.workspace_dir / "this-run.md"
    fresh.write_text("Beta quoted $9.10/unit today.", encoding="utf-8")

    outcome = RunOutcome(
        question="What did this run actually find?",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="wall_clock",
        started_at=started_at,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Beta quoted $9.10/unit today." in body
    assert "Acme was cheapest, from last week's run." not in body


def test_write_report_keeps_every_note_when_no_start_time_is_known(make_config):
    """`started_at=None` means "unknown", which must keep notes rather than silently drop
    them — losing this run's findings would be the worse failure of the two.
    """
    config = make_config()
    config.agent.workspace_dir.mkdir(parents=True, exist_ok=True)
    old = config.agent.workspace_dir / "whenever.md"
    old.write_text("A finding of unknown vintage.", encoding="utf-8")
    os.utime(old, (_LONG_AGO, _LONG_AGO))

    outcome = RunOutcome(
        question="What is on disk?",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="round_cap",
        started_at=None,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "A finding of unknown vintage." in body


def test_write_report_says_so_when_the_run_produced_no_answer(make_config):
    """A cut-short run usually has no prose answer. An empty `## Answer` section reads as
    "the answer is nothing"; the reader must be told the run never got that far.
    """
    config = make_config()
    outcome = RunOutcome(
        question="Did it answer?",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="wall_clock",
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert _NO_ANSWER_TEXT in _section(body, "## Answer")


def test_write_report_has_no_cut_short_sections_when_the_run_finished(make_config):
    config = make_config()
    outcome = RunOutcome(
        question="Did the run finish cleanly?",
        answer="Yes.",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short=None,
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert _CUT_SHORT_HEADING not in body
    assert _NOTES_HEADING not in body


# --- Phase 2+3 (pooled paragraph verification and rendering): markers, conflicts, gaps -


def test_write_report_renders_a_verdict_line_for_every_citing_paragraph_regardless_of_verdict(
    make_config,
):
    """Every verdict in the frozen vocabulary renders its own `Verdict:` line —
    INCLUDING `supported`. Under the pooled-paragraph contract the Sources:/Verdict:
    block is gated on whether a paragraph cites a REGISTERED source, never on the verdict
    value (item 2), so the old "supported renders no marker" distinction from the
    per-claim scheme no longer exists. `no_sources_cited` is exercised elsewhere (it can
    only occur on a paragraph with NO registered source, which by definition renders no
    Verdict: line at all).
    """
    from typing import get_args

    from harness.verify import Verdict

    config = make_config()
    registry = SourceRegistry()
    verdict_values = ("supported", "partially_supported", "not_supported", "not_verified")
    ids = {v: registry.add(f"https://example.test/{v}") for v in verdict_values}
    for v, sid in ids.items():
        write_source_capture(config, registry, sid, f"Body for {v}.")

    texts = {
        "supported": "The sky is blue.",
        "partially_supported": "The moon is partly made of rock.",
        "not_supported": "The moon is made of cheese.",
        "not_verified": "Mercury is the closest planet to the sun.",
    }
    answer = "\n\n".join(f"{texts[v]} [{ids[v]}]" for v in verdict_values)
    paragraphs = split_paragraphs(answer)
    verdicts = [
        ParagraphVerdict(verdict=v, detail=f"Detail for {v}.", source_ids=[ids[v]])
        for v in verdict_values
    ]
    # Exhaustive over every value this test exercises, plus the one deterministic value it
    # deliberately does not (`no_sources_cited` — see docstring).
    assert set(verdict_values) | {"no_sources_cited"} == set(get_args(Verdict))
    outcome = RunOutcome(
        question="Marker rendering",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=VerificationResult(verdicts=verdicts),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    for v in verdict_values:
        label = v.replace("_", " ")
        assert f"Verdict: {label} - Detail for {v}." in answer_section
    # Exactly one Verdict: line per paragraph — the count assertion the old per-claim
    # marker count (`body.count("**[") == 5`) becomes under the new format.
    assert answer_section.count("Verdict:") == 4


def test_no_marker_or_markdown_link_ever_appears_inside_a_paragraphs_prose(make_config):
    """Item 1: no `[Sn]` marker and no markdown link survives inside prose — every link
    lives on its own `Sources:` line.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a")
    write_source_capture(config, registry, source_id, "Body.")
    answer = f"The pump failed under load [{source_id}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(verdict="supported", detail="Confirmed.", source_ids=[source_id])
        ]
    )
    outcome = RunOutcome(
        question="No inline links",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    # `_section` includes the blank line `_render_body` puts right after the `## Answer`
    # heading, so the prose is the first NON-empty line, not literally index 0.
    prose_line = next(line for line in answer_section.splitlines() if line.strip())
    assert prose_line == "The pump failed under load."
    assert "[S" not in prose_line
    assert "](" not in prose_line
    assert f"Sources: {registry.link(source_id)}" in answer_section


def test_sources_line_is_space_separated_deduped_first_appearance_order_before_verdict(
    make_config,
):
    """Item 2: `Sources:` lists space-separated, deduped links in first-appearance order,
    followed by `Verdict:`.
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Body one.")
    write_source_capture(config, registry, id2, "Body two.")
    answer = f"The pump [{id2}] failed [{id1}] again [{id2}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[ParagraphVerdict(verdict="supported", detail="Confirmed.", source_ids=[id2, id1])]
    )
    outcome = RunOutcome(
        question="Dedupe order",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    expected_sources_line = f"Sources: {registry.link(id2)} {registry.link(id1)}"
    assert expected_sources_line in answer_section
    assert answer_section.index("Sources:") < answer_section.index("Verdict:")


def test_write_report_resolves_registered_sources_and_discloses_unregistered_markers(
    make_config,
):
    """R1: a registered `[Sn]` marker resolves into a `Sources:` link — D4: links only
    ever appear on the `Sources:` line, never inline in prose. An unregistered marker's
    citation is stripped from the prose the same as any other marker (D4) and is
    disclosed by `_gaps_section`'s `unresolved_ids` scan of the RAW answer instead of
    being left visible in the answer text (the old per-claim scheme's behavior — see
    `## Discoveries`).
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/known", title="Known page")
    write_source_capture(config, registry, source_id)
    answer = "A known fact [S1].\n\nAn unresolvable fact [S9]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(verdict="supported", detail="Confirmed.", source_ids=[source_id]),
            ParagraphVerdict(verdict="no_sources_cited", detail="Nothing to check.", source_ids=[]),
        ]
    )
    outcome = RunOutcome(
        question="Citation resolution",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    answer_section = _section(body, "## Answer")
    assert "[S1]" not in answer_section  # stripped from prose; resolved onto a Sources: link
    assert "[S9]" not in answer_section  # stripped too — never left bare in the prose
    assert "example.test" in answer_section  # the registered source's Sources: link
    gaps = _section(body, "## Gaps and disclosures")
    assert "S9" in gaps


def test_write_report_conflicts_section_names_both_sources_with_no_winner(make_config):
    """D3: contradiction is now MODEL-REPORTED via `sources_conflict`, not derived from
    disagreeing per-source verdicts. The section identifies the paragraph and lists both
    cited sources as links, adjudicating nothing.
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://one.example.test/a", title="One")
    id2 = registry.add("https://two.example.test/b", title="Two")
    answer = f"The vendor quoted $4.20 per unit [{id1}] [{id2}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(
                verdict="not_supported",
                detail="The two sources disagree on the price.",
                sources_conflict=True,
                source_ids=[id1, id2],
            )
        ]
    )
    outcome = RunOutcome(
        question="Conflicting prices",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    section = _section(body, "## Conflicting sources")
    assert "The vendor quoted" in section
    assert registry.link(id1) in section
    assert registry.link(id2) in section
    lowered = section.lower()
    assert "correct" not in lowered
    assert "wrong" not in lowered
    assert "more reliable" not in lowered


def test_write_report_gaps_section_lists_check_failures(make_config):
    """The gaps section carries `check_failures` verbatim. The uncited-claim count this
    test used to also assert no longer exists (plan `## Reconciliations` 2026-08-13,
    second entry) — `_gaps_section` no longer denominates anything by claim count.
    """
    config = make_config()
    failure_line = "S3: model call raised TimeoutError"
    verification = VerificationResult(check_failures=[failure_line])
    outcome = RunOutcome(
        question="Gap disclosure",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    gaps = _section(body, "## Gaps and disclosures")
    assert failure_line in gaps
    # Proves the removal, not just its absence by accident.
    assert "claim(s)" not in gaps


def test_write_report_omits_both_new_sections_when_there_is_nothing_to_disclose(make_config):
    """`supported`, no conflict, no failures, no unresolved markers → neither new section
    appears.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a")
    write_source_capture(config, registry, source_id)
    answer = f"Everything checks out [{source_id}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(verdict="supported", detail="Confirmed.", source_ids=[source_id])
        ]
    )
    outcome = RunOutcome(
        question="Clean run",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "## Conflicting sources" not in body
    assert "## Gaps and disclosures" not in body


def test_write_report_a_list_paragraphs_lead_in_and_bullets_render_cleanly_with_bullet_marks(
    make_config,
):
    """A lead-in line plus its bullets is ONE paragraph and gets ONE Sources:/Verdict:
    pair; only the failing bullet carries a trailing `*`, and the detail opens with an
    `n/m bullets verified` rollup (item 3). Supersedes the old marker-placement guard
    against `extract_claims` collapsing whitespace: `outcome.paragraphs` is now the only
    source of paragraph boundaries (D2), so there is no text re-matching step left to
    fail — placement is correct by construction, not by locating a claim string back
    inside the raw answer.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/vendor")
    write_source_capture(config, registry, source_id, "Body text.")
    answer = f"Key findings:\n- The vendor quoted $4.20 [{source_id}].\n- Lead time is six weeks."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(
                verdict="partially_supported",
                detail="The quote does not match the source.",
                unsupported_items=[0],
                source_ids=[source_id],
            )
        ]
    )
    outcome = RunOutcome(
        question="Lead-in plus bullets",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")
    lines = [line.strip() for line in answer_section.splitlines()]

    assert "Key findings:" in answer_section
    # `strip_markers` preserves trailing punctuation, so the `*` mark appends after the
    # full rendered (already-punctuated) line, per B4: "append ` *` to the rendered line".
    assert "- The vendor quoted $4.20. *" in lines
    assert "- Lead time is six weeks." in lines
    assert (
        "Verdict: partially supported - 1/2 bullets verified. The quote does not match the source."
    ) in answer_section


def test_write_report_a_hard_wrapped_paragraph_renders_as_one_clean_prose_block(make_config):
    """A sentence hard-wrapped across two lines is still ONE `Paragraph` (blank-line
    delimited, not sentence-delimited), so its Sources:/Verdict: pair renders once,
    beneath the whole prose block — the wrap is not a splitting boundary.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/vendor")
    write_source_capture(config, registry, source_id, "Body text.")
    answer = f"The vendor quoted a price of\n$4.20 per unit [{source_id}]."
    paragraphs = split_paragraphs(answer)
    assert len(paragraphs) == 1  # the wrap is one paragraph, not two
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(
                verdict="not_supported", detail="The source disagrees.", source_ids=[source_id]
            )
        ]
    )
    outcome = RunOutcome(
        question="Hard-wrapped paragraph",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert "The vendor quoted a price of" in answer_section
    assert "$4.20 per unit" in answer_section
    assert answer_section.count("Verdict:") == 1
    assert "Verdict: not supported - The source disagrees." in answer_section


def test_a_paragraphs_verdict_binds_to_its_own_prose_not_the_next_paragraphs(make_config):
    """Found by the Phase 5 live check against the old sentence-marker scheme: a verdict
    separated from its own claim read as a label on whatever followed. Paragraphs make
    this structural now — each paragraph's Sources:/Verdict: pair sits directly beneath
    its own prose and before the next paragraph's prose begins.
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Body one.")
    write_source_capture(config, registry, id2, "Body two.")
    answer = f"The vendor quoted $4.20 [{id1}].\n\nLead time is six weeks [{id2}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(verdict="not_supported", detail="Says $5.10.", source_ids=[id1]),
            ParagraphVerdict(verdict="supported", detail="Confirmed.", source_ids=[id2]),
        ]
    )
    outcome = RunOutcome(
        question="Two paragraphs",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    first_verdict_pos = answer_section.index("Verdict: not supported - Says $5.10.")
    second_prose_pos = answer_section.index("Lead time is six weeks")
    second_verdict_pos = answer_section.index("Verdict: supported - Confirmed.")
    assert first_verdict_pos < second_prose_pos < second_verdict_pos


def test_a_paragraph_citing_multiple_sources_still_renders_exactly_one_verdict_line(
    make_config,
):
    """The model judges a paragraph's pooled sources together and returns ONE verdict
    (D3) — `report.py` never re-derives per-source noise from that; it renders exactly
    what it was given, once, no matter how many sources the paragraph cited. Successor to
    the old "one supporting source suppresses the others' markers" property, which was a
    `report.py`-level aggregation rule that no longer exists — that judgment now happens
    entirely inside the model's pooled verdict (`harness.verify`).
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Confirms the price.")
    write_source_capture(config, registry, id2, "Says nothing about price.")
    answer = f"The vendor quoted $4.20 [{id1}][{id2}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(
                verdict="supported", detail="One source confirms it.", source_ids=[id1, id2]
            )
        ]
    )
    outcome = RunOutcome(
        question="Partial coverage",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert answer_section.count("Verdict:") == 1
    assert "Verdict: supported - One source confirms it." in answer_section


def test_a_paragraph_no_source_supports_renders_the_models_single_not_supported_verdict(
    make_config,
):
    """The other half: when nothing supports the paragraph, the model's single
    `not_supported` verdict is what renders — `report.py` does not fan it out per source.
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Says $5.10.")
    write_source_capture(config, registry, id2, "Says nothing about price.")
    answer = f"The vendor quoted $4.20 [{id1}][{id2}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(
                verdict="not_supported",
                detail="Neither source confirms it.",
                source_ids=[id1, id2],
            )
        ]
    )
    outcome = RunOutcome(
        question="No support at all",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert answer_section.count("Verdict:") == 1
    assert "Verdict: not supported - Neither source confirms it." in answer_section


def test_an_out_of_range_unsupported_item_index_is_ignored_rather_than_raising(make_config):
    """Item 4: an out-of-range bullet index is ignored, not raised."""
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a")
    write_source_capture(config, registry, source_id, "Body.")
    answer = f"- First item [{source_id}]\n- Second item"
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(
                verdict="partially_supported",
                detail="One bullet is off.",
                unsupported_items=[0, 7],  # 7 is out of range for a two-item list
                source_ids=[source_id],
            )
        ]
    )
    outcome = RunOutcome(
        question="Out of range index",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")
    lines = [line.strip() for line in answer_section.splitlines()]

    assert "- First item *" in lines
    assert "- Second item" in lines
    assert "Verdict: partially supported - 1/2 bullets verified" in answer_section


def test_a_verdicts_list_shorter_than_paragraphs_renders_not_verified_for_the_rest(
    make_config,
):
    """Item 4: a `verdicts` list shorter than `paragraphs` renders `not verified` for the
    paragraphs with no corresponding entry, rather than raising an index error.
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Body one.")
    write_source_capture(config, registry, id2, "Body two.")
    answer = f"First paragraph [{id1}].\n\nSecond paragraph [{id2}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[ParagraphVerdict(verdict="supported", detail="Confirmed.", source_ids=[id1])]
    )
    outcome = RunOutcome(
        question="Short verdicts list",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert "Verdict: supported - Confirmed." in answer_section
    assert "Verdict: not verified - verification did not run for this paragraph." in answer_section


def test_write_report_discloses_skipped_verification_when_the_run_died(make_config):
    """3F Minor finding 5: when a run ends `cut_short == "error"`, `__main__` skips the
    verification pass rather than issuing near-certainly-failing model calls, and
    discloses the skip here rather than silently omitting it.
    """
    config = make_config()
    verification = VerificationResult(
        check_failures=[
            "verification skipped: the run ended in an error, so claims were not checked"
        ]
    )
    outcome = RunOutcome(
        question="What happened to the run?",
        answer="Partial finding before the crash.",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="error",
        cut_short_detail="APIConnectionError: getaddrinfo failed",
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    gaps = _section(body, "## Gaps and disclosures")
    assert "verification skipped" in gaps
    assert "the run ended in an error" in gaps


def test_write_report_with_verification_none_still_flags_a_citing_paragraph_as_not_verified(
    make_config,
):
    """Reshaped backwards-compatibility guard (item 4): `verification=None` no longer
    means "no markers, exactly the old plain report" — the Sources:/Verdict: gate is on
    whether a paragraph cites a REGISTERED source, never on whether verification ran. A
    citing paragraph with no verification pass still gets a `Verdict: not verified` line
    saying so, deterministically, never raising.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a")
    write_source_capture(config, registry, source_id)
    answer = f"Answer text with a marker [{source_id}]."
    paragraphs = split_paragraphs(answer)
    outcome = RunOutcome(
        question="Backwards compatibility",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=None,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert "Answer text with a marker" in answer_section
    assert f"[{source_id}]" not in answer_section
    assert "Verdict: not verified - verification did not run for this paragraph." in answer_section
    assert "## Conflicting sources" not in body
    assert "## Gaps and disclosures" not in body


def test_write_report_with_verification_none_and_no_registered_source_renders_prose_alone(
    make_config,
):
    """The other half of item 2's "a non-citing paragraph renders neither" — with no
    registered source there is no Sources:/Verdict: block at all, verification or not.
    """
    config = make_config()
    answer = "Answer text with no citation at all."
    paragraphs = split_paragraphs(answer)
    outcome = RunOutcome(
        question="No citation",
        answer=answer,
        registry=SourceRegistry(),
        usage=_usage(),
        paragraphs=paragraphs,
        verification=None,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert answer_section.strip() == "Answer text with no citation at all."
    assert "Verdict:" not in answer_section
    assert "Sources:" not in answer_section


def test_write_report_ignores_a_source_captured_under_a_different_run(make_config):
    """Regression test for the Drift (see the plan's `## Reconciliations` 2026-08-12 —
    Phase 6): a capture written under a DIFFERENT run's directory must never be read as
    this run's evidence, since `agent.workspace_dir` is one directory reused across runs.
    """
    config = make_config()
    other_run = SourceRegistry(run_id="2020-01-01-000000")
    this_run = SourceRegistry(run_id="2020-01-01-000001")
    source_id = this_run.add("https://example.test/only-captured-elsewhere", title="Example")
    write_source_capture(config, other_run, source_id)
    outcome = RunOutcome(
        question="Does another run's capture leak in?",
        answer=f"Should be unusable [{source_id}].",
        registry=this_run,
        usage=_usage(),
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert _UNUSABLE_HEADING in body
    heading_pos = body.index(_UNUSABLE_HEADING)
    bullet_pos = body.index(f"[{source_id}]", heading_pos)
    assert bullet_pos > heading_pos


# --- PR #4 review -----------------------------------------------------------------------


def test_working_notes_are_found_in_a_subdirectory(make_config):
    """The agent is given no path convention, and nested writes are legal.

    `harness/prompts/orchestrator.md` says only "Write findings into your workspace as you
    go", and deepagents' `FilesystemBackend.write` creates parent directories rather than
    rejecting `notes/pricing.md`. A top-level `*.md` glob therefore printed "no working
    notes were written" over a workspace holding this run's findings — in the one report
    where the reader has nothing else to fall back on.
    """
    config = make_config()
    nested = config.agent.workspace_dir / "notes"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "pricing.md").write_text("Acme quoted $4.20/unit.", encoding="utf-8")

    outcome = RunOutcome(
        question="What pricing was found before the cutoff?",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="wall_clock",
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Acme quoted $4.20/unit." in body
    assert _NO_NOTES_TEXT not in body
    # Named by its path under the workspace, so two same-named notes stay distinguishable.
    assert "notes/pricing.md" in body


def test_working_notes_are_not_restricted_to_markdown(make_config):
    """Nothing pins an extension either, so a `.txt` note is still this run's findings."""
    config = make_config()
    config.agent.workspace_dir.mkdir(parents=True, exist_ok=True)
    (config.agent.workspace_dir / "findings.txt").write_text(
        "Lead time is six weeks.", encoding="utf-8"
    )

    outcome = RunOutcome(
        question="What was found?",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="wall_clock",
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Lead time is six weeks." in body
    assert _NO_NOTES_TEXT not in body


def test_machine_written_bulk_still_never_reaches_the_notes_section(make_config):
    """Recursion must not undo what the old top-level glob got right by accident.

    `sources/` is captured page text and the other two are the summarizer's evicted
    history — none of it is the agent's own notes, and all of it is large.
    """
    config = make_config()
    workspace = config.agent.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.md").write_text("A real note.", encoding="utf-8")

    registry = SourceRegistry()
    write_source_capture(config, registry, "S1", "CAPTURED_PAGE_BODY")
    for directory, filename, text in (
        ("conversation_history", "thread.md", "EVICTED_HISTORY_BODY"),
        ("large_tool_results", "result.md", "OFFLOADED_RESULT_BODY"),
    ):
        target = workspace / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / filename).write_text(text, encoding="utf-8")

    outcome = RunOutcome(
        question="Does machine-written bulk leak into the notes?",
        answer="",
        registry=registry,
        usage=_usage(),
        cut_short="wall_clock",
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "A real note." in body
    assert "CAPTURED_PAGE_BODY" not in body
    assert "EVICTED_HISTORY_BODY" not in body
    assert "OFFLOADED_RESULT_BODY" not in body


def test_dead_branches_are_disclosed_on_a_run_that_finished_normally(make_config):
    """R4 names dead branches unconditionally, not only for a cut-short run.

    An agent that simply stops with steps still `pending` has abandoned those branches
    just as surely as one the wall clock killed, and the reader was told nothing at all.
    """
    config = make_config()
    todos = [
        {"content": "Check the delivery claim against a second source", "status": "pending"},
        {"content": "Summarize the vendor comparison", "status": "completed"},
    ]
    outcome = RunOutcome(
        question="Which vendor is cheapest?",
        answer="Acme quoted $4.20.",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short=None,
        todos=todos,
        verification=VerificationResult(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert _CUT_SHORT_HEADING not in body, "this run was not cut short"
    gaps = _section(body, "## Gaps and disclosures")
    assert "Check the delivery claim against a second source" in gaps
    assert "Summarize the vendor comparison" not in gaps


def test_a_cut_short_run_does_not_list_its_dead_branches_twice(make_config):
    """`## Run cut short` already lists them; the gaps section must not repeat them."""
    config = make_config()
    todos = [{"content": "Check the delivery claim", "status": "pending"}]
    outcome = RunOutcome(
        question="Which vendor is cheapest?",
        answer="Partial answer.",
        registry=SourceRegistry(),
        usage=_usage(),
        cut_short="wall_clock",
        todos=todos,
        verification=VerificationResult(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert body.count("Check the delivery claim") == 1


def test_each_cut_short_reason_names_only_its_own_bound(make_config, tmp_path):
    """The protection `report.py`'s `_ROUND_CAP_TEXT` comment promises a reviewer.

    That comment claims the tests assert one phrase present AND another absent, so a
    swapped `except` label in `__main__` cannot slip past. No test actually did that
    (PR #4 review, Minor) — the existing bound tests assert on the configured NUMBER, and
    a branch rendering "the wall clock (configured at 17 rounds per pass)" passed both.
    """
    agent = AgentSettings(workspace_dir=tmp_path / "workspace", reports_dir=tmp_path / "reports")
    config = make_config(agent=agent)
    phrases = {
        "round_cap": _ROUND_CAP_TEXT,
        "wall_clock": _WALL_CLOCK_TEXT,
        "error": _ERROR_TEXT,
    }

    for reason, expected in phrases.items():
        outcome = RunOutcome(
            question=f"Which bound ended the {reason} run?",
            answer="Partial answer.",
            registry=SourceRegistry(),
            usage=_usage(),
            cut_short=reason,
            cut_short_detail="APIConnectionError: boom" if reason == "error" else None,
        )
        body = write_report(outcome, config).read_text(encoding="utf-8")
        section = _section(body, _CUT_SHORT_HEADING)

        assert expected in section, f"{reason} did not name its own bound"
        for other_reason, other in phrases.items():
            if other_reason != reason:
                assert other not in section, f"{reason} also named {other_reason}'s bound"
