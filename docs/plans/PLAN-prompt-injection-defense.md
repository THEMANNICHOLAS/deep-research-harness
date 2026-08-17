# PLAN: Prompt Injection Defense

**Status:** In Progress
**Created:** 2026-08-15
**Reconciled:** 2026-08-17 — rewritten against the post-loop tree (the PR #16 original was
scoped before harness/agent.py, report.py, verify.py, runlog.py existed; see D5-D7).
**Type:** Single plan

## Intent

**True goal:** Harden the research harness against prompt injection arriving in untrusted
web content (fetched pages and SearXNG result titles/snippets), layered on top of the
existing per-role tool allocation. Detection + blocking as the active layer, structural
containment as the floor. The operator pastes finished reports into other Claude chats,
so the report itself is a downstream trust boundary.

**Binding outcomes:**
- **R1** — Sources carrying likely injection are detected and blocked before their content
  reaches any model OR any disk write; blocked sources are excluded from the answer AND from
  the final report body, with the blocking disclosed (drop + disclose).
  - False positives on pages that merely discuss/quote injection techniques (LLM-security
    blogs, docs) are accepted cost — thinner coverage, disclosed.
- **R2** — Fetched content cannot steer the harness into fetching attacker-chosen URLs
  carrying smuggled data (exfiltration channel closed). Policy decided in design: strict
  provenance — only search-result and user-supplied URLs are fetchable (see D2).
- **R3** — Injection never survives verbatim into the saved markdown report. Reports are
  pasted into other LLM chats as trusted user input; what lands in a report must be safe
  to paste.
- **R4** — Containment floor: a source that evades detection can influence answer text at
  worst — it can never trigger tool calls, writes, or fetches on its own authority.
- **R5** — A citation ID `[Sn]` exists only for sources that passed the guard and fetched
  successfully. Blocked and failed fetches (timeout, error, non-HTML, no-result) are
  disclosed by URL, never carry an ID, never leave a capture file on disk, and never reach
  a model or report as content. (Added at reconciliation, developer-confirmed 2026-08-17.)

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- Detection quality worth paying for, but not at the cost of making every fetch
  interactive or dominating per-page latency on the homelab box.
- Prefer mechanisms testable offline with fixtures (matches the existing suite).

**Non-goals:**
- Malicious/compromised SearXNG instance or compromised dependencies (supply chain).
- Protecting downstream chats beyond what R3 provides.
- Interactive per-source operator prompts (blocks unattended runs).
- Injection via the user's own typed queries.

**Constraints & assumptions:**
- Best-effort + disclose invariant applies: degraded coverage is answered and disclosed.
- Tests stay offline/fixture-based.
- Any model-based detection is config-routed (harness.toml + env), never hardcoded.
- The agent loop, report writer, and verifier all exist now — R3 and R4 are enforced in
  code inside them by this plan, not handed off as contracts (supersedes the original
  "parallel session" constraint; see D7).

**Open questions:**
- none — R1 mechanism (heuristics + spotlighting), R2 policy (strict provenance), pipeline
  ordering (D5), and the identity-model migration (D6) were all decided in design.

## Background

