"""Split a synthesized answer into paragraph-shaped blocks with their citations intact.

This is the one shared definition of a paragraph, per D1 — it cannot live in `report.py`
or `verify.py` without a circular import. It is NOT responsible for verdicts, links, or
rendering: those live in `report.py` and `verify.py`, which build on this module's output
rather than the other way around.
"""

import re

from pydantic import BaseModel, ConfigDict

from harness.sources import MARKER_RE, marker_ids

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•–]|\d+[.)])\s+")


class Paragraph(BaseModel):
    """A single blank-line-delimited block of an answer, verbatim with markers intact."""

    model_config = ConfigDict(extra="forbid")

    text: str
    source_ids: list[str]
    items: list[str]


def split_paragraphs(answer: str) -> list[Paragraph]:
    """Split `answer` into `Paragraph`s on blank lines, dropping fenced code entirely."""
    without_code = _FENCE_RE.sub("", answer)

    paragraphs: list[Paragraph] = []
    for block in _BLANK_LINE_RE.split(without_code):
        block = block.strip("\n")
        if not block.strip():
            continue

        text = block
        source_ids = marker_ids(text)

        items: list[str] = []
        for line in text.split("\n"):
            if _LIST_ITEM_RE.match(line):
                items.append(_LIST_ITEM_RE.sub("", line, count=1).strip())

        paragraphs.append(Paragraph(text=text, source_ids=source_ids, items=items))

    return paragraphs


def strip_markers(text: str) -> str:
    """Remove every `[Sn]` marker from `text` and repair the whitespace it leaves.

    Applied per line so list and nested-list indentation survives.
    """
    lines: list[str] = []
    for line in text.split("\n"):
        indent_match = re.match(r"[ \t]*", line)
        indent = indent_match.group() if indent_match else ""
        body = MARKER_RE.sub("", line[len(indent) :])
        body = re.sub(r"[ \t]{2,}", " ", body)
        body = re.sub(r"[ \t]+([,.;:!?)])", r"\1", body)
        body = body.strip()
        if not body:
            continue
        lines.append(indent + body)

    return "\n".join(lines).strip("\n")
