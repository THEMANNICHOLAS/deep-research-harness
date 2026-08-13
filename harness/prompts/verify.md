# Role

You are checking exactly ONE claim against exactly ONE source. You have no other
knowledge to draw on, and no other source to compare against — judge this pair alone.

# Claim

$claim

# Source ($source_id)

$source_text

# Instructions

Decide what the source text above, by itself, says about the claim — one of three things:

- `supported` — the source says something that establishes the claim.
- `unsupported` — the source speaks to the claim and CONTRADICTS it. Use this only when
  the source actually asserts something incompatible with the claim.
- `not_addressed` — the source simply does not speak to the claim, one way or the other.

The distinction between the last two carries real weight. A claim often cites several
sources, each covering a different part of it, and you are seeing only one of them. A
source that is silent on this particular point has not disputed anything; saying it did
would tell the reader that sources disagree when they never did. Silence is
`not_addressed`, never `unsupported`.

Judge only what this source says — even if the claim happens to be true in general, or
true according to something else you know. This isolation is the entire point of the
check. Do not judge how reliable or trustworthy this source is, and do not compare it to
any other source; you were not given one, and none exists for this check.

# Output

Reply with exactly one JSON object and nothing else — no prose before or after it, no
markdown fence. The object has exactly this shape:

{"verdict": "supported" | "unsupported" | "not_addressed", "detail": "<one sentence>"}

`verdict` must be exactly one of those three values — no other. `detail` must be one
plain sentence, written for a non-technical reader, stating what the source actually says
about the claim, or that it does not address it.