- Industry consensus (2026): prompt injection cannot be fully prevented; effective
  defense is architectural containment (provenance tracking in code, tool isolation)
  layered with best-effort detection. CaMeL (DeepMind 2025, "Defeating Prompt Injections
  by Design") demonstrated provenance-tracked agents stop the AgentDojo attack class at
  ~7 points of task-utility cost (77% vs 84% undefended).
- Naive ``` fencing of untrusted content is escapable by content containing the fence
  itself; the hardening is a random per-use boundary token plus stripping any occurrence
  of the boundary inside the content ("spotlighting", Microsoft).
- LangChain ships no guardrails module; nothing to reuse from `langchain-core` here.
- Heuristic signal families and test fixtures derive from public corpora: OWASP LLM01
  cheat sheet, ProtectAI LLM Guard / Rebuff rule sets, deepset prompt-injection dataset.

## Codebase Map

All facts subagent-verified 2026-08-17 against this worktree.

- Fetch pipeline: `harness/tools/fetch.py` — `_fetch(urls, config, registry, run_log)
  -> tuple[str, list[FetchedPage]]` (line 289) is the single shared pipeline: HTML batch
  (355-380) and PDF batch (404-430) each run `_markdown_of` → `classify` → `registry.add`
  (371/421; no-result pages mint inline via `_no_result_page`, 279), converge into one
  `pages` list keyed by URL (432), then the capture-write loop (`_write_source_file`,
  441-442; filename `{source_id}.md`, 200) and finally `_render` (444). `FetchedPage`
  (117-128): `source_id` is a required field consumed by both the capture filename and the
  `## [Sn] url` render heading (237). Failures today DO mint Sn and DO write
  `FETCH_FAILED` stub captures (212-223) — R5 removes both.
- Second fetch surface: `harness/tools/fallback.py` — `fetch_raw` → `_fetch_raw` (24-57)
  calls fetch.py's `_fetch` (33), discards its rendered content, re-renders per page via
  `_render` (41), wraps results in `<undigested source="..." reason="...">` markers (50-54).
  No duplicated crawl logic; a hook inside `_fetch`/`_render` covers both tools.
- Call-site counts: `_fetch` — 2 callers (fetch.py:482, fallback.py:33). `_render` — 2
  callers (fetch.py:444, fallback.py:41). Editing `_render`'s body covers both.
- Search: `harness/tools/search.py` — `build_search_tool` (201), `search_web` (224),
  `SearchFailure` (30) typed-failure convention.
- Tool wiring: `harness/tools/__init__.py` — `build_tools(config, registry, ...)` (24);
  `fetch_pages` is routed only to the reader tool set (29-33), `fetch_raw` to the
  researcher — structural provenance at the wiring layer, but no in-tool URL check.
- Registry: `harness/sources.py` — `SourceRegistry` (205); `add(url, title=None) -> str`
  (221) idempotent per `normalize_url` (227-229), title stored verbatim first-write-wins;
  `link()` (252) emits `[hostname](raw url)`; `resolve(text)` (266); `is_failed_capture`
  (32-39) + `sources_dir` are the capture-file policy shared by report/verify/tests.
- Agent loop: `harness/agent.py` — lead owns only `ask_user`; `_researcher_spec` (224-271)
  owns `search_web`/`fetch_raw`; `_reader_spec` (177-221) owns `fetch_pages`.
  `_ReaderDigestMiddleware.awrap_tool_call` (118-149) sees every task digest post-tool /
  pre-model (bookkeeping only today). `interrupt_on` confined to `ask_user`; nothing
  anywhere parses tool-result text for tool calls or URLs to follow (R4 already holds
  structurally — this plan pins it with regression tests).
- Report: `harness/report.py` — `_render_body` (468) is the single funnel every text
  surface passes through before `write_report` (532) writes to disk: model answer,
  registry links (185/193/234), workspace notes read verbatim (262-303), runlog incident
  details (441, `## Gaps and disclosures`). `_is_usable` (141-158) re-reads captures by
  `{sid}.md`. No sanitizer exists today (`strip_markers` is citation-marker-only).
- Verifier: `harness/verify.py` — `verify_paragraphs` (123) re-reads captures (151-185)
  and pools raw page text into a `HumanMessage` (210-212). Untrusted text → model path
  the original plan never saw; covered via capture gating (D5) + fencing (Phase 5).
- Disclosure channel: `harness/runlog.py` — `Incident(kind, detail)` (13-19),
  `RunLog.record` (34); rendered live to terminal and in the report's gaps section.
- Config: `harness/config.py` — settings groups subclass `_StrictModel` (37,
  extra="forbid"); mirror `FetchSettings` (77) + `[fetch]`. harness.toml sections today:
  `[providers.opencode]`, `[roles.*]`, `[fetch]`, `[search]`, `[agent]`.
- Prompts: `harness/prompts.py` loads `harness/prompts/*.md`; five files now —
  `orchestrator.md`, `subagent.md`, `reader.md`, `verify.md`, `verify_summary.md`.
- Tests: pytest + pytest-asyncio (auto). `tests/conftest.py`: `install_crawler` (116-137,
  fakes crawl4ai at `_crawler_class`), `ScriptedChatModel` (140-224), `patch_run`
  (251-296, whole-loop harness), `install_search_transport` (299-315), `make_config`
  (406-464), capture/note writers (348-390). Whole-loop pattern: `tests/test_delegation_e2e.py`.
  Coverage floor 90% enforced in CI only.
- Commands: `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` /
  `uv run mypy .`

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- No worker-model (LLM-judge) detection in this plan — heuristics only; the judge is a
  backlog item (see D1).
- No per-run escape hatch widening strict provenance to link-following — strict only,
  by explicit developer choice (see D2); an escape hatch can be added later if research
  reach suffers.
- No per-domain allowlists/denylists — provenance is the mechanism, not curation.
- No rewriting of blocked content into a "cleaned" version — the guard blocks or passes
  whole pages; surgery on attacker text is a losing game (D1). Byte hygiene on PASSED
  pages (Phase 3) is mechanical stripping, not content repair.

## Design Decisions

### D1: R1 detection mechanism
- **Chosen:** Heuristic scanner (regex/scoring rules over five signal families:
  instruction-override phrases, role/format spoofing, AI-directed addressing,
  obfuscation artifacts, exfil-shaped markup) + spotlighting (random-boundary fencing
  of all untrusted content in tool results). No model involved.
- **Rejected:** Worker-model LLM-judge per page — better semantic coverage, but a new
  config role and offline-mock burden; goes to backlog as a config-gated later layer.
  Heuristics-without-spotlighting also rejected — fencing is nearly free at `_render`.
- **Consequences:** Detection catches syntactic attacks only; semantic steering is
  stopped by containment (R2/R4), not detection. Fixtures must include benign
  security-topic samples to bound false positives.

### D2: R2 fetch policy
- **Chosen:** Strict provenance — a URL is fetchable only if it arrived from SearXNG
  results or was supplied by the user; enforced in code via the per-run
  `SourceRegistry`, which all fetch surfaces already share.
- **Rejected:** Sanitized link-following (strip query/fragment from in-page links) —
  residual exfil channel via path segments and attacker-chosen domains.
  Provenance + config escape hatch — developer chose strict; revisit only on evidence
  that research reach suffers.
- **Consequences:** The loop cannot follow links discovered inside pages; such URLs are
  rejected pre-crawl as a disclosed outcome. User-supplied URLs must be approved at run
  start by `harness/__main__` (in-repo now, not a handoff — see D7).

### D3: Single guard module
- **Chosen:** One new module `harness/guard.py` holding scan, fence, and
  report-sanitize functions; fetch/search/verify/report all call into it.
- **Rejected:** Inline logic per consumer — quadruplicates patterns and fixtures.
- **Consequences:** guard.py is dependency-light (stdlib + pydantic only).

### D4: Blocked-by-guard disclosure rides the RunLog (revised at reconciliation)
- **Chosen:** A blocked page is dropped from the pipeline entirely and disclosed as a
  `RunLog` incident (URL + fired signal families) — surfacing in the live terminal and
  the report's existing `## Gaps and disclosures` section with zero new plumbing.
  Blocked search results disclose the same way.
- **Rejected:** The original "new `FetchOutcome` value rendered like failure buckets" —
  written before `runlog.py` existed; under R5 blocked pages never reach `_render`, so a
  render-side outcome has nothing to attach to. Silent filtering — violates
  best-effort + disclose.
- **Consequences:** New incident kind (`guard_blocked`); the report's disclosure section
  gets blocked counts for free; per-page render never mentions blocked sources.

### D5: Firewall pipeline — scan sits inside `_fetch`, before capture and mint
- **Chosen:** One scan site inside fetch.py's `_fetch`, applied to raw markdown BEFORE
  `classify`, `registry.add`, and `_write_source_file`. Pipeline becomes: crawl → scan
  (drop blocked + disclose) → classify survivors → mint Sn for successes only → sanitize
  survivor markdown → capture to disk → render fenced. Pages already buffer in memory
  (`pages_by_url`) so the reorder costs no RAM/latency worth measuring.
- **Rejected:** Per-tool-factory wiring (`build_fetch_tool` + `build_fallback_tool`) —
  two insertion points, any future fetch surface starts unscanned, and it sits AFTER the
  capture write, so blocked bytes land on disk where verify.py (151-185) and report.py
  (152-156) re-read them by filename convention, silently bypassing R1. Middleware seam
  (`awrap_tool_call`) — runs after the reader's model already saw the text; a complement,
  not a substitute.
- **Consequences:** Disk becomes a protected zone: verify.py and report.py inherit R1
  transitively because a blocked source has no capture file. The guard is invisible from
  the tool factories — each factory docstring must name it. verify.py needs no second
  scan, only fencing (Phase 5).

### D6: Identity-model migration — Sn only for guarded successes (R5)
- **Chosen:** `registry.add` moves after the scan+classify point and runs only for
  `outcome == "fetched"` pages. Failures are disclosed by URL (RunLog + render line);
  the `FETCH_FAILED` stub-capture policy is deleted; `is_failed_capture` and
  `_holds_successful_capture` go with it (captures dir now holds only clean successes).
- **Rejected:** Mint-and-mark-blocked (quarantine) — fits the old convention but the
  developer chose one rule: "Sn = a real source with content." Never-mint for blocked
  only — leaves the blocked/failed inconsistency in place.
- **Consequences:** The convention shows up in 39 references across 13 files (4 harness
  modules, 5 test files) — this is the plan's biggest regression surface (!#3).
  `FetchedPage.source_id` becomes `str | None`; render headings for failures switch to
  URL-only. verify/report capture-reading simplifies (no failed-stub check needed).

### D7: R3/R4 enforced in code here (supersedes the "parallel session" contracts)
- **Chosen:** The original plan's three handoff contracts become in-repo work:
  `sanitize_for_report` is CALLED by report.py's `_render_body` (the single funnel,
  report.py:468); user-URL approval is wired where the run starts; R4 becomes regression
  tests pinning the already-true structural properties (no parsing of tool-result text
  for tool calls; `interrupt_on` confined to `ask_user`; URLs enter fetches only as
  model-chosen tool args).
