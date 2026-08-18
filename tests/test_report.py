"""Behavioral tests for harness.report. Pure string work: no model, no network.

`RunOutcome`'s field names are pinned by this suite, not by the plan — only the class name,
`write_report`'s signature and the filename format are frozen there.

`outcome.paragraphs` is the ONLY source of paragraph boundaries for `## Answer` (D2), so every
test that checks Answer content passes `paragraphs=split_paragraphs(answer)` explicitly, exactly
as `harness/__main__.py` does.
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
    _DIGESTED_HEADING,
    _ERROR_TEXT,
    _FALLBACK_HEADING,
    _NO_ANSWER_TEXT,
    _NO_NOTES_TEXT,
    _NOTES_HEADING,
    _READ_MODES_HEADING,
    _ROUND_CAP_TEXT,
    _UNREAD_HEADING,
    _UNUSABLE_HEADING,
    _WALL_CLOCK_TEXT,
    RunOutcome,
    write_report,
)
from harness.runlog import Incident
from harness.sources import SourceRegistry, sources_dir
from harness.verify import ParagraphVerdict, VerificationResult
from tests.conftest import write_failed_capture, write_source_capture, write_workspace_note

_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}-[a-z0-9-]+\.md$")

# An mtime far enough back that no test run can straddle it: "written by a previous run".
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
    # The question's own words must survive into the filename, not just satisfy the shape.
    assert "melting" in path.name
    assert "tungsten" in path.name


def test_write_report_run_metadata_names_both_configured_models(make_config):
    """Every role gets its own named line (R6) even when configured to the same model, so this
    asserts on the labeled lines rather than on the model string appearing somewhere.
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
    assert "- Researcher Model: test-model" in body
    assert "- Reader Model: test-model" in body
    assert "- Verifier Model: test-model" in body


