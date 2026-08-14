"""Split a synthesized answer into paragraph-shaped blocks with their citations intact.

This is the one shared definition of a paragraph, per D1 — it cannot live in `report.py`
or `verify.py` without a circular import. It is NOT responsible for verdicts, links, or
rendering: those live in `report.py` and `verify.py`, which build on this module's output
rather than the other way around.
"""

import re

from pydantic import BaseModel, ConfigDict

from harness.sources import MARKER_RE, marker_ids

# Capturing, so `re.split` keeps the fence as its own segment rather than dropping it.
_FENCE_RE = re.compile(r"(```.*?```)", re.DOTALL)
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
# Public: `report.py` gates bullet marking on the same test that builds `items` here, so
# the Nth list line of a block is `items[N]` by construction rather than by text matching.
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•–]|\d+[.)])\s+")
# What a bullet whose only content was a citation (`- [S1]`) leaves behind once the marker
# is gone: its list syntax alone. See `strip_markers`.
_LIST_SYNTAX_ONLY_RE = re.compile(r"^(?:[-*+•–]|\d+[.)])$")


class Paragraph(BaseModel):
    """A single blank-line-delimited block of an answer, verbatim with markers intact."""

    model_config = ConfigDict(extra="forbid")

    text: str
    source_ids: list[str]
    items: list[str]
    # A fenced code block, kept whole. It carries no citations and no bullets, so it takes
    # verification's zero-call `no_sources_cited` path and renders verbatim — the fence is
    # excluded from the VERIFICATION unit without being dropped from the report.
    is_code: bool = False


def split_paragraphs(answer: str) -> list[Paragraph]:
    """Split `answer` into `Paragraph`s on blank lines, keeping each fence as one block."""
    paragraphs: list[Paragraph] = []
    for segment in _FENCE_RE.split(answer):
        if not segment.strip():
            continue

        if segment.startswith("```"):
            paragraphs.append(
                Paragraph(text=segment.strip("\n"), source_ids=[], items=[], is_code=True)
            )
            continue

        for block in _BLANK_LINE_RE.split(segment):
            text = block.strip("\n")
            if not text.strip():
                continue

            items = [
                LIST_ITEM_RE.sub("", line, count=1).strip()
                for line in text.split("\n")
                if LIST_ITEM_RE.match(line)
            ]
            paragraphs.append(Paragraph(text=text, source_ids=marker_ids(text), items=items))

    return paragraphs


def strip_markers(text: str) -> str:
    """Remove every `[Sn]` marker from `text` and repair the whitespace it leaves.

    Applied per line so list and nested-list indentation survives. A line left with no
    content at all is dropped — including a bullet that cited a source and said nothing
    else, whose list syntax would otherwise survive as a contentless `-` (PR #7 review).
    """
    lines: list[str] = []
    for line in text.split("\n"):
        indent_match = re.match(r"[ \t]*", line)
        indent = indent_match.group() if indent_match else ""
        body = MARKER_RE.sub("", line[len(indent) :])
        body = re.sub(r"[ \t]{2,}", " ", body)
        body = re.sub(r"[ \t]+([,.;:!?)])", r"\1", body)
        body = body.strip()
        if not body or _LIST_SYNTAX_ONLY_RE.match(body):
            continue
        lines.append(indent + body)

    return "\n".join(lines).strip("\n")