- **Rejected:** Keeping contracts-on-paper with no counterparty — the parallel session
  finished and merged; an uncalled sanitizer is a rule that degrades with every edit.
- **Consequences:** Phase 5 grows to touch report.py and verify.py; the plan's diff is
  larger but R3/R4 close instead of being documented aspirations.

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Detect & block before model or disk | Phase 2 (scanner), Phase 3 (wired pre-capture, both fetch surfaces + search) |
| R2 | No exfil via fetch | Phase 4 (provenance enforcement in `_fetch`) |
| R3 | Report hygiene | Phase 5 (sanitize_for_report wired into `_render_body`; registry/title hygiene) |
| R4 | Containment floor | Phase 5 (fencing + regression tests over the existing loop) |
| R5 | Sn only for guarded successes | Phase 1 (identity migration) |

## Progress
- [x] Phase 1: Identity-model migration
- [x] Phase 2: Guard scanner core
- [x] Phase 3: Firewall wiring in fetch and search
- [x] Phase 4: Strict URL provenance
- [ ] Phase 5: Spotlighting, report hygiene, containment tests
- [ ] Final verification

## Phases

### Phase 1: Identity-model migration
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** `[Sn]` IDs exist only for successfully fetched sources; failures are disclosed
by URL with no ID, no capture file, and no stub policy — independent of the guard, so the
riskiest refactor lands and stabilizes first.
**Requirements:** R5
**Files:**
- `harness/tools/fetch.py` — move `registry.add` after classification, gate on
  `outcome == "fetched"`; `FetchedPage.source_id: str | None`; failure render lines and
  `_no_result_page` become URL-only; capture loop writes successes only
