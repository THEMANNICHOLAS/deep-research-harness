"""Behavioral tests for harness.verify — claim extraction, per-claim checks, conflicts.

`harness.verify` does not exist yet — Phase 6 builds it next. Importing it here is
deliberate collection-error red (see the plan's "Expected red" section); the assertions
below are written against the module's frozen contract so a stub `verify_claims` still
fails them on content, not just on import.
"""

import asyncio
import re
from typing import get_args

from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr, SecretStr

from harness.sources import SourceRegistry
from harness.verify import Conflict, Verdict, extract_claims, verify_claims
from tests.conftest import (
    ScriptedChatModel,
    verify_reply,
    write_failed_capture,
    write_source_capture,
)


def _flatten(messages) -> str:
    """Join a call's message contents into one string, for substring assertions."""
    return " ".join(str(getattr(m, "content", "")) for m in messages)


async def test_every_verdict_in_the_frozen_vocabulary_is_reachable(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    supported_id = registry.add("https://example.test/tungsten-melting-point")
    unsupported_id = registry.add("https://example.test/oven-specs")
    failed_id = registry.add("https://example.test/dead-source")
    silent_id = registry.add("https://example.test/kiln-catalogue")
    write_source_capture(config, registry, supported_id, "Tungsten melts at 3422 degrees Celsius.")
    write_source_capture(config, registry, unsupported_id, "The oven only reaches 1200 Celsius.")
    write_failed_capture(config, registry, failed_id, outcome="blocked")
    write_source_capture(config, registry, silent_id, "Our kilns ship in three colours.")

    supported_claim = "Tungsten melts at 3422 degrees Celsius [S1]."
    unsupported_claim = "The oven can easily melt tungsten [S2]."
    not_addressed_claim = "Tungsten is used in light bulb filaments [S4]."
    uncited_claim = "Tungsten is a very hard metal."
    unresolved_claim = "Tungsten was discovered in 1781 [S9]."
    unverifiable_claim = "Tungsten has the highest melting point of any metal [S3]."
    answer = " ".join(
        [
            supported_claim,
            unsupported_claim,
            not_addressed_claim,
            uncited_claim,
            unresolved_claim,
            unverifiable_claim,
        ]
    )

    model = scripted_model(
        [
            verify_reply("supported", "Matches the source exactly."),
            verify_reply("unsupported", "The oven falls short of tungsten's melting point."),
            verify_reply("not_addressed", "The catalogue says nothing about filaments."),
        ]
    )
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(answer, config, registry)

    by_claim = {check.claim: check for check in result.checks}
    assert by_claim[supported_claim].verdict == "supported"
    assert by_claim[supported_claim].source_id == "S1"
    assert by_claim[unsupported_claim].verdict == "unsupported"
    assert by_claim[unsupported_claim].source_id == "S2"
    assert by_claim[not_addressed_claim].verdict == "not_addressed"
    assert by_claim[not_addressed_claim].source_id == "S4"
    assert by_claim[uncited_claim].verdict == "uncited"
    assert by_claim[uncited_claim].source_id is None
    assert by_claim[unresolved_claim].verdict == "unresolved"
    assert by_claim[unresolved_claim].source_id == "S9"
    assert by_claim[unverifiable_claim].verdict == "unverifiable"
    assert by_claim[unverifiable_claim].source_id == "S3"
    # Exhaustive by construction: a verdict added later fails here until it gets a case.
    assert {check.verdict for check in result.checks} == set(get_args(Verdict))


async def test_a_check_sees_only_its_own_sources_captured_text(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "UNIQUE_MARKER_ONE: source one body text.")
    write_source_capture(config, registry, id2, "UNIQUE_MARKER_TWO: source two body text.")
    answer = f"Claim about one [{id1}]. Claim about two [{id2}]."

    model = scripted_model([verify_reply("supported", "ok"), verify_reply("supported", "ok")])
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    await verify_claims(answer, config, registry)

    assert len(model._received_messages) == 2
    first_call_text = _flatten(model._received_messages[0])
    second_call_text = _flatten(model._received_messages[1])
    assert "UNIQUE_MARKER_ONE" in first_call_text
    assert "UNIQUE_MARKER_TWO" not in first_call_text
    assert "UNIQUE_MARKER_TWO" in second_call_text
    assert "UNIQUE_MARKER_ONE" not in second_call_text


async def test_a_check_never_fetches(make_config, scripted_model, monkeypatch):
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/page")
    write_source_capture(config, registry, source_id, "Captured content already on disk.")
    answer = f"A claim about the page [{source_id}]."

    class _ExplodingCrawler:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("verification must never fetch — D10/R8")

    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", _ExplodingCrawler)

    model = scripted_model([verify_reply("supported", "matches")])
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(answer, config, registry)

    assert result.checks[0].verdict == "supported"


class _ConcurrencyTrackingModel(ScriptedChatModel):
    """Tracks in-flight `_agenerate` calls to prove the verification loop is sequential.

    The `asyncio.sleep(0)` is load-bearing (D4, plan test 4): without a real await
    point, a concurrent `asyncio.gather` would still show a peak of 1, since nothing
    would ever yield control between increment and decrement.
    """

    _in_flight: int = PrivateAttr(default=0)
    _peak_in_flight: int = PrivateAttr(default=0)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._in_flight += 1
        self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
        await asyncio.sleep(0)
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        finally:
            self._in_flight -= 1


async def test_claims_are_checked_one_at_a_time(make_config, monkeypatch):
    config = make_config()
    registry = SourceRegistry()
    ids = [registry.add(f"https://example.test/page-{i}") for i in range(3)]
    for source_id in ids:
        write_source_capture(config, registry, source_id, f"Body text for {source_id}.")
    answer = " ".join(f"Claim {i} about the page [{source_id}]." for i, source_id in enumerate(ids))

    model = _ConcurrencyTrackingModel(
        model="test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([verify_reply("supported", "ok")] * len(ids))
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(answer, config, registry)

    assert model._peak_in_flight == 1
    assert len(model._received_messages) == len(ids)
    assert len(result.checks) == len(ids)


class _RaisingOnSecondCallModel(ScriptedChatModel):
    """Raises on its second call only, recording every call it was actually asked to make."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        call_index = self._call_count
        self._received_messages.append(list(messages))
        self._call_count += 1
        if call_index == 1:
            raise RuntimeError("simulated model outage")
        response = self._script[call_index]
        return ChatResult(generations=[ChatGeneration(message=response)])


async def test_one_failing_check_does_not_fail_the_pass(make_config, monkeypatch):
    config = make_config()
    registry = SourceRegistry()
    ids = [registry.add(f"https://example.test/page-{i}") for i in range(3)]
    for source_id in ids:
        write_source_capture(config, registry, source_id, f"Body text for {source_id}.")
    answer = " ".join(f"Claim {i} about the page [{source_id}]." for i, source_id in enumerate(ids))

    model = _RaisingOnSecondCallModel(
        model="test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([verify_reply("supported", "ok")] * len(ids))
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(answer, config, registry)

    # Every claim was still attempted — the failure did not stop the loop.
    assert len(model._received_messages) == 3
    verdicts = [check.verdict for check in result.checks]
    assert verdicts.count("supported") == 2
    assert verdicts.count("unverifiable") == 1
    failing_check = next(c for c in result.checks if c.verdict == "unverifiable")
    assert failing_check.detail is not None
    assert "simulated model outage" in failing_check.detail
    assert len(result.check_failures) == 1
    assert "simulated model outage" in result.check_failures[0]


async def test_disagreeing_sources_produce_a_conflict_with_no_adjudication(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Source one says the price is $4.20.")
    write_source_capture(config, registry, id2, "Source two says the price is $5.10.")
    claim = f"The vendor quoted $4.20 per unit [{id1}] [{id2}]."

    model = scripted_model(
        [
            verify_reply("supported", "Confirms $4.20."),
            verify_reply("unsupported", "Says $5.10 instead."),
        ]
    )
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(claim, config, registry)

    assert len(result.checks) == 2
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.claim == claim
    ids_seen = {position.source_id for position in conflict.positions}
    assert ids_seen == {id1, id2}
    verdicts_seen = {position.verdict for position in conflict.positions}
    assert verdicts_seen == {"supported", "unsupported"}
    details_seen = {position.detail for position in conflict.positions}
    assert details_seen == {"Confirms $4.20.", "Says $5.10 instead."}
    # `extra="forbid"` on `Conflict` pins its fields to `claim`/`positions` only — there
    # is no attribute anywhere that could name a winner.
    assert set(Conflict.model_fields) == {"claim", "positions"}


async def test_two_sources_on_one_sentence_both_supported_yields_no_conflict(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Source one confirms the $4.20 price.")
    write_source_capture(config, registry, id2, "Source two also confirms the $4.20 price.")
    claim = f"The vendor quoted $4.20 per unit [{id1}] [{id2}]."

    model = scripted_model(
        [verify_reply("supported", "Confirms."), verify_reply("supported", "Also confirms.")]
    )
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(claim, config, registry)

    assert len(result.checks) == 2
    assert result.conflicts == []
    assert {c.claim for c in result.checks} == {claim}
    assert {c.source_id for c in result.checks} == {id1, id2}
    assert all(c.verdict == "supported" for c in result.checks)


async def test_a_silent_source_is_not_addressed_and_never_makes_a_conflict(
    make_config, scripted_model, monkeypatch
):
    """Found by the Phase 6 live check. A synthesized sentence typically cites several
    sources, each covering part of it. Reading "this source does not establish the claim"
    as disagreement made the report state, in the harness's own voice, that sources
    disagreed when one had simply said nothing on the point — the same false-confidence
    failure D3 refuses, pointed the other way.
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Source one confirms the $4.20 price.")
    write_source_capture(config, registry, id2, "Source two discusses shipping lead times.")
    claim = f"The vendor quoted $4.20 per unit [{id1}] [{id2}]."

    model = scripted_model(
        [
            verify_reply("supported", "Confirms $4.20."),
            verify_reply("not_addressed", "Says nothing about price."),
        ]
    )
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(claim, config, registry)

    by_source = {check.source_id: check for check in result.checks}
    assert by_source[id1].verdict == "supported"
    assert by_source[id2].verdict == "not_addressed"
    # Silence is not disagreement.
    assert result.conflicts == []


async def test_a_contradiction_alongside_a_silent_source_still_conflicts(
    make_config, scripted_model, monkeypatch
):
    """The other half: `not_addressed` must not SUPPRESS a real conflict either. One
    source confirming, one contradicting and one silent is still a genuine disagreement,
    and the silent source is listed so the reader sees the full picture.
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    id3 = registry.add("https://example.test/three")
    for source_id, text in (
        (id1, "Source one says the price is $4.20."),
        (id2, "Source two says the price is $5.10."),
        (id3, "Source three is about lead times."),
    ):
        write_source_capture(config, registry, source_id, text)
    claim = f"The vendor quoted $4.20 per unit [{id1}] [{id2}] [{id3}]."

    model = scripted_model(
        [
            verify_reply("supported", "Confirms $4.20."),
            verify_reply("unsupported", "Says $5.10 instead."),
            verify_reply("not_addressed", "Says nothing about price."),
        ]
    )
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(claim, config, registry)

    assert len(result.conflicts) == 1
    positions = result.conflicts[0].positions
    assert {p.source_id for p in positions} == {id1, id2, id3}


# --- 3F fix pass: direct unit tests for extract_claims (Minor finding 2) ----------------


def test_extract_claims_removes_fenced_code_blocks_entirely():
    answer = "Here is a snippet.\n\n```python\nprint('not a claim')\n```\n\nDone talking."

    claims = extract_claims(answer)

    assert not any("not a claim" in claim for claim in claims)
    assert not any("print(" in claim for claim in claims)
    assert claims == ["Here is a snippet.", "Done talking."]


def test_extract_claims_drops_markdown_headings():
    answer = "## Findings\n\nThe vendor confirmed the price."

    claims = extract_claims(answer)

    assert claims == ["The vendor confirmed the price."]
    assert not any("Findings" in claim for claim in claims)


def test_extract_claims_strips_list_markers():
    answer = "- The vendor quoted $4.20.\n- Lead time is six weeks.\n1) A third item."

    claims = extract_claims(answer)

    assert claims == ["The vendor quoted $4.20.", "Lead time is six weeks.", "A third item."]


def test_extract_claims_splits_multiple_sentences_on_terminal_punctuation():
    answer = "The vendor quoted $4.20. Is that final? Yes! Confirmed."

    claims = extract_claims(answer)

    assert claims == ["The vendor quoted $4.20.", "Is that final?", "Yes!", "Confirmed."]


def test_extract_claims_handles_a_blank_line_separated_multi_block_answer():
    answer = "First block sentence one. First block sentence two.\n\nSecond block sentence."

    claims = extract_claims(answer)

    assert claims == [
        "First block sentence one.",
        "First block sentence two.",
        "Second block sentence.",
    ]


def test_extract_claims_drops_a_sentence_with_no_alphanumeric_content():
    answer = "A real claim here. ---. Another real claim."

    claims = extract_claims(answer)

    assert claims == ["A real claim here.", "Another real claim."]
    assert not any(claim.strip("- ") == "" for claim in claims)


# --- PR #4 review: block splitting is decided per LINE, not per block -------------------


def test_a_lead_in_above_a_list_does_not_glue_onto_the_first_bullet():
    """The Blocker's headline shape: `Key findings:` directly above bullets, no blank line.

    The old rule needed EVERY line in the block to be a bullet, so one lead-in flipped the
    whole block into "join it all with spaces" and the first claim came out as
    `Key findings: - The vendor quoted $4.20 [S1].`
    """
    answer = "Key findings:\n- The vendor quoted $4.20 [S1].\n- Lead time is six weeks [S2]."

    claims = extract_claims(answer)

    assert claims == ["The vendor quoted $4.20 [S1].", "Lead time is six weeks [S2]."]
    assert not any("Key findings" in claim for claim in claims)
    assert not any(claim.startswith("-") for claim in claims)


def test_unpunctuated_bullets_under_a_lead_in_stay_one_claim_each():
    """The worse variant: without terminal punctuation the old rule merged the whole list.

    Nothing then split it, because the sentence splitter needs `.`/`!`/`?`, so ONE claim
    came out carrying every source ID — and `verify_claims` asked each source to support
    the others' facts. Asserting one ID per claim is what pins that shut.
    """
    answer = "Key findings:\n- The vendor quoted $4.20 [S1]\n- Lead time is six weeks [S2]"

    claims = extract_claims(answer)

    assert claims == ["The vendor quoted $4.20 [S1]", "Lead time is six weeks [S2]"]
    for claim in claims:
        assert len(re.findall(r"\[S\d+\]", claim)) == 1, claim


def test_a_heading_directly_above_a_list_drops_only_the_heading_line():
    """A heading with no blank line under it used to take the whole block with it.

    Those claims were never checked AND never disclosed — silent, which is worse than a
    wrong verdict. Only the heading LINE is dropped now.
    """
    answer = "## Findings\n- Solar grew 40% [S1]\n- Wind fell 3% [S2]"

    claims = extract_claims(answer)

    assert claims == ["Solar grew 40% [S1]", "Wind fell 3% [S2]"]
    assert not any("Findings" in claim for claim in claims)


def test_a_wrapped_bullet_stays_one_claim_with_its_continuation():
    answer = "- The vendor quoted $4.20 per unit for the\n  first thousand units [S1]."

    claims = extract_claims(answer)

    assert claims == ["The vendor quoted $4.20 per unit for the first thousand units [S1]."]


def test_a_bullet_character_outside_the_ascii_set_still_starts_a_claim():
    answer = "• Solar grew 40% [S1]\n• Wind fell 3% [S2]"

    claims = extract_claims(answer)

    assert claims == ["Solar grew 40% [S1]", "Wind fell 3% [S2]"]


def test_a_colon_line_with_no_list_under_it_is_still_a_claim():
    """Only a lead-in ABOVE A LIST is dropped — an ordinary colon sentence is an assertion."""
    answer = "The vendor's position was clear: the price is $4.20 [S1]."

    claims = extract_claims(answer)

    assert claims == ["The vendor's position was clear: the price is $4.20 [S1]."]


async def test_a_repeated_sentence_is_checked_once_per_source(
    make_config, scripted_model, monkeypatch
):
    """One model call per (claim x source), even when the answer repeats the sentence.

    The script holds exactly ONE reply: a second call on the same pair overruns it and
    raises IndexError, which is what makes this test able to fail.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/quote")
    write_source_capture(config, registry, source_id, "The vendor quoted $4.20 per unit.")
    sentence = f"The vendor quoted $4.20 per unit [{source_id}]."
    answer = f"{sentence}\n\nIn summary. {sentence}"

    model = scripted_model([verify_reply("supported", "Confirms $4.20.")])
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(answer, config, registry)

    assert model._call_count == 1
    checked = [c for c in result.checks if c.source_id == source_id]
    assert len(checked) == 1
    assert result.conflicts == []


async def test_one_source_checked_twice_never_reads_as_sources_disagreeing(
    make_config, scripted_model, monkeypatch
):
    """A conflict needs two DISTINCT sources, not merely two disagreeing verdicts.

    Feeds the same (claim, source) pair two different verdicts by passing an explicit
    `claims` list that names the sentence twice, bypassing the dedupe above. Grouping on
    claim text alone then produced a `Conflict` whose positions both read `[S1]`, and the
    report stated "the cited sources disagree on this claim" about a single source.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/quote")
    write_source_capture(config, registry, source_id, "The vendor quoted $4.20 per unit.")
    claim = f"The vendor quoted $4.20 per unit [{source_id}]."

    model = scripted_model(
        [
            verify_reply("supported", "Confirms $4.20."),
            verify_reply("unsupported", "Reads $5.10 to me."),
        ]
    )
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(claim, config, registry, claims=[claim, claim])

    assert result.conflicts == [], "one source cannot disagree with itself"


async def test_a_supplied_claims_list_is_what_gets_checked(
    make_config, scripted_model, monkeypatch
):
    """`claims=` is the shape `__main__` calls with, and nothing else exercised it.

    The supplied list is deliberately NOT what `extract_claims(answer)` would return, so a
    `verify_claims` that ignored the argument and recomputed would check the wrong text.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/quote")
    write_source_capture(config, registry, source_id, "The vendor quoted $4.20 per unit.")
    answer = f"Some prose the caller already parsed differently [{source_id}]."
    supplied = [f"The vendor quoted $4.20 per unit [{source_id}]."]

    model = scripted_model([verify_reply("supported", "Confirms $4.20.")])
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(answer, config, registry, claims=supplied)

    assert [c.claim for c in result.checks] == supplied
    assert "The vendor quoted $4.20 per unit" in _flatten(model._received_messages[0])
    assert "Some prose the caller already parsed" not in _flatten(model._received_messages[0])
