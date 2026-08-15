# PLAN: Source Hygiene and Agent Hierarchy

**Status:** Not started
**Created:** 2026-08-15
**Type:** Central plan (2 sub-plans)

## Intent

**True goal:** Reports whose sources are clean and independently verified — no duplicate
registrations of the same work, no silent PDF garbage, a stable report structure, and a
verifier that did not write the report — plus a 3-tier agent hierarchy (lead → researchers →
readers) that scales research breadth with parallel researchers. For the developer operating
the harness and anyone reading its reports.

**Binding outcomes:**
- **R1** — One registered source per canonical URL: normalization plus arxiv-variant rules
  (abs/html/pdf/vN of the same work share one `Sn` ID); reported source counts reflect
  deduped reality.
  - Existing `Sn` IDs stay stable: dedup collapses at registration time, never renumbers.
- **R2** — A fetched PDF yields extracted text evidence, or a disclosed unusable capture —
  never silent garbage (or a silent `non_html` exclusion) entering verification.
  - Extraction failure (encrypted, image-only scan) falls back to the disclosed-unusable
    path, same as a failed fetch.
- **R3** — Reports follow a fixed heading structure enforced both by prompt template and at
  render time; no model-authored meta/disclosure sections — harness-side disclosures are the
  only meta layer.
- **R4** — Verification runs on a config-driven `verifier` role defaulting to a model that
  did not write the report (`gpt-5.6-luna`).
- **R5** — Hierarchy: lead → researchers (parallel, one per research angle) → readers, per
  the frozen researcher contract in `harness/prompts/subagent.md`; depth capped at 3.
- **R6** — Full role/model routing via config: lead=`kimi-k3`, researcher=`deepseek-v4-pro`,
  reader=`deepseek-v4-flash`, verifier=`gpt-5.6-luna` — each gated on a live preflight check
  before being committed to config.
- **R7** — Digest/read-status tracking and report disclosures (digested / raw fallback /
  unread) survive the new hierarchy intact.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- Verifier verdicts quote the evidence span behind each verdict, so a human can spot-check
  a verdict without rereading the source.
- Sources that were read but never cited in the answer are disclosed as unused.

**Non-goals:**
- Depth >3 or recursive self-spawning agents.
- Content-similarity / title-matching same-work detection beyond the arxiv rule table.
- Judge calibration set and multi-judge panel (backlog — revisit when single-judge verdicts
  are observably noisy).
- Researcher semantics beyond the frozen contract in `harness/prompts/subagent.md`.

**Constraints & assumptions:**
- Pinned deps: deepagents ==0.7.5, crawl4ai ==0.9.2; any new dependency (pypdf via
  `crawl4ai[pdf]`) is pinned exactly per project convention.
- Project invariants hold: no shell tool, additive tool registry, config-driven model
  routing, best-effort + disclose.
- OpenCode is the only provider; parallel researcher fan-out is bounded by its rate limits
  and our own concurrency knobs, not by any "subagent limit".

**Open questions:**
- OpenCode rate limits (requests/min, tokens/min per key) — read off the dashboard or live
  probe; bounds researcher fan-out width.
- `kimi-k3` availability on the OpenCode workspace — live `preflight` check.
- `deepseek-v4-flash` region opt-in now valid — live `preflight` check (403'd previously,
  see docs/decisions.md).

## Codebase Map

- Entry points: `harness/__main__.py` — CLI run loop, streams the agent, runs verification,
  writes the report; `harness/agent.py` — `build_agent`, the only deepagents importer,
  declares the `reader` SubAgent and all middleware.
- Source registry: `harness/sources.py` — `SourceRegistry.add(url, title=None)` dedups on
  `normalize_url()` (scheme/host case, trailing slash, default port, fragment; query
  preserved), first-write-wins, sequential stable `Sn` IDs; `mark_read(id, mode)` tracks
  `unread`/`digested`/`fallback`.
- Fetch: `harness/tools/fetch.py` — `classify()` is the single outcome decision point
  (`FetchOutcome` literal); success capture `# {sid}: {heading}… - Outcome: fetched` vs
  `FETCH FAILED` stub; config keys `page_timeout_ms`, `max_concurrency`,
  `per_page_char_cap`, `max_urls_per_call`; crawler built in `_crawler_class()` with default
  Playwright strategy (no PDF handling — PDF URLs classify as `non_html` today).