- `harness/sources.py` — delete `is_failed_capture` and the stub policy
- `harness/report.py`, `harness/verify.py` — drop failed-stub handling when reading captures
- `tests/test_fetch.py`, `tests/test_report.py`, `tests/test_verify.py`,
  `tests/test_display.py`, `tests/conftest.py` — rewrite assertions bound to the old convention
**Diff budget:** ~200-350 lines across 8-9 files

**Reuse:**
- Disclosure: `RunLog.record` per harness/runlog.py — failures already record
  `fetch_failed`; keep that, keyed by URL not Sn
- Pattern to mirror: existing typed-outcome rendering in fetch.py (Codebase Map)

**Contracts:**
- After this phase: the captures dir contains ONLY `{Sn}.md` files for successful
  fetches — Phases 3/5 and verify/report rely on "a capture file exists ⇒ content is
  real page text"
- `FetchedPage.source_id: str | None` — `None` for every non-fetched outcome

**Out of scope:**
- No guard/scan logic (Phase 2-3); no provenance (Phase 4); no sanitization or fencing
- No behavior change to WHICH pages fetch or how outcomes classify — identity only

**Tests (write first, confirm red):**
- [ ] A failed fetch (timeout/error/non_html/no-result) mints no Sn, writes no file, and
  renders by URL with its outcome disclosed
- [ ] A successful fetch mints Sn, writes `{Sn}.md`, renders `## [Sn] url` as today
- [ ] A URL that fails then succeeds on a later call mints a fresh Sn normally
- [ ] report/verify handle a registry containing only successful sources (no stub-file
  special cases remain)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Reorder `_fetch` (mint post-classify, success-only) and make `source_id` optional.
3. Delete the stub policy; simplify report/verify capture reads; update touched tests.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] Full suite green: `uv run pytest`; `uv run mypy .` clean on the `str | None` change

