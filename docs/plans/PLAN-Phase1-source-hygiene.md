# PLAN: Source Hygiene and Agent Hierarchy — Phase 1: Source hygiene & independent verification

**Status:** In Progress
**Created:** 2026-08-15
**Parent:** `PLAN-source-hygiene-and-hierarchy.md`
**Phase:** 1 of 2

## Context
Fixes the four report-quality defects observed in the 2026-08-15 live run: duplicate source
registrations of the same work, PDF URLs silently degrading to `non_html`, model-authored H1s
and meta sections breaking report structure, and the writer model grading its own answer.
See the parent plan for Intent (R1–R4), Codebase Map, and Design Decisions D1–D4, D7.

## Progress
- [x] Step 1: Canonical-URL dedup
- [x] Step 2: PDF fetch path
- [ ] Step 3: Report structure enforcement
- [ ] Step 4: Verifier role
- [ ] Phase verification

## Steps

### Step 1: Canonical-URL dedup
**Risk:** flagged (!#1)
**Test-first:** required
**Requirements:** R1
**Files:**
- `harness/sources.py` — extend `normalize_url()`: strip common tracking params
  (`utm_*`, `fbclid`, `gclid`, `ref`), plus an arxiv rule table mapping
  `/abs/<id>`, `/html/<id>[vN]`, `/pdf/<id>[vN]` and version suffixes to one canonical form.
- `tests/test_sources.py` — cases for each collapse rule and each must-NOT-collapse case.
**Diff budget:** ~60–120 lines across 2 files

**Reuse:**
- Extend `normalize_url` in `harness/sources.py` — do NOT add a second canonicalization
  function or a registry-side dedup pass (D1).
- Pattern to mirror: the existing normalization cases in `tests/test_sources.py`.

**Contracts:**
- `normalize_url(url: str) -> str` remains the single notion of "same source"; `add()`
  behavior (first-write-wins, stable sequential `Sn`) is unchanged.

**Out of scope:**
- Any non-arxiv host rules; content/title similarity; touching `SourceRegistry.add` itself;
  rewriting URLs at render time (the registered URL is what the report shows).

**Tests (write first, confirm red):**
- [x] arxiv abs/html/pdf/version variants of one work all return the same canonical form and
  therefore one `Sn` ID (the observed S21/S25 and S23/S26 dup pairs are the fixtures).
- [x] Tracking params are stripped; meaningful query strings are preserved.
- [x] Different arxiv works, and same-path URLs on other hosts, do NOT collapse.

**Details:**
Red→green. Keep the arxiv table a module-level dict/tuple of rules next to `normalize_url`,
not config — hosts are added by editing code when a real dup bites (D1 consequence).

**Acceptance criteria:**
- [x] Registering the six URLs from the 2026-08-15 report's dup pairs yields 4 distinct IDs,
  not 6 (named test — representative URLs of the observed dup-pair shapes; the literal
  live-run URLs were not retrievable locally, reports live on the homelab).

### Step 2: PDF fetch path
**Risk:** flagged (!#2)
**Test-first:** required
**Requirements:** R2
**Assumes:**
- crawl4ai 0.9.2's `PDFCrawlerStrategy`/`PDFContentScrapingStrategy` import cleanly once
  `pypdf` is installed (explorer-confirmed the classes and the guarded import).
**Files:**
- `pyproject.toml` — add `pypdf==<current>` (exact pin per convention; the `crawl4ai[pdf]`
  extra's engine).
- `harness/tools/fetch.py` — detect PDF URLs (extension and/or `application/pdf`
  content-type), route them through the PDF strategies, land extracted text in the normal
  fetched capture shape; empty/failed extraction writes the existing `FETCH FAILED` stub.
- `tests/test_fetch.py` — PDF branch tests (mocked crawler, per suite convention).
**Diff budget:** ~120–200 lines across 3 files (+ lockfile)

**Reuse:**
- Extend `classify()` and `_fetch()` in `harness/tools/fetch.py` — do NOT add a separate
  PDF tool; `fetch_pages` stays the one fetch surface.
- Pattern to mirror: the existing failure-stub path in `fetch.py` and its tests' mocked
  crawler fixtures.

**Contracts:**
- Capture file shapes are UNCHANGED: PDF success uses the `- Outcome: fetched` shape, PDF
  failure the `FETCH FAILED` stub — `report.py` and `verify.py` need no changes for R2.

**Out of scope:**
- OCR/image-only PDF recovery (that case is a disclosed failure by design); changing
  `per_page_char_cap` semantics; a PDF-specific config section; touching `sources.py`.

**Tests (write first, confirm red):**
- [x] A PDF URL routes to the PDF strategy (not Playwright) whether detected by extension or
  content-type, and its extracted text lands in a fetched-shaped capture.
- [x] Empty extraction and extraction exceptions each produce a `FETCH FAILED` stub —
  never a fetched capture with junk, never a silent `non_html`.
- [x] Non-PDF URLs are untouched by the new branch (existing tests stay green).

**Details:**
Red→green. Detection order: URL extension first (cheap, pre-fetch routing), content-type as
the post-fetch check for extensionless PDF URLs. Keep the PDF crawler construction inside
`_crawler_class()`/`_fetch` so tests can patch it the same way they patch the default
crawler. Char cap applies to extracted PDF text exactly as to markdown.

**Acceptance criteria:**
- [x] `uv lock` records exactly one new top-level dependency (pypdf), pinned `==` (6.16.1).

### Step 3: Report structure enforcement
**Risk:** none
**Test-first:** required
**Requirements:** R3
**Files:**
- `harness/prompts/orchestrator.md` — add the output template: the answer starts at `##`
  depth with a fixed section order; explicitly forbid meta/coverage/disclosure sections
  (the harness owns disclosure).
- `harness/report.py` — render-time demotion: shift any H1/H2 in the model answer down so
  everything nests under `## Answer`.
- `tests/test_report.py`, `tests/test_agent.py` — demotion cases; prompt-contract pin.
**Diff budget:** ~80–150 lines across 4 files

**Reuse:**
- Extend the existing `## Answer` rendering in `harness/report.py` — do NOT introduce a
  markdown-parsing dependency; heading demotion is a line-prefix transform.
- Pattern to mirror: `harness/paragraphs.py`'s line-level processing style and the existing
  prompt-contract pin tests in `tests/test_agent.py`.

**Contracts:**
- Demotion is mechanical and total: after rendering, no line inside `## Answer` starts with
  `# ` or `## ` (the report's own section headings stay the only H2s).

**Out of scope:**
- Restructuring `report.py`'s section order; validating/repairing the model's section
  ordering beyond heading depth; touching the reader or researcher prompts; any TUI change.

**Tests (write first, confirm red):**
- [ ] An answer containing `# Title` and `## Section` renders with both demoted below
  `## Answer`; heading depth ordering within the answer is preserved relatively.
- [ ] Code fences are not treated as headings (a `# comment` inside a fenced block is
  untouched).
- [ ] The orchestrator prompt names the structure and the meta-section prohibition (contract
  pin, same style as existing prompt pins).

**Details:**
Red→green. Demote in the one place the answer body is rendered, downstream of
`split_paragraphs` so paragraph/citation handling is unaffected.

**Acceptance criteria:**
- [ ] Rendering the saved 2026-08-15 answer text through the new renderer yields exactly one
  H1 in the whole report (the report title).

### Step 4: Verifier role
**Risk:** none
**Test-first:** required
**Requirements:** R4
**Files:**
- `harness.toml` — add `[roles.verifier]` (provider `opencode`, model `gpt-5.6-luna`).
- `harness/verify.py` — build role `"verifier"` instead of `"head"`.
- `harness/__main__.py` — preflight `verifier` at startup alongside `head` (fail-fast; an
  undeclared or unreachable verifier stops the run before any research is spent).
- `tests/test_verify.py`, `tests/test_agent.py` — role-selection and preflight tests;
  `tests/conftest.py` `make_config` gains the `verifier` role.
**Diff budget:** ~50–110 lines across 5 files

**Reuse:**
- Extend `build_chat_model`/`preflight` in `harness/models.py` usage — do NOT add a
  verifier-specific client builder; `patch_models_by_role` in `tests/conftest.py` already
  supports per-role models.
- Pattern to mirror: the existing `head` preflight call in `harness/__main__.py`.

**Contracts:**
- Config key `[roles.verifier]` — Phase 2's role renames (D7) treat this step's wiring as
  the template.
- `verify_paragraphs` signature is unchanged; only the role string inside moves.

**Out of scope:**
- Renaming `head`/`subagent` (Phase 2, D7); evidence-span quoting in verdicts (preference —
  implement only if trivially absorbed into the verify prompt, else leave); judge
  calibration (non-goal).

**Tests (write first, confirm red):**
- [ ] `verify_paragraphs` builds the `verifier` role, not `head` (asserted via
  `patch_models_by_role`).
- [ ] A config without `[roles.verifier]` fails startup preflight with a `ModelError` naming
  the role (no fallback to head — D4).

**Details:**
Red→green. The preflight addition mirrors the existing head preflight block including its
error-to-stderr, exit-1 shape.

**Acceptance criteria:**
- [ ] Run metadata in a written report names the verifier model (extend the metadata block
  if it does not already carry it — one line).

## Verification
- [ ] Quality gates: see the central plan's `## Verification` — full suite, ruff, format,
  mypy.
- [ ] Live run per the central plan's Phase 1 checklist (deduped sources, PDF evidence or
  disclosed failure, clean hierarchy, verifier in metadata).

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->

### 2026-08-15 — Step 1: Canonical-URL dedup
- Done: `normalize_url` extended with tracking-param stripping (`utm_*`, `fbclid`, `gclid`,
  `ref`) and a module-level arxiv rule table (abs/pdf/html, `vN`, trailing `.pdf`, `www.`
  alias → `arxiv.org/abs/<id>`); 16 new tests incl. the named 6-URLs→4-IDs acceptance test.
- Learned: the literal 2026-08-15 dup-pair URLs are only on the homelab — the acceptance
  test uses representative shapes (disclosed in its docstring). Session started with ~1,100
  lines of uncommitted prior-session runlog work; committed separately as d5893a6 before any
  phase work.
- Drift: none. Two 3F findings deferred to the central plan's `## Discoveries` (query
  re-encode asymmetry; netloc-construction reorder).
- Watch-next: Step 2 (PDF fetch path) adds pinned `pypdf` — pin the exact current version
  and confirm `uv lock` records exactly one new top-level dep.

### 2026-08-15 — Step 2: PDF fetch path
- Done: `pypdf==6.16.1` added; `_fetch` partitions batches by `.pdf` extension, new lazy
  `_pdf_crawler_parts()` seam, new `"pdf"` FetchOutcome as a one-shot content-type reroute
  signal (PDF-batch results classify with `"text/html"`, so no loop); empty extraction is
  force-failed into the `FETCH FAILED` stub. 6 new tests + repointed `non_html` rows.
- Learned: crawl4ai fixes crawler strategy at construction and scraping strategy per run
  config — mixed batches need two `arun_many` calls, which is why the partition exists.
  `PDFContentScrapingStrategy()` raises ImportError at CONSTRUCTION without pypdf.
  `fetch_raw` (fallback.py) reuses `_fetch`, so PDFs work there with no extra code.
- Drift: none. One 3F simplify deferred to central `## Discoveries` (duplicated
  result-is-None block between the two loops).
- Watch-next: Step 3 (report structure) — heading demotion must skip fenced code blocks;
  mirror `harness/paragraphs.py` line-level style.
