"""Behavioral tests for harness.paragraphs."""

import pytest

from harness.paragraphs import Paragraph, split_paragraphs, strip_markers


def test_fenced_code_block_is_removed_and_never_becomes_a_paragraph():
    answer = (
        "Intro paragraph one.\n\n```python\ndef f():\n    return 1\n```\n\nClosing paragraph two."
    )

    paragraphs = split_paragraphs(answer)

    assert len(paragraphs) == 2
    assert paragraphs[0].text == "Intro paragraph one."
    assert paragraphs[1].text == "Closing paragraph two."
    for paragraph in paragraphs:
        assert "def f()" not in paragraph.text
        assert "return 1" not in paragraph.text


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


def test_paragraph_model_round_trips_fields():
    paragraph = Paragraph(text="Some text [S1].", source_ids=["S1"], items=[])

    assert paragraph.text == "Some text [S1]."
    assert paragraph.source_ids == ["S1"]
    assert paragraph.items == []
