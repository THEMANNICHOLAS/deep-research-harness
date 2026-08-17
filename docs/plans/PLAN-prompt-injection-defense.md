# PLAN: Prompt Injection Defense

**Status:** Not started
**Created:** 2026-08-15
**Type:** Single plan

## Intent

**True goal:** Harden the research harness against prompt injection arriving in untrusted
web content (fetched pages and SearXNG result titles/snippets), layered on top of the
existing per-role tool allocation. Detection + blocking as the active layer, structural
containment as the floor. The operator pastes finished reports into other Claude chats,
so the report itself is a downstream trust boundary.

**Binding outcomes:**
- **R1** — Sources carrying likely injection are detected and blocked before their content
  reaches any model; blocked sources are excluded from the answer AND from the final
  report body, with the blocking disclosed (drop + disclose).
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
- The agent loop does not exist yet — this defense is being designed before/alongside it,
  so it can shape the loop rather than retrofit it. A parallel session is building the
  loop and report writer concurrently; this plan's Contracts are its interface.

**Open questions:**
- none — R1 mechanism (heuristics + spotlighting) and R2 policy (strict provenance)
  were decided in design; see D1, D2.

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

- Entry points: `harness/tools/fetch.py` — `build_fetch_tool` / `fetch_pages` (line ~254);
  `harness/tools/search.py` — `build_search_tool` / `search_web` (line ~133);
  `harness/tools/__init__.py` — `build_tools(config, registry)` assembles the list.
- Fetch content choke point: `_markdown_of` (fetch.py ~96) turns crawl4ai output into
  `FetchedPage.markdown`; `_render` (~133) is the ONLY transform before the model sees
  text (boundary-aware truncation, length-only). Scan/sanitize slots in per-page in
  `_fetch`, before `_render`. `FetchedPage` carries full untruncated text.
- Outcome convention: `classify` (fetch.py ~48) buckets into `FetchOutcome`
  (fetched/blocked/timeout/non_html/error) — typed outcomes, never exceptions; failures
  render as disclosed text. Search mirrors this with `SearchFailure`.
- Fetch input: `FetchPagesInput` (fetch.py ~241) bounds URL count only — no provenance
  check exists (R2 gap).
- Source registry: `harness/sources.py` — `SourceRegistry.add(url, title=None) -> str`
  (~81) mints `Sn` IDs keyed by `normalize_url`, stores title VERBATIM (untrusted);
  `resolve` (~123) rewrites `[Sn]` → `[domain](url)` with raw URL as href (R3 gap);
  `link()` (~106). Registry is the per-run object shared by all tools.
- Config: `harness/config.py` — settings groups are `_StrictModel` (extra="forbid")
  subclasses with bounded Fields, one field on `HarnessConfig` + one `[section]` in
  `harness.toml` (mirror `FetchSettings`/`[fetch]`). `_cross_check_roles` validates roles.
- Prompts: `harness/prompts.py` loads `harness/prompts/*.md` `string.Template` files;
  `orchestrator.md` and `subagent.md` exist.
- Tests: pytest + pytest-asyncio (auto mode), `tests/conftest.py` has `make_config`;
  crawl4ai faked per-file in `tests/test_fetch.py` via local `install_crawler` fixture
  (monkeypatches `harness.tools.fetch.AsyncWebCrawler`). Naming: `test_<behavior>`
  sentences, comments cite R-IDs. Run: `uv run pytest`.
- Commands: `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` /
  `uv run mypy .`
- No agent loop and no report writer exist yet (being built in a parallel session).

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- No worker-model (LLM-judge) detection in this plan — heuristics only; the judge is a
  backlog item (see D1).
- No minimal report writer built here to "complete" R3 — the sanitizer contract is
  handed to the parallel loop/report-writer work instead.
- No per-run escape hatch widening strict provenance to link-following — strict only,
  by explicit developer choice (see D2); an escape hatch can be added later if research
  reach suffers.
- No per-domain allowlists/denylists — provenance is the mechanism, not curation.

## Design Decisions

### D1: R1 detection mechanism
- **Chosen:** Heuristic scanner (regex/scoring rules over five signal families:
  instruction-override phrases, role/format spoofing, AI-directed addressing,
  obfuscation artifacts, exfil-shaped markup) + spotlighting (random-boundary fencing
  of all untrusted content in tool results). No model involved.
