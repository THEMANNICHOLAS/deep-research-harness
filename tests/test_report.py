"""Behavioral tests for harness.report. Pure string work: no model, no network.

`RunOutcome`'s field names (`question`, `answer`, `registry`, `usage`) are not pinned by
the plan's Contracts section — only the class name, `write_report`'s signature, and the
frozen filename format are. This suite fixes those field names as part of writing the
tests first; `harness/report.py` must match them.
"""

import os
import re
from datetime import datetime

import pytest
from pydantic import ValidationError

from harness.config import AgentSettings
from harness.report import (
    _CUT_SHORT_HEADING,
    _NO_ANSWER_TEXT,
    _NO_NOTES_TEXT,
    _NOTES_HEADING,
    _UNUSABLE_HEADING,
    RunOutcome,
    write_report,
)
from harness.sources import SourceRegistry
from harness.tools.fetch import FETCH_FAILED_PREFIX, _sources_dir

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


def _write_usable_source_file(config, registry, source_id: str) -> None:
    """Write a real, `fetched`-shaped capture under `registry`'s run directory.

    `report.py` reads this to judge usability. Takes `registry` (Phase 6 amendment —
    see the plan's `## Reconciliations` 2026-08-12 — Phase 6) so the file lands under
    `sources/<run_id>/`, never the flat `sources/` layout Phases 2-5 used.
    """
    sources_dir = _sources_dir(config, registry)
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / f"{source_id}.md").write_text(
        f"# {source_id}: Example page\n\n- Outcome: fetched\n\nSome captured body text.",
        encoding="utf-8",
    )


def _write_stub_source_file(config, registry, source_id: str, outcome: str = "error") -> None:
    """Write a failure stub — the shape `harness/tools/fetch.py` writes for a bad fetch."""
    sources_dir = _sources_dir(config, registry)
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / f"{source_id}.md").write_text(
        f"{FETCH_FAILED_PREFIX}{outcome}\n", encoding="utf-8"
    )


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


