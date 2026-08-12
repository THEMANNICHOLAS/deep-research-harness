"""Behavioral tests for harness.verify — claim extraction, per-claim checks, conflicts.

`harness.verify` does not exist yet — Phase 6 builds it next. Importing it here is
deliberate collection-error red (see the plan's "Expected red" section); the assertions
below are written against the module's frozen contract so a stub `verify_claims` still
fails them on content, not just on import.
"""

import asyncio
import json

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr, SecretStr

from harness.sources import SourceRegistry
from harness.tools.fetch import FETCH_FAILED_PREFIX, _sources_dir
from harness.verify import Conflict, extract_claims, verify_claims
from tests.conftest import ScriptedChatModel


def _write_source(config, registry, source_id: str, body: str) -> None:
    """Write a real, `fetched`-shaped capture — the only thing a check may ever read."""
    sources_dir = _sources_dir(config, registry)
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / f"{source_id}.md").write_text(
        f"# {source_id}: captured page\n\n- Outcome: fetched\n\n{body}", encoding="utf-8"
    )


def _write_stub(config, registry, source_id: str, outcome: str = "blocked") -> None:
    """Write a failure stub — the shape `harness/tools/fetch.py` writes for a bad fetch."""
    sources_dir = _sources_dir(config, registry)
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / f"{source_id}.md").write_text(
        f"{FETCH_FAILED_PREFIX}{outcome}\n", encoding="utf-8"
    )


def _reply(verdict: str, detail: str) -> AIMessage:
    """A model reply in the JSON envelope `verify.py`'s parser is expected to accept."""
    return AIMessage(content=json.dumps({"verdict": verdict, "detail": detail}))


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
    _write_source(config, registry, supported_id, "Tungsten melts at 3422 degrees Celsius.")
    _write_source(config, registry, unsupported_id, "The oven only reaches 1200 degrees Celsius.")
    _write_stub(config, registry, failed_id, outcome="blocked")

    supported_claim = "Tungsten melts at 3422 degrees Celsius [S1]."
    unsupported_claim = "The oven can easily melt tungsten [S2]."
    uncited_claim = "Tungsten is a very hard metal."
    unresolved_claim = "Tungsten was discovered in 1781 [S9]."
    unverifiable_claim = "Tungsten has the highest melting point of any metal [S3]."
    answer = " ".join(
        [
            supported_claim,
            unsupported_claim,
            uncited_claim,
            unresolved_claim,
            unverifiable_claim,
        ]
    )

    model = scripted_model(
        [
            _reply("supported", "Matches the source exactly."),
            _reply("unsupported", "The oven falls short of tungsten's melting point."),
        ]
    )
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(answer, config, registry)

    by_claim = {check.claim: check for check in result.checks}
    assert by_claim[supported_claim].verdict == "supported"
    assert by_claim[supported_claim].source_id == "S1"
    assert by_claim[unsupported_claim].verdict == "unsupported"
    assert by_claim[unsupported_claim].source_id == "S2"
    assert by_claim[uncited_claim].verdict == "uncited"
    assert by_claim[uncited_claim].source_id is None
    assert by_claim[unresolved_claim].verdict == "unresolved"
    assert by_claim[unresolved_claim].source_id == "S9"
    assert by_claim[unverifiable_claim].verdict == "unverifiable"
    assert by_claim[unverifiable_claim].source_id == "S3"


async def test_a_check_sees_only_its_own_sources_captured_text(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    _write_source(config, registry, id1, "UNIQUE_MARKER_ONE: source one body text.")
    _write_source(config, registry, id2, "UNIQUE_MARKER_TWO: source two body text.")
    answer = f"Claim about one [{id1}]. Claim about two [{id2}]."

    model = scripted_model([_reply("supported", "ok"), _reply("supported", "ok")])
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
    _write_source(config, registry, source_id, "Captured content already on disk.")
    answer = f"A claim about the page [{source_id}]."

    class _ExplodingCrawler:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("verification must never fetch — D10/R8")

    monkeypatch.setattr("harness.tools.fetch.AsyncWebCrawler", _ExplodingCrawler)

    model = scripted_model([_reply("supported", "matches")])
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
        _write_source(config, registry, source_id, f"Body text for {source_id}.")
    answer = " ".join(f"Claim {i} about the page [{source_id}]." for i, source_id in enumerate(ids))

    model = _ConcurrencyTrackingModel(
        model="test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([_reply("supported", "ok")] * len(ids))
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
        _write_source(config, registry, source_id, f"Body text for {source_id}.")
    answer = " ".join(f"Claim {i} about the page [{source_id}]." for i, source_id in enumerate(ids))

    model = _RaisingOnSecondCallModel(
        model="test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([_reply("supported", "ok")] * len(ids))
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
    _write_source(config, registry, id1, "Source one says the price is $4.20.")
    _write_source(config, registry, id2, "Source two says the price is $5.10.")
    claim = f"The vendor quoted $4.20 per unit [{id1}] [{id2}]."

    model = scripted_model(
        [
            _reply("supported", "Confirms $4.20."),
            _reply("unsupported", "Says $5.10 instead."),
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
    _write_source(config, registry, id1, "Source one confirms the $4.20 price.")
    _write_source(config, registry, id2, "Source two also confirms the $4.20 price.")
    claim = f"The vendor quoted $4.20 per unit [{id1}] [{id2}]."

    model = scripted_model(
        [_reply("supported", "Confirms."), _reply("supported", "Also confirms.")]
    )
    monkeypatch.setattr("harness.verify.build_chat_model", lambda config, role: model)

    result = await verify_claims(claim, config, registry)

    assert len(result.checks) == 2
    assert result.conflicts == []
    assert {c.claim for c in result.checks} == {claim}
    assert {c.source_id for c in result.checks} == {id1, id2}
    assert all(c.verdict == "supported" for c in result.checks)


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