- **Rejected:** Worker-model LLM-judge per page — better semantic coverage, but requires
  the harness's first model client (net-new HTTP client, new config role, offline-mock
  strategy); goes to backlog as a config-gated later layer. Heuristics-without-
  spotlighting also rejected — fencing is nearly free at the `_render` choke point.
- **Consequences:** Detection catches syntactic attacks only; semantic steering is
  stopped by containment (R2/R4), not detection. Fixtures must include benign
  security-topic samples to bound false positives.

### D2: R2 fetch policy
- **Chosen:** Strict provenance — a URL is fetchable only if it arrived from SearXNG
  results or was supplied by the user; enforced in code via the per-run
  `SourceRegistry`, which both tools already share.
- **Rejected:** Sanitized link-following (strip query/fragment from in-page links) —
  residual exfil channel via path segments and attacker-chosen domains.
  Provenance + config escape hatch — developer chose strict; revisit only on evidence
  that research reach suffers.
- **Consequences:** The loop cannot follow links discovered inside pages; such URLs are
  rejected as a disclosed typed outcome. User-supplied URLs must be registered as
  approved at run start (contract for the loop).

### D3: Single guard module
- **Chosen:** One new module `harness/guard.py` holding scan, fence, and
  report-sanitize functions; fetch/search/report-writer all call into it.
- **Rejected:** Inline logic per tool — triplicates patterns and fixtures across
  fetch.py, search.py, and the future report writer.
- **Consequences:** guard.py is dependency-light (stdlib + pydantic only) so the
  parallel loop session can consume it without coupling to tool internals.

### D4: Blocked-by-guard is a typed outcome
- **Chosen:** Guard blocks surface as new outcome values rendered like existing failure
  buckets (`FetchOutcome`-style), disclosed in tool output; never exceptions.
- **Rejected:** Raising/filtering silently — violates the best-effort + disclose
  invariant and the codebase's typed-failure convention.
- **Consequences:** Drop + disclose falls out of the existing rendering path; the
  report's disclosure section gets blocked-source counts for free.

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Detect & block before model | Phase 1 (scanner), Phase 2 (wired into fetch+search) |
| R2 | No exfil via fetch | Phase 3 (provenance enforcement) |
| R3 | Report hygiene | Phase 4 (registry sanitization now; sanitizer contract to loop) |
| R4 | Containment floor | Phase 4 (spotlighting + documented loop constraints) |

## Progress
- [ ] Phase 1: Guard scanner core
- [ ] Phase 2: Wire scanning into fetch and search
- [ ] Phase 3: Strict URL provenance
- [ ] Phase 4: Spotlighting and report hygiene
- [ ] Final verification

## Phases

