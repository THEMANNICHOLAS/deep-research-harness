"""Behavioral tests for harness.verify — pooled, one-call-per-paragraph verification.

The pooled contract: one model call per `Paragraph` however many sources it cites, deterministic
verdicts assigned without a call, and `sources_conflict`/`unsupported_items` carried straight
through from the reply.

The `_parse_reply` tolerance tests at the end exist because the model is not guaranteed to return
bare, complete JSON — every tolerance must have a test that fails if the tolerance is removed.
"""

import asyncio
from typing import get_args

import pytest
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr, SecretStr

from harness.paragraphs import Paragraph
from harness.sources import SourceRegistry, sources_dir
from harness.verify import CHECK_FAILED_DETAIL, Verdict, verify_paragraphs
from tests.conftest import (
    ScriptedChatModel,
    verify_reply,
    write_failed_capture,
    write_source_capture,
)


def _flatten(messages) -> str:
    """Join a call's message contents into one string, for substring assertions."""
    return " ".join(str(getattr(m, "content", "")) for m in messages)


def _paragraph(text: str, source_ids: list[str], items: list[str] | None = None) -> Paragraph:
    return Paragraph(text=text, source_ids=source_ids, items=items or [])


# --- item 1: one call per paragraph, prompt carries every pooled source ----------------


