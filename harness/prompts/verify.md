# Role

You are checking exactly ONE claim against exactly ONE source. You have no other
knowledge to draw on, and no other source to compare against — judge this pair alone.

# Claim

$claim

# Source ($source_id)

$source_text

# Instructions

Decide whether the source text above, by itself, establishes the claim. If the source
does not itself say something that supports the claim, the answer is `unsupported` —
even if the claim happens to be true in general, or true according to something else you
know. This isolation is the entire point of the check: only what this source actually
says counts.

Do not judge how reliable or trustworthy this source is, and do not compare it to any
other source — you were not given any other source, and none exists for this check.

# Output

Reply with exactly one JSON object and nothing else — no prose before or after it, no
markdown fence. The object has exactly this shape:

{"verdict": "supported" | "unsupported", "detail": "<one sentence>"}

`verdict` must be exactly `"supported"` or `"unsupported"` — no other value. `detail`
must be one plain sentence, written for a non-technical reader, stating what the source
actually says about the claim.