### Phase 2: Guard scanner core
**Risk:** flagged (!#1)
**Test-first:** required
**Goal:** A pure, offline-testable heuristic scanner that scores text for injection
signals and returns a block/pass verdict with the signals that fired.
**Requirements:** R1
**Files:**
- `harness/guard.py` — new; scanner rules + `ScanResult` model (new file per D3: shared
  by fetch, search, verify, and report)
- `tests/test_guard.py` — new; behavior tests over real attack fixtures
- `tests/fixtures/injection/` — new; attack samples (from public corpora) + benign
  security-topic samples (false-positive bounds)
**Diff budget:** ~250-400 lines across 3-6 files

**Reuse:**
- Pydantic models: `_StrictModel`-style `ConfigDict(extra="forbid")` per harness/config.py
- Test naming/convention exemplar: `tests/test_fetch.py` (behavior sentences, R-ID comments)

**Contracts:**
- `scan(text: str) -> ScanResult` where `ScanResult.blocked: bool` and
  `ScanResult.signals: list[str]` — consumed by Phase 3
- Signal families (stable names, one per family): `instruction_override`,
  `role_spoofing`, `ai_directed`, `obfuscation`, `exfil_markup`

**Out of scope:**
- No integration into fetch/search (Phase 3); no config wiring (Phase 3)
- No model-based detection; no fencing/sanitization functions yet (Phase 5)
- No tuning beyond the fixture set — do not invent regexes without a fixture that fires them

**Tests (write first, confirm red):**
- [ ] Each signal family blocks at least one real attack fixture and names the family in
  `signals`
- [ ] Benign fixtures pass: ordinary article text, and a security-blog sample QUOTING
  injection phrases inside code blocks (documents the accepted-cost boundary — where we
  do still block, the test asserts the block, citing R1's accepted-cost line)
- [ ] Empty/whitespace text passes; scanning is deterministic (same input, same result)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Collect attack + benign fixtures from public corpora into `tests/fixtures/injection/`.
3. Implement `ScanResult` and `scan` with per-family rule lists.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `uv run mypy .` and `uv run ruff check .` clean with the new module

### Phase 3: Firewall wiring in fetch and search
**Risk:** none
**Test-first:** required
**Goal:** Every fetched page (both tools, HTML and PDF) is scanned before classify, mint,
or disk; blocked pages vanish from the pipeline and are disclosed via RunLog; survivor
markdown is byte-sanitized before capture. Search titles/snippets scanned the same way.
**Requirements:** R1
**Assumes:**
- Phase 1's success-only minting and Phase 2's `scan` contract are merged and green
**Files:**
- `harness/tools/fetch.py` — scan inside `_fetch` on raw markdown before classify/mint
  (D5); drop blocked pages; `run_log.record("guard_blocked", ...)`; strip zero-width/
  control chars from survivor markdown before `_write_source_file`; factory docstring
  names the guard (D5 consequence)
- `harness/tools/search.py` — scan title+snippet per parsed result; blocked results
  dropped + disclosed via RunLog
- `harness/config.py` — new `GuardSettings` (`_StrictModel`) + field on `HarnessConfig`
- `harness.toml` — new `[guard]` section (enabled flag)
- `tests/test_fetch.py`, `tests/test_fallback.py`, `tests/test_search.py` — behavior tests
**Diff budget:** ~150-250 lines across 6-7 files

**Reuse:**
- `scan` from harness/guard.py (Phase 2 contract) — no inline rules
- Disclosure: `RunLog.record` (D4) — no new disclosure mechanism
- Config pattern: mirror `FetchSettings`/`[fetch]` per Codebase Map
- Fakes: `install_crawler` + `install_search_transport` fixtures in tests/conftest.py

**Contracts:**
- Pipeline order inside `_fetch` is FROZEN: scan → classify → mint → sanitize → capture
  → render (D5) — Phases 4/5 and verify/report depend on "nothing blocked ever reaches
  disk"
- Incident kind `guard_blocked`, detail carries URL + fired signal families

**Out of scope:**
- No provenance checks (Phase 4); no fencing (Phase 5)
- No changes to truncation or crawl behavior; no new tool in `build_tools`

**Tests (write first, confirm red):**
- [ ] A page carrying an attack fixture: no Sn, no capture file, absent from rendered
  content, `guard_blocked` incident recorded with its URL — asserted through BOTH
  `fetch_pages` and `fetch_raw` (shared `_fetch` covers both; prove it)
- [ ] A blocked PDF-pass page is dropped identically to an HTML-pass page
- [ ] Survivor markdown on disk carries no zero-width/control chars
- [ ] A search result with an injected snippet is dropped + disclosed; clean results render
- [ ] `[guard] enabled = false` bypasses scanning (config-driven, both tools)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add `GuardSettings` + `[guard]`; thread config into the pipeline.
3. Insert scan + sanitize into `_fetch` per the frozen order; wire search-side scanning.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] Full suite green: `uv run pytest`

### Phase 4: Strict URL provenance
**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** `_fetch` refuses any URL that did not arrive from SearXNG results or explicit
user approval, pre-crawl, as a disclosed outcome (R2 closed structurally for both tools).
**Requirements:** R2
**Assumes:**
- The per-run `SourceRegistry` is shared by all tools (confirmed: `build_tools(config,
  registry)`); Phase 1's URL-keyed failure disclosure is in place
**Files:**
- `harness/sources.py` — provenance set on `SourceRegistry`: `approve(url)` and
  `is_approved(url)` keyed by `normalize_url`
- `harness/tools/search.py` — every parsed result URL approved on ingestion
- `harness/tools/fetch.py` — unapproved URLs rejected inside `_fetch` pre-crawl (covers
  `fetch_pages` AND `fetch_raw` at one site), disclosed by URL; never passed to the crawler
- `harness/__main__.py` — user-supplied URLs approved at run start (in-repo per D7)
- `tests/test_sources.py`, `tests/test_fetch.py`, `tests/test_fallback.py`,
  `tests/test_search.py` — behavior tests
**Diff budget:** ~100-180 lines across 6-7 files

**Reuse:**
- `normalize_url` in harness/sources.py for approval keying — do NOT re-normalize ad hoc
- Rejection disclosure: RunLog, same shape as Phase 3's `guard_blocked`

**Contracts:**
- `SourceRegistry.approve(url: str) -> None` / `is_approved(url: str) -> bool` — the only
  sanctioned way to widen fetchability
- Rejection is per-URL: one bad URL in a batch never fails the approved ones

**Out of scope:**
- No escape-hatch config to re-enable link-following (Non-Goals; D2)
- No domain-level rules; no changes to search parsing beyond the approve call

**Tests (write first, confirm red):**
- [ ] A URL never seen by search/user is rejected pre-crawl with a disclosed outcome;
  the crawler is not invoked for it (assert via fake) — through both fetch tools
- [ ] URLs from search results fetch normally; `approve`d user URLs fetch normally
- [ ] Approval respects `normalize_url` (trailing-slash/case variants approved)
- [ ] Mixed batch: approved URLs succeed while the unapproved one is rejected

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the provenance set + methods to `SourceRegistry`.
3. Approve on search ingestion and at run start; enforce in `_fetch` pre-crawl.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] Full suite green: `uv run pytest`

