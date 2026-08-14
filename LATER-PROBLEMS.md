# Later Problems

Known defects accepted into a merge rather than fixed at the gate. Each entry names
what is wrong, what it costs while it stands, and what fixing it takes. Unlike
docs/backlog.md — which holds deferred *work* — everything here is a real defect
already shipping.

---

## [RESOLVED by PLAN-report-output] Claims on a line above a list skip verification entirely

**Was:** `harness/verify.py` — `_block_units`, the list lead-in branch. A
colon-terminated line above a list was discarded instead of becoming a claim, so an
unchecked sentence rendered byte-identical to a verified one and nothing disclosed it.
Found by the PR #4 review (CONFIRMED, Blocker, 3/3 quorum) and accepted into that merge.

**Why it is gone:** `_block_units` and the whole per-line claim-unit concept were deleted
by `docs/plans/PLAN-report-output.md`. `split_paragraphs` splits on blank lines only, so a
lead-in stays inside its own `Paragraph.text` and its `[Sn]` is collected into
`source_ids` — the line reaches verification as part of the block it introduces. Verified
against the entry's own example: `Three factors drove the decline [S1]:` over two bullets
yields one paragraph carrying `S1` and both items.

**Watch:** the structural fix the entry recommended — reconciling every `[Sn]`-bearing
line against exactly one checked unit — was never built. Paragraph boundaries make the
old shape unreachable rather than detected, so a future change to `split_paragraphs`
could reopen the class without a test noticing.

---

# Deferred from the PLAN-report-output implement run

Issues found during the `/implement` run of `docs/plans/PLAN-report-output.md`
(2026-08-13) that were deliberately NOT fixed in that run, with why. Ordered by severity.
---

## 1. [Major — FIXED, PR #7 review] Fenced code blocks are dropped from the report's `## Answer`

**Resolved** by the fix the entry itself proposed: `Paragraph` gained `is_code`,
`split_paragraphs` emits each fence as its own code paragraph (citation-free and
bullet-free, so verification still makes zero calls for it and index alignment holds),
and `_paragraph_block` returns that text verbatim. The Phase 1 test asserting fences are
removed was replaced by one asserting they survive as `is_code` paragraphs. Original
write-up kept below.

---


**What:** `_answer_section` renders only `outcome.paragraphs`, and `split_paragraphs`
strips fenced blocks before any `Paragraph` is built (`harness/paragraphs.py`, the
`_FENCE_RE.sub("", answer)` line). The old `_annotate` returned
`registry.resolve(outcome.answer)` verbatim, so a fenced snippet, ASCII table, or config
sample used to reach the report. It no longer does — it vanishes silently.

**Why it matters:** R2 removes MARKERS from prose, not content. An answer that puts a
comparison table or a command in a fence now loses it, and nothing tells the reader.

**Why not fixed here:** the fix changes a frozen Phase 1 contract and its test
("fenced code blocks are removed entirely and never become a paragraph"), which was
written to keep code out of the VERIFICATION unit — a good reason that does not extend to
rendering. Changing it unattended would be improvising past an approved contract.

**Suggested fix:** give `Paragraph` an `is_code: bool = False` field; have
`split_paragraphs` emit fenced blocks as paragraphs with `is_code=True`, `items=[]`,
`source_ids=[]`; have the renderer emit `paragraph.text` verbatim (no `strip_markers`) for
those, and skip the `Sources:`/`Verdict:` pair (already gated off, since a code block
carries no registered markers). Verification needs no change — a code block has no
markers, so it already takes the zero-call `no_sources_cited` path and index alignment
holds. Add a test that an answer containing a fence renders that fence in `## Answer`.

---

## 2. [Minor] R4's `no sources cited` verdict label can never appear in a report

**What:** R4 lists `no sources cited` as one of the four reader-facing `Verdict:` values,
but no code path emits it. The `Sources:`/`Verdict:` pair is gated on the paragraph having
at least one REGISTERED source, and `no_sources_cited` is assigned exactly when that list
is empty — the two conditions are mutually exclusive by construction.