- Verification: `harness/verify.py` — `verify_paragraphs(paragraphs, config, registry,
  on_paragraph=None)`, builds role `"head"` today; per-paragraph pooled calls; reads raw
  captures under `sources_dir`.
- Report: `harness/report.py` — `RunOutcome` + `write_report`, renders `## Answer` from
  paragraphs, per-paragraph `Sources:`/`Verdict:`, disclosure sections, source-reading
  rollup.
- Config: `harness/config.py` — `HarnessConfig` with generic `roles: dict[str, RoleConfig]`
  (role keys are data, not schema); `harness/models.py` — `build_chat_model(config, role)` +
  `preflight(config, role)`.
- Prompts: `harness/prompts/` — `$variable` templates via `harness/prompts.py:render`;
  `orchestrator.md` (lead), `reader.md`, `subagent.md` (frozen researcher contract, unwired).
- deepagents 0.7.5 facts (explorer-confirmed, site-packages): `SubAgent` TypedDict has no
  `subagents` field but accepts `middleware`, `model`, `tools`, `interrupt_on`
  (`deepagents/middleware/subagents.py:36-127`); a child gets its own `task` tool by putting
  `SubAgentMiddleware(subagents=[...])` in its `middleware` list (documented usage, `:608+`);
  parallel `task` calls run concurrently via `asyncio.gather`
  (`langgraph/prebuilt/tool_node.py:828-858`); `create_deep_agent` binds
  `recursion_limit: 9_999` which subagents inherit unless they bind their own
  (`graph.py:936-937`); `interrupt_on` inherits top-level unless overridden.
- crawl4ai 0.9.2 facts (explorer-confirmed): `PDFCrawlerStrategy` /
  `PDFContentScrapingStrategy` exist (`crawl4ai/processors/pdf/__init__.py`) but require
  `pypdf` (extra `crawl4ai[pdf]`), NOT currently installed; no automatic PDF detection
  anywhere — caller must route.
- Tests: `tests/` — pytest (asyncio_mode=auto), `tests/conftest.py` holds
  `ScriptedChatModel`, `patch_model`/`patch_run`, `make_config`, capture writers,
  `install_search_transport`. Suite is offline/fixture-based, 388 tests green.
- Commands: `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` /
  `uv run mypy .` (CI adds a 90% coverage floor on `harness/`).

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- No re-canonicalization of historical reports or old run workspaces — new runs only.
- No change to verification pooling semantics (one pooled call per paragraph stays).
- No TUI/display changes in either sub-plan.
- No renaming of report capture file shapes — PDF captures reuse the existing
  fetched/FETCH FAILED shapes.

## Design Decisions

### D1: Where canonical-URL dedup lives
- **Chosen:** Extend `normalize_url()` in `harness/sources.py` — generic cleanup (tracking
  params) plus a small host-rule table for arxiv (abs/html/pdf/vN → one canonical form).
  One function stays the single notion of "same source".
- **Rejected:** A separate canonicalization layer or registry-side same-work detection —
  two competing notions of canonical, more code, false-merge risk on non-arxiv hosts.
- **Consequences:** Dedup is registration-time only; the arxiv table is the extension point
  for future hosts (added only when a real dup bites). First-write-wins and `Sn` stability
  are preserved by construction.