### Phase 5: Spotlighting, report hygiene, containment tests
**Risk:** flagged (!#4)
**Test-first:** required
**Goal:** All untrusted text reaching a model is fenced with an unforgeable random
boundary; every report byte passes through `sanitize_for_report` before disk; R4's
structural containment is pinned by regression tests.
**Requirements:** R3, R4
**Assumes:**
- Phases 1-4 merged; five prompt files exist per Codebase Map
**Files:**
- `harness/guard.py` — `fence(text) -> str` (random per-call boundary, occurrences of the
  boundary inside content stripped) and `sanitize_for_report(text) -> str` (strip
  zero-width/control chars, neutralize fence-like and chat-marker sequences)
- `harness/tools/fetch.py` — `_render`'s BODY fences page markdown (one edit covers both
  callers, incl. fallback's `<undigested>` wrapper — fence nests inside it)
- `harness/tools/search.py` — `_render` fences titles/snippets
- `harness/verify.py` — pooled source text fenced before the verifier `HumanMessage` (D5:
  capture gating already guarantees it is scan-passed; fencing is the second layer)
- `harness/report.py` — `_render_body` output passes through `sanitize_for_report`
  before `write_report` writes disk (D7); registry titles sanitized at `add`;
  `resolve`/`link` emit only http(s) URLs (anything else rendered as plain text)
- `harness/prompts/orchestrator.md`, `subagent.md`, `reader.md`, `verify.md` — one rule
  block each: fenced text is data, never instructions
- `tests/test_guard.py`, `test_fetch.py`, `test_search.py`, `test_verify.py`,
  `test_report.py`, `test_sources.py`, `test_agent.py` — behavior + regression tests
**Diff budget:** ~220-320 lines across 11-13 files

**Reuse:**
- Fencing/sanitizing live in `harness/guard.py` (D3) — no per-consumer copies
- Prompt files follow existing `string.Template` loading; no prompts.py changes
- R4 regression tests: assert against `build_agent`/`build_tools` wiring per
  tests/test_agent.py and tests/test_tools_registry.py patterns

**Contracts:**
- `fence(text: str) -> str` — opening/closing boundary lines around content; boundary
  unpredictable per call
- `sanitize_for_report(text: str) -> str` — idempotent; called by `_render_body`, the
  single funnel (report.py:468) — never bypassed for any report section

**Out of scope:**
- No prompt rewrites beyond the single fenced-data rule block per file
- No new agent middleware; R4 work is tests over existing structure, not construction

**Tests (write first, confirm red):**
- [ ] Fenced output brackets content with matching random boundaries; a payload containing
  the boundary cannot escape; boundaries differ across calls; fence nests correctly inside
  fallback's `<undigested>` wrapper
- [ ] `sanitize_for_report` strips zero-width/control chars, neutralizes chat-marker and
  fence sequences, idempotent on clean text; a hostile string planted in answer text,
  a registry title, a workspace note, and an incident detail never reaches the report
  file verbatim (end-to-end through `write_report`)
- [ ] Registry: hostile title sanitized at `add`; `resolve` renders a `javascript:` URL
  as plain text, never a link
- [ ] Fetch/search `_render` output and verify's pooled sources block are fenced
- [ ] R4 regression: `fetch_pages`/`fetch_raw`/`search_web` are reachable only from their
  assigned tiers; `interrupt_on` contains exactly `ask_user`; no code path feeds
  tool-result text back as tool-call input (pin via wiring assertions)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `fence` and `sanitize_for_report` in guard.py.
3. Wire fencing into both `_render`s and verify.py; wire `sanitize_for_report` into
   `_render_body`; sanitize registry ingestion and link emission.
4. Add the fenced-data rule block to the four prompt templates.
5. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Full suite green + all four quality gates (see `## Verification`)

## Verification
- [ ] `uv run pytest` — full suite green
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] Manual: run the fetch tool against a local fixture page containing a known attack
  string (offline fake) and observe drop + disclosure end to end — no Sn, no capture
  file, a `guard_blocked` line in the gaps section of the written report