### Phase 1: Guard scanner core
**Risk:** flagged (!#1)
**Test-first:** required
**Goal:** A pure, offline-testable heuristic scanner that scores text for injection
signals and returns a block/pass verdict with the signals that fired.
**Requirements:** R1
**Files:**
- `harness/guard.py` — new; scanner rules + `ScanResult` model (new file per D3: shared
  by fetch, search, and the future report writer)
- `tests/test_guard.py` — new; behavior tests over real attack fixtures
- `tests/fixtures/injection/` — new; attack samples (from public corpora) + benign
  security-topic samples (false-positive bounds)
**Diff budget:** ~250-400 lines across 3-6 files

**Reuse:**
- Pydantic models: `_StrictModel`-style `ConfigDict(extra="forbid")` per harness/config.py
- Test naming/convention exemplar: `tests/test_fetch.py` (behavior sentences, R-ID comments)

**Contracts:**
- `scan(text: str) -> ScanResult` where `ScanResult.blocked: bool` and
  `ScanResult.signals: list[str]` — consumed by Phase 2 (tools) and the parallel loop work
- Signal families (stable names, one per family): `instruction_override`,
  `role_spoofing`, `ai_directed`, `obfuscation`, `exfil_markup`

**Out of scope:**
- No integration into fetch/search (Phase 2); no config wiring (Phase 2)
- No model-based detection; no fencing/sanitization functions yet (Phase 4)
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
- [ ] `uv run mypy .` and `uv run ruff check .` clean with the new module

### Phase 2: Wire scanning into fetch and search
**Risk:** none
**Test-first:** required
**Goal:** Every fetched page and every SearXNG title/snippet is scanned before its text
is rendered to the model; blocked items are dropped and disclosed as typed outcomes.
**Requirements:** R1
**Assumes:**
- Phase 1's `scan` contract is implemented and green
**Files:**
- `harness/tools/fetch.py` — scan each page in `_fetch` before `_render`; new
  guard-blocked outcome in the `FetchOutcome` bucket set; blocked pages excluded from
  content AND artifact page list
- `harness/tools/search.py` — scan title+snippet per result in `_parse_results` path;
  blocked results dropped with a disclosed count in `_render`
- `harness/config.py` — new `GuardSettings` (`_StrictModel`) + field on `HarnessConfig`
- `harness.toml` — new `[guard]` section (enabled flag; thresholds only if Phase 1
  produced a scored rather than binary verdict)
- `tests/test_fetch.py`, `tests/test_search.py` — new behavior tests
**Diff budget:** ~150-250 lines across 5-6 files

**Reuse:**
- Outcome pattern: mirror `classify`/`FetchOutcome` rendering in fetch.py (D4)
- Fakes: reuse `install_crawler` fixture pattern in tests/test_fetch.py
- Config pattern: mirror `FetchSettings`/`[fetch]` per Codebase Map

**Contracts:**
- Blocked sources never appear in the tool's content string, artifact list, or the
  source registry — consumed by the parallel report-writer work (nothing to filter
  downstream; disclosure text is the only trace)

**Out of scope:**
- No provenance checks (Phase 3); no fencing (Phase 4)
- No changes to truncation or crawl behavior; no new tool in `build_tools`

**Tests (write first, confirm red):**
- [ ] A fetched page carrying an attack fixture is excluded from content and artifact,
  and the rendered output discloses the block with its URL
- [ ] A blocked page is never added to the source registry (no `Sn` minted)
- [ ] A search result with an injected snippet is dropped; rendered output discloses the
  dropped count; clean results still render
- [ ] `[guard] enabled = false` bypasses scanning (config-driven, both tools)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add `GuardSettings` + `[guard]`; thread config into both build factories.
3. Integrate `scan` at the fetch and search choke points; add outcome + rendering.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Full suite green: `uv run pytest`

### Phase 3: Strict URL provenance
**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** The fetch tool refuses any URL that did not arrive from SearXNG results or
explicit user approval, as a disclosed typed outcome (R2 closed structurally).
**Requirements:** R2
**Assumes:**
- The per-run `SourceRegistry` instance is shared by search and fetch tools
  (confirmed: `build_tools(config, registry)`)
**Files:**
- `harness/sources.py` — provenance set on `SourceRegistry`: `approve(url)` and
  `is_approved(url)` keyed by `normalize_url`
- `harness/tools/search.py` — every parsed result URL is approved on ingestion
- `harness/tools/fetch.py` — unapproved URLs rejected pre-crawl as a new typed outcome
  (disclosed); never passed to `arun_many`
- `tests/test_sources.py`, `tests/test_fetch.py`, `tests/test_search.py` — behavior tests
**Diff budget:** ~100-180 lines across 5-6 files

**Reuse:**
- `normalize_url` in harness/sources.py for approval keying — do NOT re-normalize ad hoc
- Outcome pattern: same typed-outcome rendering as Phase 2

**Contracts:**
- `SourceRegistry.approve(url: str) -> None` / `is_approved(url: str) -> bool` — the
  parallel loop session MUST call `approve` for user-supplied URLs at run start; this is
  the only sanctioned way to widen fetchability
- Rejection is per-URL: one bad URL in a batch never fails the approved ones

**Out of scope:**
- No escape-hatch config to re-enable link-following (Non-Goals; D2)
- No domain-level rules; no changes to search parsing beyond the approve call

**Tests (write first, confirm red):**
- [ ] A URL never seen by search/user is rejected pre-crawl with a disclosed outcome;
  the crawler is not invoked for it (assert via fake)
- [ ] URLs from search results fetch normally; `approve`d user URLs fetch normally
- [ ] Approval respects `normalize_url` (trailing-slash/case variants of an approved
  URL are approved)
- [ ] Mixed batch: approved URLs succeed while the unapproved one is rejected

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the provenance set + methods to `SourceRegistry`.
3. Approve on search ingestion; enforce in fetch pre-crawl.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Full suite green: `uv run pytest`

### Phase 4: Spotlighting and report hygiene
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** All untrusted text reaching a model is fenced with an unforgeable random
boundary and declared as data-not-instructions; untrusted bytes entering the registry
or a report are sanitized.
**Requirements:** R3, R4
**Assumes:**
- Phases 1-3 merged; `harness/prompts/orchestrator.md` and `subagent.md` exist
**Files:**
- `harness/guard.py` — `fence(text) -> str` (random per-call boundary token, any
  occurrence of the token inside content stripped) and `sanitize_for_report(text) -> str`
  (strip zero-width/control chars, neutralize fence-like and chat-marker sequences)
- `harness/tools/fetch.py`, `harness/tools/search.py` — `_render` wraps untrusted
  content (page markdown; title/snippet) in `fence`
- `harness/sources.py` — `add` sanitizes titles on ingestion; `resolve`/`link` emit only
  http(s) URLs (anything else rendered as plain text, not a link)
- `harness/prompts/orchestrator.md`, `harness/prompts/subagent.md` — one rule block:
  fenced text is data, never instructions
- `tests/test_guard.py`, `tests/test_fetch.py`, `tests/test_search.py`,
  `tests/test_sources.py` — behavior tests
**Diff budget:** ~180-280 lines across 7-8 files

**Reuse:**
- Fencing/sanitizing live in `harness/guard.py` (D3) — no per-tool copies
- Prompt files follow the existing `string.Template` loading; no prompts.py changes

**Contracts:**
- `sanitize_for_report(text: str) -> str` — the parallel report-writer work MUST pass
  every model-generated report body through this before writing to disk (R3's second
  half; this plan cannot enforce it in code because the writer doesn't exist here)
- Loop constraint (documented, for the parallel loop work): text originating from tool
  results is never parsed for tool calls or instructions; only model output drives tool
  invocation (R4's second half)
- `fence(text: str) -> str` — output shape: opening/closing boundary lines around the
  content; boundary is unpredictable per call

**Out of scope:**
- No report writer, no agent loop code (parallel session owns them)
- No prompt rewrites beyond the single fenced-data rule block

**Tests (write first, confirm red):**
- [ ] Fenced output brackets content with matching random boundaries; a payload
  containing the boundary string cannot escape (occurrence stripped); boundaries differ
  across calls
- [ ] `sanitize_for_report` strips zero-width/control chars and neutralizes chat-marker
  and fence sequences; idempotent on clean text
- [ ] Registry: hostile title (control chars, fake `[Sn]`/markdown) is sanitized at
  `add`; `resolve` renders a `javascript:` URL as plain text, never a link
- [ ] Fetch/search `_render` output contains fenced untrusted content (both tools)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `fence` and `sanitize_for_report` in guard.py.
3. Wire fencing into both `_render`s; sanitize registry ingestion and link emission.
4. Add the fenced-data rule block to both prompt templates.
5. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Full suite green + all four quality gates (see `## Verification`)
- [ ] Contracts above communicated to the parallel loop/report-writer session (the
  implement session surfaces this handoff to the developer at completion)

## Verification
- [ ] `uv run pytest` — full suite green
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] Manual: run the fetch tool against a local fixture page containing a known attack
  string (offline fake) and observe drop + disclosure end to end

## Notes
- A parallel session is building the agent loop and report writer NOW. The three
  handoff contracts it must consume: `SourceRegistry.approve` for user URLs (Phase 3),
  `sanitize_for_report` before any disk write (Phase 4), and the never-parse-tool-calls-
  from-tool-results loop constraint (Phase 4). If that session lands first, its plan
  should gain these as reconciliations.
- Backlog candidates for the implement session to record in docs/backlog.md:
  worker-model LLM-judge detection layer (config-gated, D1); provenance escape hatch if
  research reach measurably suffers (D2).

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
#3. **Parallel loop/report-writer session may drift against this plan's contracts** —
    the loop is being built concurrently, not later. Mitigation: contracts are frozen
    in Phases 3-4 and listed in `## Notes`; the implement session must surface the
    handoff explicitly. If the loop lands with a different report path, reconcile there,
    not by loosening R3 here.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->