### D2: PDF handling
- **Chosen:** Add pinned `pypdf` (the `crawl4ai[pdf]` extra's engine), detect PDF URLs in
  `harness/tools/fetch.py` (extension and/or response content-type `application/pdf`) and
  route them through crawl4ai's `PDFCrawlerStrategy`/`PDFContentScrapingStrategy`; extracted
  text lands in the normal fetched capture shape; extraction failure or empty text writes
  the existing `FETCH FAILED` stub (disclosed-unusable).
- **Rejected:** Detect-and-mark-unusable only — loses PDF evidence (arxiv PDFs are common in
  this domain). HTML-sibling rewrite — host-specific, already partially covered by D1's
  arxiv canonicalization.
- **Consequences:** New pinned dependency; a second crawler instance/strategy path inside
  `_fetch`; `classify()` gains a PDF-aware branch so a PDF never exits as silent `non_html`.

### D3: Report structure enforcement
- **Chosen:** Both ends. Prompt-side: the orchestrator prompt gains an explicit output
  template (heading levels, section order, no meta/coverage/disclosure sections — the
  harness owns disclosure). Render-side: `harness/report.py` demotes any model-authored H1/H2
  to fit under `## Answer` mechanically.
- **Rejected:** Prompt-only — depends on model obedience, the observed failure. Render-only —
  fixes hierarchy but not model-authored meta sections.
- **Consequences:** Report structure is stable across model swaps; the verifier never sees a
  model-authored confession paragraph (dissolves the disclosure-impeachment problem).

### D4: Verifier role
- **Chosen:** New config role key `verifier` (model `gpt-5.6-luna`); `harness/verify.py`
  builds `"verifier"` instead of `"head"`; `__main__` preflights it at startup alongside
  `head` (fail-fast invariant). Undeclared role = startup `ModelError`, no fallback.
- **Rejected:** Fallback-to-head when undeclared — silently reintroduces self-grading, the
  exact defect this fixes. `deepseek-v4-flash` as verifier — shares training family with the
  researcher tier's writer models; luna maximizes cross-family independence from the report
  writer. Residual accepted risk: luna also digests pages (reader today, researcher context
  later), but verification judges raw captures, not digests.
- **Consequences:** Every run costs a second preflight call; role keys in `harness.toml` are
  load-bearing for verification.

### D5: How the researcher tier nests
- **Chosen:** Researcher is a declared `SubAgent` whose `middleware` list carries its own
  `SubAgentMiddleware(subagents=[reader_spec])`, giving it a `task` tool one level down —
  plus the relocated digest middleware and the task-scoped retry/error middleware.
- **Rejected:** `CompiledSubAgent` wrapping a full `create_deep_agent` — must hand-compile a
  compatible state schema, loses the declared-spec symmetry with `reader`, bigger diff.
- **Consequences:** `harness/agent.py` stays the single deepagents importer; the lead's tool
  surface shrinks (search/fetch delegation moves down); `_ReaderDigestMiddleware` moves from
  the lead's middleware to the researcher's.

### D6: Bounds and interrupts in the nested tree
- **Chosen:** The researcher binds its own explicit recursion limit (config-derived, same
  `max_rounds * 2 + 1` shape as the lead) rather than inheriting deepagents' ambient 9,999;
  researcher and reader both leave `interrupt_on` unset/overridden so `ask_user` can never
  fire from a tier with no checkpointer.
- **Rejected:** Inheriting ambient limits — a runaway researcher could consume the whole
  wall clock invisibly.
- **Consequences:** A researcher that hits its bound fails that one `task` call (caught by
  the existing task error middleware) — the run degrades and discloses, never hangs.

### D7: Role key naming
- **Chosen:** Role keys become `head` (lead), `researcher`, `reader`, `verifier`; the
  `subagent` key is retired. `roles` is already a generic dict in config, so this is data +
  call-site changes, not schema.
- **Rejected:** Keeping `subagent` as an alias — two names for the reader role invites
  config drift.
- **Consequences:** `harness.toml`, `.env.example` docs, `build_chat_model` call sites, and
  test fixtures all move in Phase 2 (Sub-plan 2 Step 2); Phase 1 touches only `verifier`.

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Canonical-URL dedup | Phase 1 → PLAN-Phase1-source-hygiene.md (Step 1) |
| R2 | PDF text or disclosed-unusable | Phase 1 → PLAN-Phase1-source-hygiene.md (Step 2) |
| R3 | Enforced report structure, no model meta | Phase 1 → PLAN-Phase1-source-hygiene.md (Step 3) |
| R4 | Config-driven independent verifier | Phase 1 → PLAN-Phase1-source-hygiene.md (Step 4) |
| R5 | 3-tier hierarchy per frozen contract | Phase 2 → PLAN-Phase2-agent-hierarchy.md (Steps 3–4) |
| R6 | Role/model routing, preflight-gated | Phase 2 → PLAN-Phase2-agent-hierarchy.md (Steps 1–2) |
| R7 | Read-status tracking survives hierarchy | Phase 2 → PLAN-Phase2-agent-hierarchy.md (Steps 3–4) |

## Progress
- [ ] Phase 1: Source hygiene & independent verification
- [ ] Phase 2: Agent hierarchy
- [ ] Final verification

## Phases

### Phase 1: Source hygiene & independent verification
**Risk:** flagged (!#1, !#2)
**Sub-plan:** `PLAN-Phase1-source-hygiene.md`
**Goal:** Reports register each work once, PDFs become evidence or disclosed failures, the
answer follows an enforced structure, and verification runs on a non-writer model.
**Key deliverables:**
- `normalize_url` canonicalization (tracking params + arxiv rule table)
- PDF fetch path via crawl4ai's PDF strategy, pinned `pypdf`
- Orchestrator output template + render-time heading demotion
- `[roles.verifier]` wired through `verify.py` and startup preflight

### Phase 2: Agent hierarchy
**Risk:** flagged (!#3, !#4, !#5)
**Sub-plan:** `PLAN-Phase2-agent-hierarchy.md`
**Goal:** Lead delegates research angles to parallel researcher subagents, each of which
delegates page reading to readers; models routed per D7; disclosures intact.
**Key deliverables:**
- Live preflight checks: kimi-k3, deepseek-v4-flash, rate limits recorded
- Role keys `head`/`researcher`/`reader`/`verifier` in config and call sites
- Researcher `SubAgent` with nested reader `task` tool, digest middleware relocated,
  explicit recursion bound
- Orchestrator prompt rewritten to plan angles and delegate to researchers
- End-to-end scripted-model test proving disclosures survive the hierarchy

## Phase Dependencies
Phase 2 requires Phase 1 (both edit `harness.toml` roles and `models`/preflight call sites;
Phase 1's verifier role is the template Phase 2's role renames follow). Within Phase 2,
Step 1 (live checks) gates Step 2 (committing model IDs to config).

## Verification
- [ ] `uv run pytest` — full suite green
- [ ] `uv run ruff check .` and `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] One live end-to-end run (`python -m harness "<question>"`) after each phase: Phase 1 —
  report shows deduped sources, a PDF source with real text or a disclosed failure, clean
  heading hierarchy, verifier model in run metadata; Phase 2 — researcher fan-out visible in
  the TUI, read-status disclosure section present and plausible.

## Risks
#1. **Arxiv canonicalization can merge pages whose extracted text differs** — abs, html and
    pdf views of one work render different text; after merging, the capture verification
    reads may not be the exact page the reader digested. Accepted: they are the same work,
    and the claim-vs-work check is what verification is for. Confirm in Phase 1's live run
    that a merged source still verifies sensibly; if not, narrow the table to version
    suffixes only.
#2. **pypdf extraction quality varies** — image-only or encrypted PDFs yield empty/garbage
    text. Mitigation is R2's edge-case default: empty extraction → `FETCH FAILED` stub,
    disclosed. The test suite must include an extraction-failure fixture, not just the happy
    path.
#3. **Model availability is unverified** — kimi-k3 may not exist on this OpenCode workspace;
    deepseek-v4-flash 403'd before the region opt-in. Phase 2 Step 1 is a hard gate: no
    model ID is committed to `harness.toml` until its live preflight passes; fallback
    assignments are decided at that gate with the developer, not improvised.
#4. **Nested SubAgentMiddleware is documented but unexercised here** — the researcher's own
    `task` tool, digest-middleware relocation, and interrupt inheritance interact in ways
    the current suite never touches. Mitigation: D6 pins interrupts off below the lead;
    Step 3 carries a dedicated nested-delegation test with scripted models before any live
    run; if nesting fails in practice, the fallback is D5's rejected `CompiledSubAgent`
    route — a known, bigger diff, not a dead end.
#5. **Researcher fan-out can hit OpenCode rate limits** — parallel `task` calls each drive
    model traffic. Step 1 records the account's RPM/TPM; the orchestrator prompt bounds
    concurrent researchers (prompt-side cap, config knob only if the live run proves it
    insufficient).

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->