## Notes
- Backlog items to record in docs/backlog.md during implementation (directed by the
  original plan but never filed — confirmed absent 2026-08-17): worker-model LLM-judge
  detection layer (config-gated, D1); provenance escape hatch if research reach
  measurably suffers (D2).
- The original plan's "parallel session" handoff contracts are superseded by D7; if any
  external doc still references them, the reconciled truth is this file.

## Risks
#1. **Heuristic false-positive/false-negative rates are unmeasured** — the rules are
    only as good as the fixture corpus. Chosen: real public attack corpora as fixtures
    plus benign security-topic samples to bound false positives; accepted cost per R1.
    Confirm during implementation that every rule has a firing fixture and no benign
    fixture blocks unless the plan's accepted-cost line covers it.
#2. **Strict provenance may starve research tasks that need link-chasing** — chosen
    deliberately (D2) with disclosure on every rejection, so starvation is visible in
    reports, not silent. If reach suffers, the backlog escape hatch is the remedy;
    do not weaken enforcement inside this plan.
#3. **Identity-model migration has the plan's widest blast radius** — the old
    failures-mint-Sn + stub-capture convention appears in 39 references across 13 files
    (4 harness modules, 5 test files). Regression risk concentrates in report/verify
    disclosure behavior. Mitigation: it runs FIRST (Phase 1), isolated from all guard
    logic, so any breakage attributes cleanly; the whole suite is the gate before
    Phase 2 starts.
#4. **Fencing interacts with existing render structure** — fallback.py already wraps
    pages in `<undigested source=...>` markup (50-54), and `_render` does boundary-aware
    truncation; a careless fence could break either, and boundary-stripping could mangle
    legitimate content that resembles the boundary. Mitigation: fence nests inside the
    wrapper (tested), boundary tokens are random enough that collisions are adversarial
    not accidental, and truncation runs before fencing so the fence never gets cut.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

### 2026-08-17 — Phase 2 review findings (deferred by developer: "I'll do at home")
- **[Major, resolved 2026-08-17]** (U+200D deleted from the char class pre-Phase-3,
  developer-approved; guard tests green) harness/guard.py obfuscation char class includes U+200D (ZWJ) with no
  firing fixture — breaches the phase's no-regex-without-fixture rule, and ZWJ is common in
  benign content (compound emoji, Indic/Persian scripts), so it will false-positive once
  Phase 3 wires the scan. Fix: delete the character or add a justifying fixture. MUST be
  resolved before/with Phase 3.
### 2026-08-17 — Phase 4 gap decided (developer, this session)
- No user-URL input path existed in `harness/__main__.py` (CLI takes one question string).
  Developer decision: the prompt stays a plain question — no `--url` flag; http(s) URLs
  found INSIDE the question text are extracted and `approve`d at run start, so a pasted
  "read this page" URL stays fetchable under strict provenance. Guard-blocked search
  results are NOT approved (only survivors of `_drop_guarded` are) — the plan's "every
  parsed result URL approved on ingestion" predates Phase 3's drop.

### 2026-08-17 — Phase 3 review finding (deferred)
- **[Simplify, deferred]** `_guard_blocked_detail` is byte-identical in
  harness/tools/fetch.py and harness/tools/search.py; per D3 the formatter belongs in
  harness/guard.py with both tools importing it. Defer to Phase 5, which touches guard.py
  anyway.

- **[Minor, open]** guard.py role_spoofing rule `^\s*\[?system\]?\s*:` (MULTILINE, brackets
  optional) blocks benign "System: Ubuntu 22.04"-style lines. Within risk #1's accepted
  breadth; watch the `guard_blocked` rate once Phase 3 makes it observable, tighten if noisy.

## Phase Handoff Log

