# Later Problems

Known defects accepted into a merge rather than fixed at the gate. Each entry names
what is wrong, what it costs while it stands, and what fixing it takes. Unlike
docs/backlog.md — which holds deferred *work* — everything here is a real defect
already shipping.

## Claims on a line above a list skip verification entirely

**Where:** `harness/verify.py` — `_block_units`, the list lead-in branch.

**What:** the lead-in test (line ends in `:` and the next line is a list item) runs
before the test for whether the line is itself a claim-bearing unit, so any
colon-terminated line above a list is discarded instead of becoming a claim. The
common shape is ordinary prose — `Three factors drove the decline [S1]:` followed by
bullets — not just a bulleted parent above a sub-list.

**Cost while it stands:** the dropped line never reaches `extract_claims`, so no
`ClaimCheck` exists for it. `_annotate` marks only claims it has checks for, and
supported claims are deliberately left unmarked, so an *unchecked* sentence renders
byte-identical to a verified one. Nothing discloses it either: `_gaps_section` covers
unresolved IDs, check failures, unplaced markers and the uncited count, and a dropped
line hits none of them, while `registry.resolve` still renders its `[Sn]` as a working
link. That is a violation of R3 (MUST — the report never overstates its evidence) that
neither the reader nor the operator can detect.

**To address:** test `_LIST_ITEM_RE` against the current line before the lead-in
branch, so a line that is itself a list item is never treated as a discardable
lead-in. Cover both shapes in tests — a bulleted `[S1]:` parent over a sub-list, and
prose `...[S1]:` over a sibling list; `tests/test_verify.py` pins only the
colon-line-with-no-list-under-it case today.

**Worth considering alongside it:** the verification design marks only failures, so
"unmarked" means both "verified fine" and "never checked". A reconciliation check —
every `[Sn]`-bearing line in the answer appears in exactly one claim unit — would
catch this whole class structurally rather than one prose shape at a time.

Found by the PR #4 review (CONFIRMED, Blocker, 3/3 quorum); accepted into that merge
deliberately.
