"""Behavioral tests for harness.paragraphs."""

import pytest

from harness.paragraphs import Paragraph, renders_content, split_paragraphs, strip_markers


def test_renders_content_distinguishes_prose_citation_only_and_code():
    """`renders_content` is the ONE definition of "shows up in `## Answer`" (D1), counted by
    both `report._answer_section` (numbering/dropping) and `verify._format_verdicts_block`
    (the reviewer paragraph's numbers) — pinned directly so a change to it is a deliberate
    edit to that shared contract, not a side effect noticed via one consumer's tests.
    """
    prose, citation_only, code = split_paragraphs(
        "Real prose [S1].\n\n[S2]\n\n```python\nprint('hi')\n```"
    )

    assert renders_content(prose) is True
    # A citation-only paragraph strips to nothing and renders nothing.
    assert renders_content(citation_only) is False
    # A fence always renders, even though it carries no citations or bullets.
    assert renders_content(code) is True


def test_renders_content_is_false_for_a_citation_only_bullet_list():
    # A list whose every bullet is nothing but a marker leaves only list syntax, which
    # `strip_markers` drops — so the block renders nothing.
    [paragraph] = split_paragraphs("- [S1]\n- [S2]")

    assert renders_content(paragraph) is False


def test_fenced_code_block_is_its_own_code_paragraph_keeping_its_content():
    """A fence is excluded from the VERIFICATION unit without being dropped from the answer: as an
    `is_code` paragraph with no citations and no bullets it takes the zero-call path, and its text
    still renders.
    """
    answer = (
        "Intro paragraph one.\n\n```python\ndef f():\n    return 1\n```\n\nClosing paragraph two."
    )

    paragraphs = split_paragraphs(answer)

    assert [p.text for p in paragraphs] == [
        "Intro paragraph one.",
        "```python\ndef f():\n    return 1\n```",
        "Closing paragraph two.",
    ]
    assert [p.is_code for p in paragraphs] == [False, True, False]
    code = paragraphs[1]
    assert code.source_ids == [] and code.items == []


def test_a_marker_inside_a_fence_is_not_treated_as_a_citation():
    """A `[S1]` in a code sample is syntax, not a citation: it must not pull a source onto a
    `Sources:` line or trigger a verification call.
    """
    paragraphs = split_paragraphs("```\nlookup(table[S1])\n```")

    assert len(paragraphs) == 1
    assert paragraphs[0].is_code is True
    assert paragraphs[0].source_ids == []


def test_source_ids_deduplicated_in_first_appearance_order():
    answer = "The pump [S2] failed [S1] again [S2]."

    paragraphs = split_paragraphs(answer)

    assert len(paragraphs) == 1
    assert paragraphs[0].source_ids == ["S2", "S1"]


def test_source_ids_empty_list_when_block_has_no_marker():
    answer = "No citations in this block at all."

    paragraphs = split_paragraphs(answer)

    assert len(paragraphs) == 1
    assert paragraphs[0].source_ids == []


@pytest.mark.parametrize(
    ("marker", "block"),
    [
        ("-", "- First item [S1]\n- Second item [S2]\n- Third item"),
        ("*", "* First item [S1]\n* Second item [S2]\n* Third item"),
        ("1.", "1. First item [S1]\n2. Second item [S2]\n3. Third item"),
    ],
)
def test_bullet_list_yields_one_paragraph_with_marker_stripped_items(marker, block):
    paragraphs = split_paragraphs(block)

    assert len(paragraphs) == 1
    assert paragraphs[0].items == ["First item [S1]", "Second item [S2]", "Third item"]


def test_lead_in_line_directly_above_list_stays_in_text_and_is_not_an_item():
    block = "Here are the findings:\n- First item\n- Second item"

    paragraphs = split_paragraphs(block)

    assert len(paragraphs) == 1
    assert paragraphs[0].text == block
    assert paragraphs[0].items == ["First item", "Second item"]


def test_heading_directly_above_prose_stays_in_the_same_paragraph_text():
    block = "## Findings\nThe pump failed under load."

    paragraphs = split_paragraphs(block)

    assert len(paragraphs) == 1
    assert paragraphs[0].text == "## Findings\nThe pump failed under load."
    assert paragraphs[0].items == []


def test_strip_markers_removes_mid_sentence_and_end_of_line_markers_cleanly():
    assert strip_markers("The pump [S1] failed [S2].") == "The pump failed."


def test_strip_markers_drops_a_line_that_holds_only_a_marker():
    text = "First line [S1].\n[S2]\nThird line [S3]."

    result = strip_markers(text)

    assert result == "First line.\nThird line."


def test_strip_markers_drops_a_bullet_that_held_only_a_marker():
    """The list-syntax half of the test above: `- [S1]` survived as a contentless `-`, because the
    dash kept the line truthy once the marker was gone.
    """
    text = "- Real finding [S1].\n- [S2]\n1. [S3]"

    result = strip_markers(text)

    assert result == "- Real finding."


def test_paragraph_model_round_trips_fields():
    paragraph = Paragraph(text="Some text [S1].", source_ids=["S1"], items=[])

    assert paragraph.text == "Some text [S1]."
    assert paragraph.source_ids == ["S1"]
    assert paragraph.items == []