### 2026-08-17 — Phase 1: Identity-model migration
- Done: Sn IDs mint only for `outcome == "fetched"` pages (`FetchedPage.source_id: str | None`); failures write no capture, disclose by URL via the existing `fetch_failed` RunLog path; the FETCH_FAILED stub policy (`is_failed_capture`, `_holds_successful_capture`, conftest's `write_failed_capture`) is deleted repo-wide. Full suite 438 green; ruff/format/mypy clean; flagged-risk (!#3) judgment review clean.
- Learned: report's `_is_usable` and verify's pooled-read still handle a registered source with a missing/unreadable capture — that path stays reachable via the `capture_write_failed` OSError branch, so Phase 3/5 must not assume "registered => file exists", only "file exists => real page text". Four tests beyond the plan's named list needed the same mechanical fix (PDF-batch/heading tests in test_fetch.py, one in test_fallback.py).
- Drift: none.
- Watch-next: Phase 2 is greenfield (`harness/guard.py` + fixtures); the Phase 3 contract "a capture file exists => content is real page text" is now the invariant to protect when wiring the scan.

### 2026-08-17 — Phase 2: Guard scanner core
- Done: `harness/guard.py` (`scan` -> `ScanResult`, five regex signal families) + `tests/test_guard.py` (11 tests) + `tests/fixtures/injection/` (11 attack, 3 benign fixtures with README provenance map). Full suite 449 green; all quality gates clean.
- Learned: the benign security-blog fixture quoting override phrases IS blocked, asserted deliberately per R1's accepted-cost line — do not "fix" that test.
- Drift: none. Two review findings deferred by the developer — see `## Discoveries` 2026-08-17: [Major] U+200D in the obfuscation class has no firing fixture and will FP on emoji/Indic pages (resolve before/with Phase 3); [Minor] role_spoofing matches bare "System: ..." lines.
- Watch-next: resolve the ZWJ Major FIRST, then Phase 3 wires `scan` into `_fetch` per the frozen order scan -> classify -> mint -> sanitize -> capture -> render, plus `GuardSettings`/`[guard]` config and search-side scanning.
### 2026-08-17 — Phase 3: Firewall wiring in fetch and search
- Done: `scan` wired inside `_fetch` for both HTML and PDF batches per the frozen order scan -> classify -> mint -> sanitize -> capture -> render; blocked pages vanish (no FetchedPage/Sn/capture/render) and disclose via `guard_blocked` incidents (URL + families); search titles/snippets scanned per result via `_drop_guarded`; `strip_invisibles` added to guard.py and applied to survivor markdown unconditionally; `GuardSettings`/`[guard] enabled` config added (developer decision: enabled=false disables the SCAN only — sanitize still runs); conftest `make_config` gained `guard=`. Pre-phase: the deferred ZWJ [Major] resolved by deleting U+200D from the obfuscation class (developer-approved). Full suite 461 green, all gates clean, quality scan clean.
- Learned: scan-before-classify means a failed crawl whose error page fires a signal discloses as `guard_blocked`, not `fetch_failed` — by design (D5), noted for operator expectations. Blocked URLs have no `pages_by_url` entry, so the final pages assembly filters membership.
- Drift: none. One deferred simplify — `_guard_blocked_detail` duplicated in fetch.py/search.py, move to guard.py in Phase 5 (see `## Discoveries`).
- Watch-next: Phase 4 (strict provenance, flagged !#2) adds `approve`/`is_approved` to `SourceRegistry` keyed by `normalize_url`, approves on search ingestion and user URLs at run start (`harness/__main__.py`), enforces pre-crawl inside `_fetch` — rejection must be per-URL, never failing the approved batch.

### 2026-08-17 — Phase 4: Strict URL provenance
- Done: `SourceRegistry.approve`/`is_approved` (keyed by `normalize_url`) + `extract_urls`; `_fetch` rejects unapproved URLs pre-crawl per-URL (`provenance_rejected` incident each, batch survives); search approves guard-SURVIVOR result URLs only; `__main__` extracts and approves http(s) URLs from the question text (developer decision — no `--url` flag). Full suite 475 green, gates clean after one mechanical ruff-format fix in test_search.py; flagged-risk (!#2) judgment review clean (never-silent, per-URL, no widening all confirmed).
- Learned: the provenance check has NO config off-switch (deliberate — the no-escape-hatch non-goal); `[guard] enabled=false` still approves all parsed results since `_drop_guarded` returns everything. Existing tests that fetch without a search needed `approve_all` (new conftest helper) or URL-in-question arranges — the same will apply to any future test driving `_fetch` directly.
- Drift: none.
- Watch-next: Phase 5 (flagged !#4) — `fence`/`sanitize_for_report` in guard.py, wire into both `_render`s + verify's pooled block + report's `_render_body`, registry title/link hygiene, prompt rule blocks, R4 regression tests. Also fold in the deferred Phase 3 simplify: move `_guard_blocked_detail` into guard.py.

<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->