def test_write_report_run_metadata_reads_each_role_from_its_own_config_entry(make_config):
    """The falsifiable half of R6: with the roles configured to DIFFERENT models, each line must
    carry its own. Rendering the head model twice passes the test above and fails this one.
    """
    config = make_config(head_model="lead-model-x", reader_model="worker-model-y")
    outcome = RunOutcome(
        question="What is the boiling point of water?",
        answer="100 C at sea level.",
        registry=SourceRegistry(),
        usage=_usage(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "- Lead Model: lead-model-x" in body
    assert "- Reader Model: worker-model-y" in body
    assert "- Reader Model: lead-model-x" not in body


def test_write_report_returns_a_path_whose_body_is_the_answer_actually_written(make_config):
    """`[S1]` is unregistered here, so it is stripped like any other marker (D4) rather than left
    bare, while the prose around it survives verbatim.
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
    # Asserted on the rendered text, not on a flag anywhere in `RunOutcome`.
    assert "no usable sources" in body.lower()


def test_write_report_with_usable_sources_does_not_claim_it_has_none(make_config):
    """The paired negative for the test above: without it, a stub that always prints "no usable
    sources" would pass. Same shape, but the registry holds a source with a real capture, so the
    phrase must be absent and the source listed instead.
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

    `fetch.py` registers every attempted URL, including 404s and blocked pages, so a registry of
    nothing but stubs must still say plainly that no usable sources were found.
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
    # The stub is listed under the "not usable" heading, never as a bullet ahead of it:
    # position, not presence, is what proves it was not counted as evidence.
    no_sources_pos = body.lower().index("no usable sources")
    heading_pos = body.index(_UNUSABLE_HEADING)
    dead_bullet_pos = body.index(f"[{dead_id}]")
    assert no_sources_pos < heading_pos < dead_bullet_pos


def test_write_report_survives_a_capture_file_that_is_not_valid_utf8(make_config):
    """A truncated capture must cost one source, never the whole report.

    `write_text` dying mid-flush leaves a byte prefix that can end mid-character, and
    `UnicodeDecodeError` is a `ValueError`, so it escaped `_is_usable` and `write_report` — which
    `__main__` does not guard — throwing away a run that had already spent everything.
    """
    config = make_config()
    registry = SourceRegistry()
    good_id = registry.add("https://example.test/good", title=None)
    torn_id = registry.add("https://example.test/torn", title=None)
    write_source_capture(config, registry, good_id)
    torn_path = sources_dir(config, registry) / f"{torn_id}.md"
    # A valid UTF-8 prefix cut mid-character, as an aborted flush would leave it.
    torn_path.write_bytes(b"# S2: torn page\n\n- Outcome: fetched\n\ncaf\xc3")
    outcome = RunOutcome(
        question="What survived?",
        answer="The good source did [S1].",
        registry=registry,
        usage=_usage(),
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert "example.test/good" in body
    # Unreadable is treated like a stub: disclosed as not usable, never as evidence.
    unusable_pos = body.index(_UNUSABLE_HEADING)
    assert unusable_pos < body.index(f"[{torn_id}]")


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
    # Position, not presence: the usable source before the "not usable" heading, the stub after.
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

    # Every REGISTERED marker is stripped from the prose (R1), but the raw marker must not
    # survive. Stripping is mechanical (D4) and runs whether or not verification did.
    answer_section = _section(body, "## Answer")
    assert "Answer text with a marker" in answer_section
    assert "[S1]" not in answer_section
    # The reasoning-token count must be visible on its own: a bare total misprices a
    # reasoning model's run.
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

    Isolates a section by content rather than by line offset, so these tests stay correct
    wherever a section lands relative to its neighbors.
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
    # The untouched `AgentSettings` default (20) would mean the configured value was ignored.
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
    registry = SourceRegistry()
    write_workspace_note(
        config, registry, "notes.md", "Vendor Acme quoted $4.20/unit, confirmed by two listings."
    )
    # A capture written straight into the run's `sources/`, the way `fetch.py` does, so nothing
    # but the notes section could surface its text.
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
    # `sources/` is excluded, so this capture body must not appear in the notes section.
    assert "Some captured body text." not in body
    # Guards a stub that always claims "no notes" regardless of what is on disk.
    assert _NO_NOTES_TEXT not in body


def test_headings_inside_working_notes_are_demoted_like_answer_prose(make_config):
    """R3's one-H1 rule covers the whole report, not just `## Answer`.

    Notes were embedded verbatim, so a cut-short run whose note opened with `# Pricing`
    put a second H1 under the report title and broke the section ordering.
    """
    config = make_config()
    registry = SourceRegistry()
    write_workspace_note(
        config, registry, "notes.md", "# Pricing findings\n\n## Vendors\n\nAcme quoted $4.20/unit."
    )
    outcome = RunOutcome(
        question="What pricing was found before the cutoff?",
        answer="",
        registry=registry,
        usage=_usage(),
        cut_short="wall_clock",
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert "### Pricing findings" in body
    assert "#### Vendors" in body
    assert "\n# Pricing findings" not in body
    assert "\n## Vendors" not in body
    # The report's own title is the only H1 in the whole document.
    assert [line for line in body.split("\n") if line.startswith("# ")] == [
        "# What pricing was found before the cutoff?"
    ]


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
    """Presenting a PREVIOUS run's notes as this run's findings overstates the evidence (R3) in a
    way the reader cannot catch. Per-run workspaces are the first defense; this pins the second,
    which still matters because `SourceRegistry(run_id=...)` lets a run id be reused.
    """
    config = make_config()
    registry = SourceRegistry()
    stale = write_workspace_note(
        config, registry, "old-run.md", "Acme was cheapest, from last week's run."
    )
    os.utime(stale, (_LONG_AGO, _LONG_AGO))

    started_at = datetime.now()
    write_workspace_note(config, registry, "this-run.md", "Beta quoted $9.10/unit today.")

    outcome = RunOutcome(
        question="What did this run actually find?",
        answer="",
        registry=registry,
        usage=_usage(),
        cut_short="wall_clock",
        started_at=started_at,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Beta quoted $9.10/unit today." in body
    assert "Acme was cheapest, from last week's run." not in body


def test_write_report_keeps_every_note_when_no_start_time_is_known(make_config):
    """`started_at=None` means "unknown", which keeps notes: losing this run's findings is the
    worse of the two failures.
    """
    config = make_config()
    registry = SourceRegistry()
    old = write_workspace_note(config, registry, "whenever.md", "A finding of unknown vintage.")
    os.utime(old, (_LONG_AGO, _LONG_AGO))

    outcome = RunOutcome(
        question="What is on disk?",
        answer="",
        registry=registry,
        usage=_usage(),
        cut_short="round_cap",
        started_at=None,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "A finding of unknown vintage." in body


def test_write_report_says_so_when_the_run_produced_no_answer(make_config):
    """A cut-short run usually has no prose answer, and an empty `## Answer` reads as "the answer
    is nothing" — the reader must be told the run never got that far.
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

# `test_write_report_renders_a_verdict_line_for_every_citing_paragraph_regardless_of_verdict`
# (per-paragraph `Verdict:` line rendered for every verdict value) is removed: Phase 2 Step 5
# stops rendering the per-paragraph `Sources:`/`Verdict:` pair entirely, so its whole subject
# no longer exists. Superseded by
# `test_no_per_paragraph_sources_or_verdict_lines_and_reviewer_paragraph_under_sources` below.


def test_no_marker_or_markdown_link_ever_appears_inside_a_paragraphs_prose(make_config):
    """No `[Sn]` marker and no markdown link survives inside prose: every link lives on its own
    `Sources:` line.
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

    # The first non-empty line is the paragraph-number prefix; the prose follows it.
    prose_line = next(
        line for line in answer_section.splitlines() if line.strip() and line != "**1.**"
    )
    assert prose_line == "The pump failed under load."
    assert "[S" not in prose_line
    assert "](" not in prose_line


def test_a_citation_only_paragraph_does_not_leave_a_blank_paragraph_gap(make_config):
    """A citation-only paragraph strips to no visible text at all (report.py's `_paragraph_block`
    returns `""` for it) -- `_answer_section`'s `"\\n\\n".join` must not turn that empty block
    into a stray blank paragraph between its neighbors (3F review issue 2).
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Body one.")
    write_source_capture(config, registry, id2, "Body two.")
    answer = f"First paragraph [{id1}].\n\n[{id2}]\n\nThird paragraph [{id1}]."
    paragraphs = split_paragraphs(answer)
    assert len(paragraphs) == 3  # sanity: the citation-only line is its own paragraph
    outcome = RunOutcome(
        question="Citation-only gap",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert "\n\n\n" not in answer_section
    # The dropped paragraph takes no number either: its neighbors renumber as 1 and 2.
    assert answer_section.strip() == "**1.**\nFirst paragraph.\n\n**2.**\nThird paragraph."


# `test_sources_line_is_space_separated_deduped_first_appearance_order_before_verdict` is
# removed: its whole subject was the per-paragraph `Sources:` line's dedup/ordering, which
# Phase 2 Step 5 no longer renders at all.


# `test_the_sources_verdict_pair_is_separated_from_its_prose_by_a_blank_line` and
# `test_a_citation_only_paragraph_opens_with_its_sources_line_not_a_blank_one` are removed:
# both tested formatting of the per-paragraph `Sources:`/`Verdict:` pair, which Phase 2 Step 5
# no longer renders at all.


def test_write_report_resolves_registered_sources_and_discloses_unregistered_markers(
    make_config,
):
    """A registered marker never survives inline (R1/D4): it is stripped from the prose like
    any other. An unregistered marker is stripped too, and disclosed instead by
    `_gaps_section`'s `unresolved_ids` scan of the RAW answer.
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
    assert "[S1]" not in answer_section  # stripped from prose
    assert "[S9]" not in answer_section  # stripped too — never left bare in the prose
    sources_section = _section(body, "## Sources")
    assert "example.test" in sources_section  # the registered source's link
    gaps = _section(body, "## Gaps and disclosures")
    assert "S9" in gaps


def test_write_report_conflicts_section_names_both_sources_with_no_winner(make_config):
    """Contradiction is MODEL-REPORTED via `sources_conflict`, not derived (D3). The section
    identifies the paragraph and lists both cited sources, adjudicating nothing.
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
    # The model's `detail` is the only statement of WHAT they disagree about.
    assert "The two sources disagree on the price." in section
    lowered = section.lower()
    assert "correct" not in lowered
    assert "wrong" not in lowered
    assert "more reliable" not in lowered


def test_conflicts_section_survives_a_paragraph_that_is_nothing_but_a_marker(make_config):
    """Covers the guard `_conflicts_section` carries: a paragraph whose text is only `[S1]` is
    truthy raw and empty once stripped, so taking its first line would raise `IndexError`.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://one.example.test/a", title="One")
    answer = f"[{source_id}]"
    paragraphs = split_paragraphs(answer)
    outcome = RunOutcome(
        question="A paragraph that is only a citation",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=VerificationResult(
            verdicts=[
                ParagraphVerdict(
                    verdict="not_supported",
                    detail="The two sources disagree on the price.",
                    sources_conflict=True,
                    source_ids=[source_id],
                )
            ]
        ),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    section = _section(body, "## Conflicting sources")
    assert registry.link(source_id) in section
    assert "The two sources disagree on the price." in section


def test_write_report_gaps_section_lists_check_failures(make_config):
    """The gaps section carries `check_failures` verbatim, denominating nothing by claim count."""
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
    """`supported`, no conflict, no failures, no unresolved markers: neither section appears."""
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


# --- Phase 2 Step 5: consolidated reviewer summary --------------------------------------


def test_no_per_paragraph_sources_or_verdict_lines_and_reviewer_paragraph_under_sources(
    make_config,
):
    """The per-paragraph `**Sources:**`/`**Verdict:**` pair no longer renders inside `## Answer`
    at all; the consolidated reviewer paragraph appears under `## Sources` instead.
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
        ],
        reviewer_summary="One paragraph was checked and fully supported.",
    )
    outcome = RunOutcome(
        question="Reviewer summary placement",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")
    sources_section = _section(body, "## Sources")

    assert "**Sources:**" not in answer_section
    assert "**Verdict:**" not in answer_section
    assert "One paragraph was checked and fully supported." in sources_section


def test_answer_paragraphs_are_numbered_to_match_the_reviewer_paragraph(make_config):
    """The reviewer paragraph names claims by paragraph number (`verify_summary.md`), so
    `## Answer` must actually show those numbers — without them the number is a pointer the
    reader can only resolve by hand-counting (PR review finding 4). Numbered by
    `renders_content`, matching `_format_verdicts_block`: a citation-only paragraph renders
    nothing and takes no number; a fenced code block takes one.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a")
    write_source_capture(config, registry, source_id)
    answer = (
        f"First finding [{source_id}].\n\n"
        f"[{source_id}]\n\n"
        f"Second finding [{source_id}].\n\n"
        "```python\nprint('hi')\n```"
    )
    outcome = RunOutcome(
        question="Numbered paragraphs",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=split_paragraphs(answer),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert "**1.**\nFirst finding." in answer_section
    # The citation-only paragraph renders nothing, so the next visible paragraph is 2 — the
    # same count `_format_verdicts_block` hands the reviewer model.
    assert "**2.**\nSecond finding." in answer_section
    assert "**3.**" in answer_section  # the code block counts too (renders_content is True)
    assert "**4.**" not in answer_section


def test_reviewer_summary_headings_are_demoted_under_sources(make_config):
    """The reviewer paragraph is model-authored prose like answer paragraphs and workspace
    notes, so its headings are demoted the same way — a model-written `# ` line must not
    collide with the report's own title/section depths (PR review finding 5).
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a")
    write_source_capture(config, registry, source_id)
    answer = f"Everything checks out [{source_id}]."
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(verdict="supported", detail="Confirmed.", source_ids=[source_id])
        ],
        reviewer_summary="# Review\n\nAll claims held up.",
    )
    outcome = RunOutcome(
        question="Demoted reviewer heading",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=split_paragraphs(answer),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    sources_section = _section(body, "## Sources")

    assert "### Review" in sources_section
    assert not any(line.startswith("# Review") for line in sources_section.splitlines())


def test_a_none_reviewer_summary_renders_sources_exactly_as_before(make_config):
    """The best-effort regression guard (D-D): a run whose consolidation never ran or failed
    (`reviewer_summary=None`) must render `## Sources` with no stray heading or blank artifact
    beyond the plain source list.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a", title="Example A")
    write_source_capture(config, registry, source_id)
    answer = f"Everything checks out [{source_id}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[
            ParagraphVerdict(verdict="supported", detail="Confirmed.", source_ids=[source_id])
        ],
        reviewer_summary=None,
    )
    outcome = RunOutcome(
        question="No reviewer summary",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    sources_section = _section(body, "## Sources")

    assert sources_section.strip() == f"- [{source_id}] {registry.link(source_id)}"


def test_write_report_a_list_paragraphs_lead_in_and_bullets_render_cleanly_with_bullet_marks(
    make_config,
):
    """A lead-in plus its bullets is ONE paragraph; only the failing bullet carries a trailing
    `*`. Placement is by construction, since `outcome.paragraphs` is the only source of
    boundaries (D2) and no text re-matching step remains.
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
    # `strip_markers` keeps trailing punctuation, so the `*` appends after the rendered line.
    assert "- The vendor quoted $4.20. *" in lines
    assert "- Lead time is six weeks." in lines


def test_a_lead_in_repeating_the_first_bullet_does_not_steal_its_unsupported_mark(make_config):
    """The `*` is placed by list-item POSITION, not by matching rendered text. A lead-in ending in
    the first bullet's own wording consumed that bullet's slot, so the mark landed on prose and the
    failing bullet rendered clean — pointing the reader at the wrong line.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/vendor")
    write_source_capture(config, registry, source_id, "Body text.")
    answer = (
        "Summary: the vendor quoted $4.20.\n"
        f"- the vendor quoted $4.20 [{source_id}].\n"
        "- Lead time is six weeks."
    )
    paragraphs = split_paragraphs(answer)
    outcome = RunOutcome(
        question="Lead-in colliding with its first bullet",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=VerificationResult(
            verdicts=[
                ParagraphVerdict(
                    verdict="partially_supported",
                    detail="The quote does not match the source.",
                    unsupported_items=[0],
                    source_ids=[source_id],
                )
            ]
        ),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    lines = [line.strip() for line in _section(body, "## Answer").splitlines()]

    assert "Summary: the vendor quoted $4.20." in lines, "the lead-in must not be marked"
    assert "- the vendor quoted $4.20. *" in lines, "the failing bullet must carry the mark"
    assert "- Lead time is six weeks." in lines


def test_a_citation_only_bullet_renders_no_line_and_is_left_out_of_the_rollup(make_config):
    """`- [S1]` is a bullet with nothing in it — it must render no line at all, not a
    contentless `-`.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/vendor")
    write_source_capture(config, registry, source_id, "Body text.")
    answer = f"- The vendor quoted $4.20 [{source_id}].\n- [{source_id}]"
    paragraphs = split_paragraphs(answer)
    outcome = RunOutcome(
        question="A list with a citation-only bullet",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=VerificationResult(
            verdicts=[
                ParagraphVerdict(
                    verdict="not_supported",
                    detail="The capture quotes a different price.",
                    unsupported_items=[0],
                    source_ids=[source_id],
                )
            ]
        ),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")
    lines = [line.strip() for line in answer_section.splitlines()]

    assert "- The vendor quoted $4.20. *" in lines
    assert "-" not in lines, "a citation-only bullet must render no line at all"
    assert "*" not in lines, "and no orphaned unsupported mark either"


# `test_a_list_that_was_never_verified_carries_no_bullets_verified_rollup` is removed: its
# whole subject was the `n/m bullets verified` rollup on the per-paragraph `Verdict:` line,
# which Phase 2 Step 5 no longer renders at all.


def test_a_fenced_code_block_reaches_the_answer_verbatim(make_config):
    """R2 removes MARKERS from prose, not CONTENT. A fence is excluded from the verification unit,
    but dropping it from the report deleted a command or config sample with no disclosure —
    the silent thinning the invariant forbids.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/docs")
    write_source_capture(config, registry, source_id, "Body text.")
    answer = f"Run the migration first [{source_id}].\n\n```bash\nuv run alembic upgrade head\n```"
    paragraphs = split_paragraphs(answer)
    outcome = RunOutcome(
        question="How do I migrate?",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=VerificationResult(
            verdicts=[
                ParagraphVerdict(
                    verdict="supported", detail="The page says so.", source_ids=[source_id]
                ),
                ParagraphVerdict(verdict="no_sources_cited", detail="Nothing to check."),
            ]
        ),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert "```bash\nuv run alembic upgrade head\n```" in answer_section


def test_write_report_a_hard_wrapped_paragraph_renders_as_one_clean_prose_block(make_config):
    """A hard-wrapped sentence is still ONE `Paragraph` — blank-line delimited, not
    sentence-delimited — so it renders as a single, unsplit prose block.
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
    # The real assertion (3F review issue 4): a substring check alone would still pass if the
    # wrap were split into two blank-line-separated blocks. Blocks are `\n\n`-joined
    # (`_answer_section`), so a real split shows up as a second entry here.
    blocks = [block.strip() for block in answer_section.split("\n\n") if block.strip()]
    assert len(blocks) == 1
    assert blocks[0] == "**1.**\nThe vendor quoted a price of\n$4.20 per unit."


# `test_a_paragraphs_verdict_binds_to_its_own_prose_not_the_next_paragraphs`,
# `test_a_paragraph_citing_multiple_sources_still_renders_exactly_one_verdict_line`, and
# `test_a_paragraph_no_source_supports_renders_the_models_single_not_supported_verdict` are
# removed: each one's whole subject was the per-paragraph `Verdict:` line's placement or
# content, which Phase 2 Step 5 no longer renders at all.


def test_an_out_of_range_unsupported_item_index_is_ignored_rather_than_raising(make_config):
    """An out-of-range bullet index is ignored, not raised."""
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


def test_a_verdicts_list_shorter_than_paragraphs_does_not_raise(make_config):
    """A `verdicts` list shorter than `paragraphs` must not raise an index error — the
    mismatch itself is disclosed separately under `## Gaps and disclosures`
    (`test_a_verdict_paragraph_count_mismatch_is_disclosed`).
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

    assert "First paragraph" in answer_section
    assert "Second paragraph" in answer_section


def test_write_report_discloses_skipped_verification_when_the_run_died(make_config):
    """When a run ends `cut_short == "error"`, `__main__` skips the verification pass rather than
    issuing near-certainly-failing model calls, and discloses the skip here.
    """
    config = make_config()
    verification = VerificationResult(
        check_failures=[
            "verification skipped: the run ended in an error, so claims were not checked"
        ]
    )
    answer = "Partial finding before the crash."
    outcome = RunOutcome(
        question="What happened to the run?",
        answer=answer,
        registry=SourceRegistry(),
        usage=_usage(),
        paragraphs=split_paragraphs(answer),
        cut_short="error",
        cut_short_detail="APIConnectionError: getaddrinfo failed",
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    gaps = _section(body, "## Gaps and disclosures")
    assert "verification skipped" in gaps
    assert "the run ended in an error" in gaps


def test_write_report_with_verification_none_still_strips_the_marker(make_config):
    """`verification=None` does not mean "the old plain report": marker stripping (R1) runs
    unconditionally, whether or not verification ran.
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
    assert "## Conflicting sources" not in body
    assert "## Gaps and disclosures" not in body


def test_write_report_with_verification_none_and_no_registered_source_renders_prose_alone(
    make_config,
):
    """A non-citing paragraph renders neither line: with no registered source there is no
    Sources:/Verdict: block at all, verification or not.
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

    assert answer_section.strip() == "**1.**\nAnswer text with no citation at all."
    assert "Verdict:" not in answer_section
    assert "Sources:" not in answer_section


def test_write_report_ignores_a_source_captured_under_a_different_run(make_config):
    """A capture written under a DIFFERENT run's directory must never be read as this run's
    evidence, since `agent.workspace_dir` is one directory reused across runs.
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
    """The agent is given no path convention, and nested writes are legal: the prompt says only
    "Write findings into your workspace as you go", and `FilesystemBackend.write` creates parents
    for `notes/pricing.md`. A top-level `*.md` glob printed "no working notes were written" over a
    workspace holding this run's findings, in the one report with nothing else to fall back on.
    """
    config = make_config()
    registry = SourceRegistry()
    write_workspace_note(config, registry, "notes/pricing.md", "Acme quoted $4.20/unit.")

    outcome = RunOutcome(
        question="What pricing was found before the cutoff?",
        answer="",
        registry=registry,
        usage=_usage(),
        cut_short="wall_clock",
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Acme quoted $4.20/unit." in body
    assert _NO_NOTES_TEXT not in body
    # Named by its path under the workspace, so two same-named notes stay distinguishable.
    assert "notes/pricing.md" in body


def test_a_concurrent_runs_notes_never_reach_this_report(make_config):
    """`started_at` cannot separate two runs in flight at once: the other run's files are NEWER
    than this run's start, so the mtime filter keeps them. Per-run subdirectories hold them apart.

    `started_at=None` disables the mtime filter, so a pass here proves the directory boundary did
    the work and not the timestamp.
    """
    config = make_config()
    this_run = SourceRegistry()
    other_run = SourceRegistry()
    write_workspace_note(config, this_run, "mine.md", "Beta quoted $9.10/unit.")
    write_workspace_note(config, other_run, "theirs.md", "AN UNRELATED QUESTION'S FINDING.")

    outcome = RunOutcome(
        question="What did this run find?",
        answer="",
        registry=this_run,
        usage=_usage(),
        cut_short="wall_clock",
        started_at=None,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Beta quoted $9.10/unit." in body
    assert "AN UNRELATED QUESTION'S FINDING." not in body


def test_working_notes_are_not_restricted_to_markdown(make_config):
    """Nothing pins an extension either, so a `.txt` note is still this run's findings."""
    config = make_config()
    registry = SourceRegistry()
    write_workspace_note(config, registry, "findings.txt", "Lead time is six weeks.")

    outcome = RunOutcome(
        question="What was found?",
        answer="",
        registry=registry,
        usage=_usage(),
        cut_short="wall_clock",
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Lead time is six weeks." in body
    assert _NO_NOTES_TEXT not in body


def test_machine_written_bulk_still_never_reaches_the_notes_section(make_config):
    """Recursion must not undo what the old top-level glob got right by accident: `sources/` is
    captured page text and the other two are evicted history — none of it the agent's own notes.
    """
    config = make_config()
    registry = SourceRegistry()
    write_workspace_note(config, registry, "notes.md", "A real note.")
    write_source_capture(config, registry, "S1", "CAPTURED_PAGE_BODY")
    write_workspace_note(config, registry, "conversation_history/thread.md", "EVICTED_HISTORY_BODY")
    write_workspace_note(config, registry, "large_tool_results/result.md", "OFFLOADED_RESULT_BODY")

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
    """R4 names dead branches unconditionally, not only for a cut-short run: an agent that stops
    with steps still `pending` has abandoned them just as surely as one the wall clock killed.
    """
    config = make_config()
    todos = [
        {"content": "Check the delivery claim against a second source", "status": "pending"},
        {"content": "Summarize the vendor comparison", "status": "completed"},
    ]
    answer = "Acme quoted $4.20."
    outcome = RunOutcome(
        question="Which vendor is cheapest?",
        answer=answer,
        registry=SourceRegistry(),
        usage=_usage(),
        paragraphs=split_paragraphs(answer),
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
    answer = "Partial answer."
    outcome = RunOutcome(
        question="Which vendor is cheapest?",
        answer=answer,
        registry=SourceRegistry(),
        usage=_usage(),
        paragraphs=split_paragraphs(answer),
        cut_short="wall_clock",
        todos=todos,
        verification=VerificationResult(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert body.count("Check the delivery claim") == 1


def test_each_cut_short_reason_names_only_its_own_bound(make_config, tmp_path):
    """The protection `report.py`'s `_ROUND_CAP_TEXT` comment promises: one phrase present AND
    another absent, so a swapped `except` label in `__main__` cannot slip past. The bound tests
    above assert only on the configured NUMBER, which "the wall clock (configured at 17 rounds per
    pass)" would satisfy.
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


# --- Phase 3 (reader delegation): read-mode disclosure -----------------------------------


def test_write_report_all_digested_run_discloses_a_single_summary_line(make_config):
    """R5 wants digestion observable even when nothing fell back — a run with no exceptions
    still gets one line saying every source was actually read via the reader.
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1)
    write_source_capture(config, registry, id2)
    registry.mark_read(id1, "digested")
    registry.mark_read(id2, "digested")
    outcome = RunOutcome(
        question="All digested",
        answer="",
        registry=registry,
        usage=_usage(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert _READ_MODES_HEADING in body
    section = _section(body, _READ_MODES_HEADING)
    assert "2" in section
    assert "digest" in section.lower()
    # No bucket headings on the all-digested path — just the summary line.
    assert _DIGESTED_HEADING not in section
    assert _FALLBACK_HEADING not in section
    assert _UNREAD_HEADING not in section


def test_write_report_mixed_read_modes_bucket_each_source_correctly(make_config):
    """Digested, fallback, and unread (a failed capture) sources each land in their own
    bucket, in a stable order.
    """
    config = make_config()
    registry = SourceRegistry()
    digested_id = registry.add("https://example.test/digested")
    fallback_id = registry.add("https://example.test/fallback")
    failed_id = registry.add("https://example.test/failed")
    write_source_capture(config, registry, digested_id)
    write_source_capture(config, registry, fallback_id)
    write_failed_capture(config, registry, failed_id, outcome="blocked")
    registry.mark_read(digested_id, "digested")
    registry.mark_read(fallback_id, "fallback")
    # failed_id is left at its default "unread" — fetch.py never marks a failed capture read.
    outcome = RunOutcome(
        question="Mixed read modes",
        answer="",
        registry=registry,
        usage=_usage(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    section = _section(body, _READ_MODES_HEADING)

    assert _DIGESTED_HEADING in section
    assert _FALLBACK_HEADING in section
    assert _UNREAD_HEADING in section
    digested_pos = section.index(_DIGESTED_HEADING)
    fallback_pos = section.index(_FALLBACK_HEADING)
    unread_pos = section.index(_UNREAD_HEADING)
    assert digested_pos < fallback_pos < unread_pos
    assert f"[{digested_id}]" in section[digested_pos:fallback_pos]
    assert f"[{fallback_id}]" in section[fallback_pos:unread_pos]
    assert f"[{failed_id}]" in section[unread_pos:]


def test_write_report_read_mode_disclosure_ignores_undigested_markers_in_capture_body(
    make_config,
):
    """D4: the disclosure buckets by `registry`'s `read_mode` field, never by parsing
    `<undigested>` markers out of a capture file's body. A capture whose body happens to
    literally contain the closing marker string must still bucket by what the registry
    recorded, since marker bodies are unescaped page text and cannot be trusted to parse.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/marked")
    write_source_capture(
        config,
        registry,
        source_id,
        '<undigested source="S1" reason="test">page text</undigested>',
    )
    registry.mark_read(source_id, "digested")
    outcome = RunOutcome(
        question="Marker text inside a digested capture",
        answer="",
        registry=registry,
        usage=_usage(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    section = _section(body, _READ_MODES_HEADING)

    assert "digest" in section.lower()
    assert _FALLBACK_HEADING not in section
    assert _UNREAD_HEADING not in section


def test_write_report_omits_the_read_modes_section_with_no_registered_sources(make_config):
    config = make_config()
    outcome = RunOutcome(
        question="No sources at all",
        answer="",
        registry=SourceRegistry(),
        usage=_usage(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert _READ_MODES_HEADING not in body


def test_incidents_render_under_gaps_even_when_verification_never_ran(make_config):
    config = make_config()
    outcome = RunOutcome(
        question="What failed?",
        answer="An answer with no citations.",
        registry=SourceRegistry(),
        usage=_usage(),
        verification=None,
        incidents=[
            Incident(kind="search_failed", detail='search for "solar" failed: unreachable'),
            Incident(kind="fetch_failed", detail="[S1] https://a.test: blocked - status 403"),
        ],
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "## Gaps and disclosures" in body
    assert "Tool failures during the run:" in body
    assert '- search for "solar" failed: unreachable' in body
    assert "- [S1] https://a.test: blocked - status 403" in body


def test_no_incidents_renders_no_tool_failures_heading(make_config):
    config = make_config()
    outcome = RunOutcome(
        question="Anything?",
        answer="An answer with no citations.",
        registry=SourceRegistry(),
        usage=_usage(),
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Tool failures during the run:" not in body


def test_a_verdict_paragraph_count_mismatch_is_disclosed(make_config):
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a")
    write_source_capture(config, registry, source_id)
    answer = f"First claim [{source_id}].\n\nSecond claim [{source_id}]."
    paragraphs = split_paragraphs(answer)
    # One verdict for two paragraphs: the overflow paragraph silently renders "not verified",
    # which the report must say out loud rather than let read as a deliberate verdict.
    verification = VerificationResult(
        verdicts=[ParagraphVerdict(verdict="supported", detail="ok", source_ids=[source_id])]
    )
    outcome = RunOutcome(
        question="Mismatch?",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Verification returned 1 verdict(s) for 2 paragraph(s)" in body


def test_matching_verdict_and_paragraph_counts_are_not_flagged(make_config):
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/a")
    write_source_capture(config, registry, source_id)
    answer = f"Only claim [{source_id}]."
    paragraphs = split_paragraphs(answer)
    verification = VerificationResult(
        verdicts=[ParagraphVerdict(verdict="supported", detail="ok", source_ids=[source_id])]
    )
    outcome = RunOutcome(
        question="Match?",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "Verification returned" not in body


# --- Phase 1 Step 3: report structure enforcement (heading demotion) -------------------


def test_model_authored_headings_are_demoted_inside_the_answer(make_config):
    """A model-authored `#`/`##`/`###` heading is demoted by two levels each, so nothing
    inside `## Answer` can collide with the report's own H1 title or H2 section headings.
    Relative depth ordering among the three levels is preserved.
    """
    config = make_config()
    registry = SourceRegistry()
    answer = "# Title\n\nSome intro prose.\n\n## Section\n\nBody text.\n\n### Sub\n\nMore text."
    paragraphs = split_paragraphs(answer)
    outcome = RunOutcome(
        question="What headings does a model write?",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert "### Title" in answer_section
    assert "#### Section" in answer_section
    assert "##### Sub" in answer_section
    for line in answer_section.splitlines():
        assert not line.startswith("# "), line
        assert not line.startswith("## "), line


def test_a_heading_inside_a_fenced_code_block_is_not_demoted(make_config):
    """A `# comment` inside a fence is content, not structure — demotion must not touch it,
    and the fence must stay byte-identical (mirrors
    `test_a_fenced_code_block_reaches_the_answer_verbatim`).
    """
    config = make_config()
    registry = SourceRegistry()
    answer = "Some prose.\n\n```python\n# comment\nprint('hi')\n```"
    paragraphs = split_paragraphs(answer)
    outcome = RunOutcome(
        question="Does a fenced heading-looking comment survive?",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")
    answer_section = _section(body, "## Answer")

    assert "```python\n# comment\nprint('hi')\n```" in answer_section


def test_a_model_authored_title_and_meta_answer_renders_with_exactly_one_h1(make_config):
    """Representative of the shape observed live on 2026-08-15: the model's answer opened
    with its own `# <title>`, had body sections, and closed with a model-authored
    `## Coverage`-style meta section. The literal saved answer text was not retrievable
    locally (reports live on the homelab); this reproduces the observed shape, not the
    verbatim text. After demotion the ENTIRE report body must contain exactly one
    `# `-prefixed line: the harness's own report title.
    """
    config = make_config()
    registry = SourceRegistry()
    answer = (
        "# Tungsten melting point\n\n"
        "Tungsten melts at 3422 C.\n\n"
        "## Background\n\n"
        "It has the highest melting point of any metal.\n\n"
        "## Coverage\n\n"
        "All sources were checked and no gaps were found."
    )
    paragraphs = split_paragraphs(answer)
    outcome = RunOutcome(
        question="What is the melting point of tungsten?",
        answer=answer,
        registry=registry,
        usage=_usage(),
        paragraphs=paragraphs,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    h1_lines = [line for line in body.splitlines() if line.startswith("# ")]
    assert len(h1_lines) == 1
    assert h1_lines[0] == "# What is the melting point of tungsten?"
