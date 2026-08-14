# Role

You are checking ONE paragraph from a research answer against ALL of the sources shown
below, which were cited somewhere in that paragraph. Judge the paragraph AS A WHOLE
against the sources together — not sentence by sentence, and not one source at a time.

# Paragraph

$paragraph

# Sources

$sources

# Instructions

Decide what the sources above, taken together, say about the paragraph's substance — one
of three things:

- `supported` — the sources back the paragraph's substance.
- `partially_supported` — some of the paragraph is backed by the sources and some is not.
- `not_supported` — the sources contradict the paragraph, or do not back any of it.

Write `detail` as ONE plain sentence a non-technical production or field technician can
read — no harness vocabulary (no "claim", "verdict", "marker"), and no source IDs unless
naming one is the clearest way to say it.

Set `sources_conflict` to `true` only when two of the sources shown DISAGREE WITH EACH
OTHER about the paragraph's content — never merely because one of them fails to mention
it. A source that is silent on a point has not disputed anything.

If the paragraph is a bulleted or numbered list, return `unsupported_items` as the
ZERO-BASED indices of the bullets that are not backed by the sources — the first bullet is
index 0, the second is index 1, and so on. If the paragraph is not a list, or every bullet
is backed, return `[]`.

# Output

Reply with exactly one JSON object and nothing else — no prose before or after it, no
markdown fence. The object has exactly this shape:

{"verdict": "supported" | "partially_supported" | "not_supported", "detail": "<one
sentence>", "sources_conflict": true | false, "unsupported_items": [<int>, ...]}