**Why not fixed here:** this is a requirements-text question, not a bug. D4 (unregistered
markers dropped) plus the citation-only rendering gate may well have retired the label on
purpose, in which case R4's wording is simply stale. Either resolution is a developer call.

**Options:** (a) strike `no sources cited` from R4 and note that an uncited paragraph is
identified by having no `Sources:`/`Verdict:` pair at all; or (b) render the pair for every
paragraph, so an uncited one carries `Verdict: no sources cited - ...`. (a) is smaller and
matches the current tests.

---

## 3. [Minor] Raw exception strings reach the reader-facing `Verdict:` line

**What:** a per-paragraph failure renders as e.g.
`Verdict: not verified - JSONDecodeError: Expecting value: line 1 column 1 (char 0)`.

**Why it matters:** R4's stated preference is verdict wording a production or field
technician can read. `JSONDecodeError` is not that.

**Why not fixed here:** the implementation plan specified this string verbatim and it
mirrors the pre-existing `_check_one` behavior, so it is a plan-level choice rather than a
deviation. The same detail is also in `check_failures`, which `## Gaps and disclosures`
prints — so the diagnostic is not lost if the reader-facing line is softened.

**Suggested fix:** render a plain sentence ("This paragraph could not be checked because
the verification step failed.") on the `Verdict:` line and leave the exception text to
`check_failures`.

---

## 4. [Minor] `## Conflicting sources` names the sources but not the disagreement

**What:** the block lists one link per pooled source. The model's `detail` — the only
statement of WHAT the sources disagree about — is never printed there.

**Fixed partially:** the accompanying sentence used to promise "both positions are given
below", which was untrue of a list of bare links; it now says the sources it read are
listed below. The underlying thinness remains.

**Suggested fix:** print the conflicting paragraph's `verdict.detail` in the block. One
line, and it turns a list of URLs into an actual disclosure.

---

## 5. [Minor, cosmetic] Deferred cleanups

- `harness/paragraphs.py` — the three inline `re.match`/`re.sub` patterns in
  `strip_markers`'s per-line loop are the module's only uncompiled patterns; hoisting them
  beside `_FENCE_RE` would match the file's own convention. The
  `indent_match ... else ""` guard is unreachable (`[ \t]*` always matches) and exists
  only to satisfy mypy.
- `harness/verify.py` — `sources_conflict = bool(...)` accepts any truthy value, so a
  quoted `"false"` from the model would read as True, unlike `detail` and
  `unsupported_items` which are both type-checked. Raised by the PR #7 review as
  PLAUSIBLE; left because the prompt asks for an unquoted boolean and no live reply has
  ever been captured. Confirm or drop it on the first live run (item 7).
- `harness/verify.py` — the `registered` list could be built as
  `[(sid, src) for sid in paragraph.source_ids if (src := registry.get(sid)) is not None]`,
  dropping a second `registry.get` call and an `assert source is not None`.

---

## 6. [Accepted limitation] A hard-wrapped bullet's continuation is not in `Paragraph.items`

`extract_claims` used to join a wrapped continuation line onto its bullet. `items` now
captures only lines that themselves start with a list marker, so a continuation line lives
in `text` but not in `items`. Accepted rather than fixed: `items` drives only bullet
indexing and the `n/m` rollup, both of which stay correct because the bullet COUNT is
unchanged, and the model judges `text`, which is complete. Revisit only if a wrapped
bullet's `*` ever lands on the wrong line.

---

## 7. [Open] The pooled reply contract has not been confirmed against a live model

Plan risk #1 says to confirm the pooled JSON contract "with a live run, not only scripted
replies". That did not happen in this session — it needs API keys and a reachable SearXNG,
neither available here. The three tolerances risk #1 predicted (prose around the JSON, a
missing `unsupported_items`, 1-based bullet indices) now each have a test that fails if the
tolerance is removed, but a scripted test only proves the parser survives what we IMAGINED
the model does. The first live run is still the real check — watch for `check_failures`
entries and for `Verdict: not verified` lines in the first real report.
