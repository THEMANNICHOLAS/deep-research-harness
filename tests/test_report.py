"""Behavioral tests for harness.report. Pure string work: no model, no network.

`RunOutcome`'s field names (`question`, `answer`, `registry`, `usage`) are not pinned by
the plan's Contracts section — only the class name, `write_report`'s signature, and the
frozen filename format are. This suite fixes those field names as part of writing the
tests first; `harness/report.py` must match them.
"""

import re

import pytest
from pydantic import ValidationError

from harness.report import _UNUSABLE_HEADING, RunOutcome, write_report
from harness.sources import SourceRegistry
from harness.tools.fetch import FETCH_FAILED_PREFIX, _sources_dir

_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}-[a-z0-9-]+\.md$")


def _usage(reasoning: int = 0, input_tokens: int = 100, output_tokens: int = 50) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "output_token_details": {"reasoning": reasoning},
    }


def _write_usable_source_file(config, source_id: str) -> None:
    """Write a real, `fetched`-shaped capture — `report.py` reads this to judge usability."""
    sources_dir = _sources_dir(config)
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / f"{source_id}.md").write_text(
        f"# {source_id}: Example page\n\n- Outcome: fetched\n\nSome captured body text.",
        encoding="utf-8",
    )


def _write_stub_source_file(config, source_id: str, outcome: str = "error") -> None:
    """Write a failure stub — the shape `harness/tools/fetch.py` writes for a bad fetch."""
    sources_dir = _sources_dir(config)
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
    _write_usable_source_file(config, source_id)
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
    _write_stub_source_file(config, dead_id, outcome="blocked")
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
    _write_usable_source_file(config, good_id)
    _write_stub_source_file(config, bad_id, outcome="timeout")
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
    from harness.config import AgentSettings

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
    _write_usable_source_file(config, source_id)
    outcome = RunOutcome(
        question="reasoning split check",
        answer="Answer text with a marker [S1].",
        registry=registry,
        usage=_usage(reasoning=37, input_tokens=200, output_tokens=80),
    )

    path = write_report(outcome, config)
    body = path.read_text(encoding="utf-8")

    assert "Answer text with a marker [S1]." in body
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