def test_write_report_returns_a_path_whose_body_is_the_answer_actually_written(make_config):
    config = make_config()
    outcome = RunOutcome(
        question="Is the sky blue?",
        answer="Yes, due to Rayleigh scattering [S1].",
        registry=SourceRegistry(),
        usage=_usage(),
    )

    path = write_report(outcome, config)

    assert path.is_file()
    assert "Yes, due to Rayleigh scattering [S1]." in path.read_text(encoding="utf-8")


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
    _write_usable_source_file(config, registry, source_id)
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
    _write_stub_source_file(config, registry, dead_id, outcome="blocked")
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
    _write_usable_source_file(config, registry, good_id)
    _write_stub_source_file(config, registry, bad_id, outcome="timeout")
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
    _write_usable_source_file(config, registry, source_id)
    outcome = RunOutcome(
        question="reasoning split check",
        answer="Answer text with a marker [S1].",
        registry=registry,
        usage=_usage(reasoning=37, input_tokens=200, output_tokens=80),
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
    _write_usable_source_file(config, registry, "S1")
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


# --- Phase 6: claim verification and disclosure ----------------------------------------
#
# `harness.verify` does not exist yet (Phase 6 builds it next) — its classes are imported
# locally inside each test that needs them so a missing module fails only that test, not
# collection of this whole file (per the plan's "Expected red" section).


def test_write_report_marks_each_non_supported_verdict_and_leaves_supported_bare(make_config):
    """Every verdict in the frozen vocabulary renders a visible marker except `supported`."""
    from harness.verify import ClaimCheck, VerificationResult

    config = make_config()
    supported = "The sky is blue."
    unsupported = "The moon is made of cheese."
    uncited = "Water boils at 100 degrees Celsius."
    unresolved = "Mercury is the closest planet to the sun."
    unverifiable = "Venus has a thick atmosphere."
    answer = " ".join([supported, unsupported, uncited, unresolved, unverifiable])
    verification = VerificationResult(
        checks=[
            ClaimCheck(claim=supported, source_id="S1", verdict="supported"),
            ClaimCheck(
                claim=unsupported, source_id="S1", verdict="unsupported", detail="disagrees"
            ),
            ClaimCheck(claim=uncited, source_id=None, verdict="uncited"),
            ClaimCheck(claim=unresolved, source_id="S9", verdict="unresolved"),
            ClaimCheck(
                claim=unverifiable,
                source_id="S2",
                verdict="unverifiable",
                detail="fetch failed",
            ),
        ]
    )
    outcome = RunOutcome(
        question="Marker rendering",
        answer=answer,
        registry=SourceRegistry(),
        usage=_usage(),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "**[unsupported — S1]**" in body
    assert "**[uncited]**" in body
    assert "**[unresolved — S9]**" in body
    assert "**[unverifiable — S2]**" in body
    # Exactly the four non-supported claims carry a marker — the supported one does not.
    assert body.count("**[") == 4


def test_write_report_resolves_registered_markers_and_discloses_unregistered_ones(
    make_config,
):
    """R1: every surviving `[Sn]` marker resolves to a link; an unresolvable one is
    reported by `unresolved_ids` in the gaps section instead.
    """
    from harness.verify import ClaimCheck, VerificationResult

    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/known", title="Known page")
    _write_usable_source_file(config, registry, source_id)
    answer = "A known fact [S1]. An unresolvable fact [S9]."
    verification = VerificationResult(
        checks=[
            ClaimCheck(claim="A known fact [S1].", source_id="S1", verdict="supported"),
            ClaimCheck(claim="An unresolvable fact [S9].", source_id="S9", verdict="unresolved"),
        ]
    )
    outcome = RunOutcome(
        question="Citation resolution",
        answer=answer,
        registry=registry,
        usage=_usage(),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    # Scoped to the Answer section, not the whole body: `## Sources` legitimately prints
    # `- [S1] <link>` as its bullet label (pinned by
    # `test_write_report_ignores_a_source_captured_under_a_different_run`), so a
    # whole-body assertion could never pass. What matters is that no bare marker survives
    # where the reader is reading prose.
    answer_section = _section(body, "## Answer")
    assert "[S1]" not in answer_section  # resolved into a markdown link, not left bare
    assert "example.test" in answer_section
    assert "[S9]" in answer_section  # unregistered — left visible verbatim
    gaps = _section(body, "## Gaps and disclosures")
    assert "S9" in gaps


def test_write_report_conflicts_section_names_both_positions_with_no_winner(make_config):
    """D3: a conflict states both positions and both IDs, and adjudicates nothing."""
    from harness.verify import ClaimCheck, Conflict, VerificationResult

    config = make_config()
    registry = SourceRegistry()
    registry.add("https://one.example.test/a", title="One")
    registry.add("https://two.example.test/b", title="Two")
    claim = "The vendor quoted $4.20 per unit [S1] [S2]."
    positions = [
        ClaimCheck(
            claim=claim, source_id="S1", verdict="supported", detail="Source A confirms $4.20"
        ),
        ClaimCheck(
            claim=claim, source_id="S2", verdict="unsupported", detail="Source B says $5.10"
        ),
    ]
    verification = VerificationResult(
        checks=positions, conflicts=[Conflict(claim=claim, positions=positions)]
    )
    outcome = RunOutcome(
        question="Conflicting prices",
        answer=claim,
        registry=registry,
        usage=_usage(),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    section = _section(body, "## Conflicting sources")
    assert "S1" in section
    assert "S2" in section
    assert "Source A confirms $4.20" in section
    assert "Source B says $5.10" in section
    lowered = section.lower()
    assert "correct" not in lowered
    assert "wrong" not in lowered
    assert "more reliable" not in lowered


def test_write_report_gaps_section_lists_check_failures_and_uncited_count(make_config):
    """The gaps section carries `check_failures` verbatim and the uncited claim count."""
    from harness.verify import ClaimCheck, VerificationResult

    config = make_config()
    failure_line = "S3: model call raised TimeoutError"
    verification = VerificationResult(
        checks=[
            ClaimCheck(
                claim="First unsupported-by-citation claim.", source_id=None, verdict="uncited"
            ),
            ClaimCheck(
                claim="Second unsupported-by-citation claim.",
                source_id=None,
                verdict="uncited",
            ),
        ],
        check_failures=[failure_line],
    )
    outcome = RunOutcome(
        question="Gap disclosure",
        answer="First unsupported-by-citation claim. Second unsupported-by-citation claim.",
        registry=SourceRegistry(),
        usage=_usage(),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    gaps = _section(body, "## Gaps and disclosures")
    assert failure_line in gaps
    assert "2" in gaps
    assert "uncited" in gaps.lower()


def test_write_report_omits_both_new_sections_when_there_is_nothing_to_disclose(make_config):
    """All-`supported`, no conflicts, no failures → neither new section appears."""
    from harness.verify import ClaimCheck, VerificationResult

    config = make_config()
    verification = VerificationResult(
        checks=[ClaimCheck(claim="Everything checks out.", source_id="S1", verdict="supported")]
    )
    outcome = RunOutcome(
        question="Clean run",
        answer="Everything checks out.",
        registry=SourceRegistry(),
        usage=_usage(),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "## Conflicting sources" not in body
    assert "## Gaps and disclosures" not in body


def test_write_report_places_marker_for_a_lead_in_plus_bullets_claim(make_config):
    """3F Major finding: `extract_claims` joins a bulleted block's lines with a single
    space, so the returned claim is NOT a verbatim substring of the answer (which still
    has its bullet markers and newlines). The marker must still land in the rendered
    Answer section rather than silently vanishing.
    """
    from harness.verify import ClaimCheck, VerificationResult, extract_claims

    config = make_config()
    answer = "Key findings:\n- The vendor quoted $4.20 [S1].\n- Lead time is six weeks [S2]."
    claims = extract_claims(answer)
    target = next(claim for claim in claims if "vendor quoted" in claim)
    verification = VerificationResult(
        checks=[ClaimCheck(claim=target, source_id="S1", verdict="unsupported", detail="mismatch")]
    )
    outcome = RunOutcome(
        question="Lead-in plus bullets",
        answer=answer,
        registry=SourceRegistry(),
        usage=_usage(),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "**[unsupported — S1]**" in body


def test_write_report_places_marker_for_a_hard_wrapped_claim(make_config):
    """Same finding, the other shape: a claim hard-wrapped across two lines within one
    paragraph is collapsed to a single space by `extract_claims`, so it is not a verbatim
    substring of the answer either.
    """
    from harness.verify import ClaimCheck, VerificationResult, extract_claims

    config = make_config()
    answer = "The vendor quoted a price of\n$4.20 per unit [S1]. Lead time is six weeks [S2]."
    claims = extract_claims(answer)
    target = next(claim for claim in claims if "vendor quoted a price" in claim)
    verification = VerificationResult(
        checks=[ClaimCheck(claim=target, source_id="S1", verdict="unsupported", detail="mismatch")]
    )
    outcome = RunOutcome(
        question="Hard-wrapped claim",
        answer=answer,
        registry=SourceRegistry(),
        usage=_usage(),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    assert "**[unsupported — S1]**" in body


def test_write_report_discloses_a_verdict_whose_marker_could_not_be_placed(make_config):
    """A claim that genuinely does not appear in the answer at all (e.g. a stale check
    against an edited answer) must be disclosed in `## Gaps and disclosures`, never
    dropped — this is the "never drop silently" half of the Major finding.
    """
    from harness.verify import ClaimCheck, VerificationResult

    config = make_config()
    answer = "The sky is blue."
    verification = VerificationResult(
        checks=[
            ClaimCheck(
                claim="A claim that does not appear in the answer at all.",
                source_id="S1",
                verdict="unsupported",
                detail="mismatch",
            )
        ]
    )
    outcome = RunOutcome(
        question="Unplaceable claim",
        answer=answer,
        registry=SourceRegistry(),
        usage=_usage(),
        verification=verification,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    # Never silently placed somewhere wrong in the answer.
    assert "**[" not in body
    gaps = _section(body, "## Gaps and disclosures")
    assert "unsupported" in gaps
    assert "S1" in gaps
    assert "could not be positioned" in gaps


def test_write_report_discloses_skipped_verification_when_the_run_died(make_config):
    """3F Minor finding 5: when a run ends `cut_short == "error"`, `__main__` skips the
    verification pass rather than issuing near-certainly-failing model calls, and
    discloses the skip here rather than silently omitting it.
    """
    from harness.verify import VerificationResult

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


def test_write_report_with_verification_none_renders_unchanged(make_config):
    """Backwards-compatibility guard: an unset `verification` renders exactly the report
    every prior phase already produces — no markers, no new sections.
    """
    config = make_config()
    outcome = RunOutcome(
        question="Backwards compatibility",
        answer="Answer text with a marker [S1].",
        registry=SourceRegistry(),
        usage=_usage(),
        verification=None,
    )

    body = write_report(outcome, config).read_text(encoding="utf-8")

    # The registry is empty, so `[S1]` is UNREGISTERED and `resolve()` leaves it alone by
    # design — this line pins "unknown markers survive verbatim", NOT "no resolution
    # happens when verification is None". Resolution is unconditional; see
    # `test_write_report_body_carries_answer_reasoning_split_and_sources`.
    assert "Answer text with a marker [S1]." in body
    assert "**[" not in body
    assert "## Conflicting sources" not in body
    assert "## Gaps and disclosures" not in body


def test_write_report_ignores_a_source_captured_under_a_different_run(make_config):
    """Regression test for the Drift (see the plan's `## Reconciliations` 2026-08-12 —
    Phase 6): a capture written under a DIFFERENT run's directory must never be read as
    this run's evidence, since `agent.workspace_dir` is one directory reused across runs.
    """
    config = make_config()
    other_run = SourceRegistry(run_id="2020-01-01-000000")
    this_run = SourceRegistry(run_id="2020-01-01-000001")
    source_id = this_run.add("https://example.test/only-captured-elsewhere", title="Example")
    _write_usable_source_file(config, other_run, source_id)
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