async def test_one_call_per_paragraph_and_prompt_contains_every_pooled_source(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    id3 = registry.add("https://example.test/three")
    write_source_capture(config, registry, id1, "UNIQUE_MARKER_ONE: body text.")
    write_source_capture(config, registry, id2, "UNIQUE_MARKER_TWO: body text.")
    write_source_capture(config, registry, id3, "UNIQUE_MARKER_THREE: body text.")
    paragraph = _paragraph(f"The pump failed under load [{id1}] [{id2}] [{id3}].", [id1, id2, id3])

    model = scripted_model([verify_reply("supported", "All three sources agree.")])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert model._call_count == 1
    prompt_text = _flatten(model._received_messages[0])
    assert "UNIQUE_MARKER_ONE" in prompt_text
    assert "UNIQUE_MARKER_TWO" in prompt_text
    assert "UNIQUE_MARKER_THREE" in prompt_text
    assert len(result.verdicts) == 1
    assert result.verdicts[0].verdict == "supported"


class _ConcurrencyTrackingModel(ScriptedChatModel):
    """Tracks in-flight `_agenerate` calls to prove the verification loop is sequential.

    The `asyncio.sleep(0)` is load-bearing (D4): with no real await point, even a concurrent
    `asyncio.gather` would show a peak of 1, since nothing yields between increment and decrement.
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


async def test_paragraphs_are_checked_strictly_sequentially(make_config, monkeypatch):
    config = make_config()
    registry = SourceRegistry()
    ids = [registry.add(f"https://example.test/page-{i}") for i in range(3)]
    for source_id in ids:
        write_source_capture(config, registry, source_id, f"Body text for {source_id}.")
    paragraphs = [
        _paragraph(f"Paragraph {i} about the page [{source_id}].", [source_id])
        for i, source_id in enumerate(ids)
    ]

    model = _ConcurrencyTrackingModel(
        model="test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([verify_reply("supported", "ok")] * len(ids))
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs(paragraphs, config, registry)

    assert model._peak_in_flight == 1
    assert model._call_count == len(paragraphs)
    assert len(result.verdicts) == len(paragraphs)


async def test_on_paragraph_fires_once_per_paragraph_including_no_call_ones(
    make_config, scripted_model, monkeypatch
):
    """The progress callback must tick for EVERY paragraph — the deterministic-verdict ones
    that skip the model call included — so the i/n sequence at the terminal stays monotone.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/one")
    write_source_capture(config, registry, source_id, "Body text.")
    paragraphs = [
        _paragraph("No citations in this block at all.", []),
        _paragraph(f"A checked claim [{source_id}].", [source_id]),
    ]

    model = scripted_model([verify_reply("supported", "ok")])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    seen: list[tuple[int, int]] = []
    await verify_paragraphs(
        paragraphs, config, registry, on_paragraph=lambda i, n: seen.append((i, n))
    )

    assert seen == [(1, 2), (2, 2)]


async def test_verify_paragraphs_uses_the_verifier_role_not_head(make_config, patch_models_by_role):
    """Phase 1 Step 4: verification runs on a model that did not write the report (D4)."""
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/page")
    write_source_capture(config, registry, source_id, "Body text.")
    paragraph = _paragraph(f"A claim [{source_id}].", [source_id])

    head_model = ScriptedChatModel(
        model="head-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([verify_reply("supported", "wrong role checked this")])
    verifier_model = ScriptedChatModel(
        model="verifier-test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script([verify_reply("supported", "checked by the verifier")])
    patch_models_by_role({"head": head_model, "verifier": verifier_model})

    result = await verify_paragraphs([paragraph], config, registry)

    assert head_model._call_count == 0
    assert verifier_model._call_count == 1
    assert result.verdicts[0].detail == "checked by the verifier"


# --- item 2: deterministic verdicts bypass the model entirely --------------------------


async def test_no_markers_or_all_unregistered_sources_returns_no_sources_cited_without_a_call(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    no_markers = _paragraph("No citations in this block at all.", [])
    all_unregistered = _paragraph("Refers to an unregistered source [S99].", ["S99"])

    model = scripted_model([])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([no_markers, all_unregistered], config, registry)

    assert model._call_count == 0
    assert [v.verdict for v in result.verdicts] == ["no_sources_cited", "no_sources_cited"]
    assert all(v.source_ids == [] for v in result.verdicts)
    assert all(v.unsupported_items == [] for v in result.verdicts)


# --- item 3: failed/missing captures are excluded; empty pool is not_verified ----------


async def test_a_failed_capture_or_missing_file_is_excluded_from_the_pooled_prompt(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    healthy_id = registry.add("https://example.test/healthy")
    failed_id = registry.add("https://example.test/failed")
    write_source_capture(config, registry, healthy_id, "UNIQUE_HEALTHY_BODY text.")
    write_failed_capture(config, registry, failed_id, outcome="blocked")
    paragraph = _paragraph(
        f"The pump failed under load [{healthy_id}] [{failed_id}].", [healthy_id, failed_id]
    )

    model = scripted_model([verify_reply("supported", "Confirmed by the one usable source.")])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert model._call_count == 1
    prompt_text = _flatten(model._received_messages[0])
    assert "UNIQUE_HEALTHY_BODY" in prompt_text
    assert "FETCH FAILED" not in prompt_text
    assert result.verdicts[0].source_ids == [healthy_id]


async def test_a_paragraph_left_with_no_usable_source_returns_not_verified_naming_the_reason(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    missing_id = registry.add("https://example.test/missing")
    failed_id = registry.add("https://example.test/failed")
    write_failed_capture(config, registry, failed_id, outcome="error")
    # `missing_id` is registered but never captured: no file exists under `sources_dir`.
    paragraph = _paragraph(
        f"The pump failed under load [{missing_id}] [{failed_id}].", [missing_id, failed_id]
    )

    model = scripted_model([])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert model._call_count == 0
    verdict = result.verdicts[0]
    assert verdict.verdict == "not_verified"
    # "readable", not just "exists": the catch covers a `UnicodeDecodeError` from a capture whose
    # write died mid-character, not only a missing file.
    assert "no readable captured content exists" in verdict.detail
    assert "FETCH FAILED: error" in verdict.detail
    assert verdict.source_ids == []


async def test_a_capture_that_is_not_valid_utf8_is_skipped_not_raised(
    make_config, scripted_model, monkeypatch
):
    """A `write_text` dying mid-flush leaves a byte prefix that can end mid-character, and
    `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so catching `OSError` alone let it
    escape the whole verification pass.
    """
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/truncated")
    write_source_capture(config, registry, source_id, "placeholder")
    # A lone continuation byte — valid on disk, undecodable as UTF-8.
    (sources_dir(config, registry) / f"{source_id}.md").write_bytes(b"Tungsten melts at \xff\xfe")

    paragraph = _paragraph(f"Tungsten melts at 3422 C [{source_id}].", [source_id])
    model = scripted_model([])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert model._call_count == 0
    assert result.verdicts[0].verdict == "not_verified"
    assert "no readable captured content exists" in result.verdicts[0].detail


# --- item 4: malformed reply / unknown verdict / raised exception all continue the loop -


class _RaisesOnThirdCallModel(ScriptedChatModel):
    """Raises on its third call only (index 2), recording every call it is actually asked."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        call_index = self._call_count
        self._received_messages.append(list(messages))
        self._call_count += 1
        if call_index == 2:
            raise RuntimeError("simulated model outage")
        response = self._script[call_index]
        return ChatResult(generations=[ChatGeneration(message=response)])


async def test_a_malformed_reply_unknown_verdict_and_raised_exception_all_continue_the_loop(
    make_config, monkeypatch
):
    from langchain_core.messages import AIMessage

    config = make_config()
    registry = SourceRegistry()
    ids = [registry.add(f"https://example.test/page-{i}") for i in range(4)]
    for source_id in ids:
        write_source_capture(config, registry, source_id, f"Body text for {source_id}.")
    paragraphs = [
        _paragraph(f"Paragraph {i} about the page [{source_id}].", [source_id])
        for i, source_id in enumerate(ids)
    ]

    script = [
        AIMessage(content="this is not json at all"),
        verify_reply("bogus_verdict_value", "irrelevant"),
        verify_reply("supported", "unreachable placeholder"),
        verify_reply("supported", "The final paragraph checked out."),
    ]
    model = _RaisesOnThirdCallModel(
        model="test-model", base_url="https://example.test/v1", api_key=SecretStr("x")
    ).script(script)
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs(paragraphs, config, registry)

    assert model._call_count == 4
    verdicts = [v.verdict for v in result.verdicts]
    assert verdicts == ["not_verified", "not_verified", "not_verified", "supported"]
    assert result.verdicts[3].detail == "The final paragraph checked out."
    assert len(result.check_failures) == 3


async def test_a_failed_check_keeps_its_diagnostic_out_of_the_readers_verdict_detail(
    make_config, scripted_model, monkeypatch
):
    """R4 wants verdict wording a technician can act on, so the exception text goes to
    `check_failures` — which `## Gaps and disclosures` prints — never to the `Verdict:` detail.
    """
    from langchain_core.messages import AIMessage

    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/page")
    write_source_capture(config, registry, source_id, "Body text.")
    paragraph = _paragraph(f"A claim [{source_id}].", [source_id])

    model = scripted_model([AIMessage(content="this is not json at all")])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    verdict = result.verdicts[0]
    assert verdict.verdict == "not_verified"
    assert verdict.detail == CHECK_FAILED_DETAIL
    # The diagnostic is not lost — it is disclosed, just not on the reader-facing line.
    assert len(result.check_failures) == 1
    assert "Error" in result.check_failures[0]


# --- item 5: sources_conflict / unsupported_items round-trip, verdicts index-align ------


async def test_sources_conflict_and_unsupported_items_round_trip_and_verdicts_are_index_aligned(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Source one text.")
    write_source_capture(config, registry, id2, "Source two text.")
    list_paragraph = _paragraph(
        f"- First finding [{id1}]\n- Second finding [{id2}]\n- Third finding [{id1}]",
        [id1, id2],
        items=["First finding [S1]", "Second finding [S2]", "Third finding [S1]"],
    )
    prose_paragraph = _paragraph(f"The pump failed under load [{id1}] [{id2}].", [id1, id2])

    model = scripted_model(
        [
            verify_reply(
                "partially_supported",
                "Two of the three findings check out.",
                sources_conflict=True,
                unsupported_items=[0, 2],
            ),
            verify_reply("supported", "Confirmed by both sources.", sources_conflict=False),
        ]
    )
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([list_paragraph, prose_paragraph], config, registry)

    assert len(result.verdicts) == 2
    first, second = result.verdicts
    assert first.verdict == "partially_supported"
    assert first.detail == "Two of the three findings check out."
    assert first.sources_conflict is True
    assert first.unsupported_items == [0, 2]
    assert second.verdict == "supported"
    assert second.detail == "Confirmed by both sources."
    assert second.sources_conflict is False
    assert second.unsupported_items == []


async def test_a_single_pooled_source_can_still_carry_a_conflict(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/one")
    write_source_capture(config, registry, source_id, "Source text.")
    paragraph = _paragraph(f"The pump failed under load [{source_id}].", [source_id])

    model = scripted_model(
        [verify_reply("not_supported", "Contradicts itself.", sources_conflict=True)]
    )
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert result.verdicts[0].sources_conflict is True
    assert result.verdicts[0].source_ids == [source_id]


# --- kept in spirit: exhaustive vocabulary, never fetches -------------------------------


async def test_every_verdict_in_the_frozen_vocabulary_is_reachable(
    make_config, scripted_model, monkeypatch
):
    config = make_config()
    registry = SourceRegistry()
    supported_id = registry.add("https://example.test/tungsten-melting-point")
    partial_id = registry.add("https://example.test/oven-specs")
    not_supported_id = registry.add("https://example.test/dead-oven")
    # Registered but never captured — the "no usable source" path to `not_verified`,
    # distinct from an unregistered marker (which is `no_sources_cited` instead).
    uncaptured_id = registry.add("https://example.test/uncaptured")
    write_source_capture(config, registry, supported_id, "Tungsten melts at 3422 degrees Celsius.")
    write_source_capture(config, registry, partial_id, "The oven reaches 1200 Celsius, no more.")
    write_source_capture(config, registry, not_supported_id, "Tungsten never melts in this kiln.")

    supported_paragraph = _paragraph(
        f"Tungsten melts at 3422 degrees Celsius [{supported_id}].", [supported_id]
    )
    partial_paragraph = _paragraph(
        f"The oven can partially melt tungsten [{partial_id}].", [partial_id]
    )
    not_supported_paragraph = _paragraph(
        f"The kiln easily melts tungsten [{not_supported_id}].", [not_supported_id]
    )
    no_sources_paragraph = _paragraph("Tungsten is a very hard metal.", [])
    not_verified_paragraph = _paragraph(
        f"Tungsten was discovered in 1781 [{uncaptured_id}].", [uncaptured_id]
    )

    model = scripted_model(
        [
            verify_reply("supported", "Matches the source exactly."),
            verify_reply("partially_supported", "Only the low end is confirmed."),
            verify_reply("not_supported", "The kiln specs contradict this."),
        ]
    )
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs(
        [
            supported_paragraph,
            partial_paragraph,
            not_supported_paragraph,
            no_sources_paragraph,
            not_verified_paragraph,
        ],
        config,
        registry,
    )

    seen = {v.verdict for v in result.verdicts}
    # Exhaustive by construction: a verdict added later fails here until it gets a case.
    assert seen == set(get_args(Verdict))


async def test_a_check_never_fetches(make_config, scripted_model, monkeypatch):
    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/page")
    write_source_capture(config, registry, source_id, "Captured content already on disk.")
    paragraph = _paragraph(f"A claim about the page [{source_id}].", [source_id])

    class _ExplodingCrawler:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("verification must never fetch — D10/R8")

    monkeypatch.setattr("harness.tools.fetch._crawler_class", lambda: _ExplodingCrawler)

    model = scripted_model([verify_reply("supported", "matches")])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert result.verdicts[0].verdict == "supported"


# --- rewrite of test_a_supplied_claims_list_is_what_gets_checked -----------------------


async def test_the_input_paragraphs_list_is_exactly_what_gets_checked(
    make_config, scripted_model, monkeypatch
):
    """`verify_paragraphs` has no `claims=`/answer-splitting parameter any more — it always
    checks the caller's list, each paragraph seeing only its OWN text, never another
    paragraph's.
    """
    config = make_config()
    registry = SourceRegistry()
    id1 = registry.add("https://example.test/one")
    id2 = registry.add("https://example.test/two")
    write_source_capture(config, registry, id1, "Source one text.")
    write_source_capture(config, registry, id2, "Source two text.")
    first = _paragraph(f"UNIQUE_PARAGRAPH_ONE [{id1}].", [id1])
    second = _paragraph(f"UNIQUE_PARAGRAPH_TWO [{id2}].", [id2])

    model = scripted_model([verify_reply("supported", "ok"), verify_reply("supported", "ok")])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([first, second], config, registry)

    first_prompt = _flatten(model._received_messages[0])
    second_prompt = _flatten(model._received_messages[1])
    assert "UNIQUE_PARAGRAPH_ONE" in first_prompt
    assert "UNIQUE_PARAGRAPH_TWO" not in first_prompt
    assert "UNIQUE_PARAGRAPH_TWO" in second_prompt
    assert "UNIQUE_PARAGRAPH_ONE" not in second_prompt
    assert len(result.verdicts) == 2


# --- risk #1: `_parse_reply` tolerances, each able to fail if its tolerance is removed --


async def test_a_reply_wrapped_in_prose_is_still_parsed(make_config, scripted_model, monkeypatch):
    """The model may narrate around its JSON: the object between the first `{` and the last `}` is
    still the answer, and none of the prose leaks into `detail`.
    """
    from langchain_core.messages import AIMessage

    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/page")
    write_source_capture(config, registry, source_id, "The vendor quoted $4.20.")
    paragraph = _paragraph(f"The vendor quoted $4.20 [{source_id}].", [source_id])

    model = scripted_model(
        [
            AIMessage(
                content=(
                    "Sure! Here is my assessment of the paragraph:\n"
                    '{"verdict": "supported", "detail": "The capture quotes $4.20.", '
                    '"sources_conflict": false, "unsupported_items": []}\n'
                    "Let me know if you would like another pass."
                )
            )
        ]
    )
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert result.verdicts[0].verdict == "supported"
    assert result.verdicts[0].detail == "The capture quotes $4.20."
    assert result.check_failures == []


_OBJECT = '{"verdict": "supported", "detail": "The capture quotes $4.20."}'


@pytest.mark.parametrize(
    ("content", "shape"),
    [
        (f"{_OBJECT} Hope that helps!", "trailing prose only"),
        (f"```json\n{_OBJECT}\n```", "markdown fence"),
    ],
)
async def test_a_reply_wrapped_on_either_side_is_still_parsed(
    make_config, scripted_model, monkeypatch, content, shape
):
    """Both remaining wrapper shapes. Trailing-prose-only is the one that failed: the repair was
    gated on the reply NOT starting with `{`, so an object followed by a sign-off reached
    `json.loads` whole and raised "Extra data", turning a genuine verdict into `not verified`.
    """
    from langchain_core.messages import AIMessage

    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/page")
    write_source_capture(config, registry, source_id, "The vendor quoted $4.20.")
    paragraph = _paragraph(f"The vendor quoted $4.20 [{source_id}].", [source_id])

    model = scripted_model([AIMessage(content=content)])
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert result.verdicts[0].verdict == "supported", shape
    assert result.verdicts[0].detail == "The capture quotes $4.20."
    assert result.check_failures == []


async def test_a_reply_omitting_unsupported_items_defaults_to_empty(
    make_config, scripted_model, monkeypatch
):
    """The model may omit `unsupported_items` and `sources_conflict` entirely: a missing list is
    empty and a missing flag is False, not a parse failure.
    """
    from langchain_core.messages import AIMessage

    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/page")
    write_source_capture(config, registry, source_id, "Body text.")
    paragraph = _paragraph(f"A claim [{source_id}].", [source_id])

    model = scripted_model(
        [AIMessage(content='{"verdict": "not_supported", "detail": "The source disagrees."}')]
    )
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    verdict = result.verdicts[0]
    assert verdict.verdict == "not_supported"
    assert verdict.detail == "The source disagrees."
    assert verdict.unsupported_items == []
    assert verdict.sources_conflict is False
    assert result.check_failures == []


async def test_non_integer_bullet_indices_are_dropped_not_carried_through(
    make_config, scripted_model, monkeypatch
):
    """`unsupported_items` may arrive with strings or nulls mixed in. Only real ints survive, so
    the render path never indexes a list with a string; `True` is an int to Python and is dropped
    too, since a boolean is not a bullet index.
    """
    from langchain_core.messages import AIMessage

    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/page")
    write_source_capture(config, registry, source_id, "Body text.")
    paragraph = _paragraph(
        f"- First [{source_id}]\n- Second\n- Third",
        [source_id],
        items=["First [S1]", "Second", "Third"],
    )

    model = scripted_model(
        [
            AIMessage(
                content=(
                    '{"verdict": "partially_supported", "detail": "Mixed.", '
                    '"unsupported_items": [0, "1", null, true, 2]}'
                )
            )
        ]
    )
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert result.verdicts[0].unsupported_items == [0, 2]


async def test_a_quoted_conflict_flag_reads_as_no_conflict(
    make_config, scripted_model, monkeypatch
):
    """The prompt asks for an unquoted boolean, but `bool("false")` is True — a quoted one filed
    the paragraph under `## Conflicting sources` against its own reply. Only a real boolean sets
    the flag, the same stance the non-int bullet indices above take.
    """
    from langchain_core.messages import AIMessage

    config = make_config()
    registry = SourceRegistry()
    source_id = registry.add("https://example.test/page")
    write_source_capture(config, registry, source_id, "Body text.")
    paragraph = _paragraph(f"A claim [{source_id}].", [source_id])

    model = scripted_model(
        [
            AIMessage(
                content=(
                    '{"verdict": "supported", "detail": "The capture agrees.", '
                    '"sources_conflict": "false"}'
                )
            )
        ]
    )
    monkeypatch.setattr("harness.models.build_chat_model", lambda config, role: model)

    result = await verify_paragraphs([paragraph], config, registry)

    assert result.verdicts[0].sources_conflict is False
    assert result.check_failures == []
