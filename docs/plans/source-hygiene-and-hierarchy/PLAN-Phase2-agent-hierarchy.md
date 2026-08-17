# PLAN: Source Hygiene and Agent Hierarchy — Phase 2: Agent hierarchy

**Status:** Complete
**Created:** 2026-08-15
**Parent:** `PLAN-source-hygiene-and-hierarchy.md`
**Phase:** 2 of 2

## Context
Wires the researcher tier: the lead plans research angles and delegates each to a parallel
researcher subagent, which searches and delegates page reading to reader subagents; models
are rerouted per D7 after live availability checks. See the parent plan for Intent (R5–R7),
Codebase Map (deepagents nesting facts), and Design Decisions D5–D7.

## Progress
- [x] Step 1: Live model & rate-limit checks
- [x] Step 2: Role keys and routing
- [x] Step 3: Researcher tier wiring
- [x] Step 4: Disclosures end-to-end
- [x] Step 5: Consolidated verification verdict
- [ ] Phase verification

## Steps

### Step 1: Live model & rate-limit checks
**Risk:** flagged (!#3, !#5)
**Test-first:** N/A — environment verification, not code
**Requirements:** R6
**Files:**
- `docs/decisions.md` — record each check's outcome (model available / 403 / absent, and the
  account's RPM/TPM limits) with date.
**Diff budget:** ~10–25 lines across 1 file

**Reuse:**
- Use the existing `preflight(config, role)` in `harness/models.py` via a throwaway
  `harness.toml` role entry or a short `uv run python -c` snippet — do NOT write a new
  check script into the repo.

**Out of scope:**
- Committing any model ID to `harness.toml` (Step 2, and only for models that passed);
  benchmarking or latency measurement.

**Manual verification:**
- [x] `preflight` against `kimi-k3` succeeds, or its failure mode is recorded.
- [x] `preflight` against `deepseek-v4-flash` succeeds (region opt-in confirmed), or its
  failure mode is recorded.
- [x] OpenCode dashboard rate limits (RPM/TPM) read and recorded in docs/decisions.md.

**Details:**
This step is the phase gate (!#3): if kimi-k3 or v4-flash is unavailable, STOP and decide
substitute assignments with the developer before Step 2 — the parent plan's R6 model list is
the intent, not an assumption to improvise around.

**Acceptance criteria:**
- [x] docs/decisions.md entry exists naming all three outcomes with the check date.

### Step 2: Role keys and routing
**Risk:** none
**Test-first:** required
**Requirements:** R6
**Assumes:**
- Step 1 passed for every model being committed (or substitutes were agreed).
- Phase 1 Step 4 landed (`[roles.verifier]` exists and is preflighted).
**Files:**
- `harness.toml` — roles become `head` (kimi-k3), `researcher` (deepseek-v4-pro), `reader`
  (deepseek-v4-flash), `verifier` (gpt-5.6-luna); `subagent` key retired (D7).
- `harness/agent.py` — `build_chat_model(config, "researcher")` / `"reader"` replace the
  single `"subagent"` build.
- `tests/conftest.py` — `make_config` roles updated; `patch_models_by_role` callers follow.
- `docs/guides/setup.md`, `docs/INDEX.md` — role table updated.
**Diff budget:** ~60–120 lines across 6 files

**Reuse:**
- `roles` is already a generic `dict[str, RoleConfig]` in `harness/config.py` — no schema
  change; do NOT add role-name constants or an enum.
- Pattern to mirror: Phase 1 Step 4's verifier wiring (config key + build call + preflight).

**Contracts:**
- Role keys `head` / `researcher` / `reader` / `verifier` — frozen for Step 3 and for
  everything downstream (prompts, docs, tests).

**Out of scope:**
- Any hierarchy wiring (Step 3); backward-compat aliasing of `subagent` (D7 rejected it);
  touching model IDs beyond what Step 1 cleared.

**Tests (write first, confirm red):**
- [x] The reader subagent's model resolves from role `reader` and the researcher's from
  `researcher` (per-role assertion via `patch_models_by_role`).
- [x] A config still declaring only `head`/`subagent` fails with a `ModelError` naming the
  missing role (loud rename, no silent fallback).

**Details:**
Red→green. Mechanical rename plus one new role; keep the diff boring.

**Acceptance criteria:**
- [x] `grep -r "subagent" harness/ --include="*.py"` finds no role-key usages (prompt
  filename `subagent.md` is renamed or re-pointed in Step 3, whichever lands there).

### Step 3: Researcher tier wiring
**Risk:** flagged (!#4)
**Test-first:** required
**Requirements:** R5, R7
**Assumes:**
- Step 2's role keys resolve; `harness/prompts/subagent.md` (frozen researcher contract) is
  the researcher's system prompt source.
**Files:**
- `harness/agent.py` — new `_researcher_spec()`: a `SubAgent` whose `middleware` carries
  `SubAgentMiddleware(subagents=[reader_spec])`, the relocated `_ReaderDigestMiddleware`,
  the task-scoped retry/error middleware, ~~and an explicit recursion bound (D6)~~
  (struck 2026-08-15 — see Reconciliations: run-level bounds + prompt guidance); the lead's
  `subagents` list declares the researcher; `search_web` moves to researcher tools; the
  lead keeps planning/workspace/`ask_user` tools only.
- `harness/prompts/orchestrator.md` — lead now plans angles and delegates via
  `task(subagent_type="researcher")`; researcher-count guidance bounded per Step 1's
  recorded rate limits (!#5).
- `harness/prompts/subagent.md` — variables filled if the frozen contract carries any;
  ~~content semantics unchanged (parent non-goal)~~ (amended 2026-08-15 — see
  Reconciliations: Tools section updated to search_web + task(reader); research-contract
  semantics still unchanged).
- `tests/test_agent.py` — nested-delegation tests with scripted models.
**Diff budget:** ~200–350 lines across 4 files

**Reuse:**
- Extend `_reader_spec`/`_middleware` structure in `harness/agent.py` — do NOT hand-build a
  langgraph graph (`CompiledSubAgent` is D5's rejected fallback, used only if nesting fails
  in practice — that failure is a STOP-and-reconcile, not an improvisation).
- Pattern to mirror: the existing reader `SubAgent` declaration and its `interrupt_on`
  omission (D6).

**Contracts:**
- Subagent type names the lead can dispatch: `"researcher"` (and `"reader"` only from
  inside a researcher). The `RESEARCHER FAILED` error-message prefix comes free from the
  existing derived `_reader_failure_message` — tests pin it.
- `_ReaderDigestMiddleware` observes the researcher's `task` calls (its digest→`mark_read`
  semantics unchanged).

**Out of scope:**
- Changing the researcher contract's semantics; TUI rendering of nested activity; wall-clock
  or round-cap redesign (amended 2026-08-15: re-pointing the wall-clock ARM TRIGGER and the
  abort passthrough in `__main__` is maintenance, not redesign — see Reconciliations Drift C);
  the general-purpose subagent (stays disabled).

**Tests (write first, confirm red):**
- [x] Lead → researcher → reader: a scripted run where the researcher's reader digest
  reaches the lead, and the digested source is marked `digested` (R7's mechanism moved, not
  broken).
- [x] A researcher crash surfaces as a `RESEARCHER FAILED (...)` error ToolMessage to the
  lead; the run continues (existing error middleware, new tier).
- ~~The researcher's graph carries its own recursion bound, not the ambient 9,999 (D6).~~
  (struck 2026-08-15 — no such bind exists in deepagents 0.7.5; see Reconciliations)
- [x] The lead's tool surface no longer includes `search_web`/direct fetch routes.

**Details:**
Red→green. Build reader model/spec first, embed in the researcher's middleware, keep
`build_agent`'s signature unchanged. ~~The recursion bound rides the researcher's
`SubAgent.middleware` or its model config — whichever deepagents 0.7.5 actually honors;
confirm against site-packages before implementing (the explorer confirmed inheritance, not
the override mechanism — if no per-subagent bind exists, bound it via the task-tool config
and record the reconciliation).~~ (struck 2026-08-15 — site-packages confirmed NO route
exists, including the task-tool config; see Reconciliations.)

**Acceptance criteria:**
- [x] Scripted end-to-end test: two researchers dispatched in one lead turn actually run
  concurrently (mirrors the `asyncio.gather` fact — peak in-flight > 1 with an
  event-ordering assertion, same style as `_ConcurrencyTrackingModel`).

### Step 4: Disclosures end-to-end
**Risk:** none
**Test-first:** required
**Requirements:** R7
**Files:**
- `harness/report.py` — only if Step 3's relocation changed what the disclosure rollup sees
  (expected: no change); `tests/test_report.py` / `tests/test_agent.py` — end-to-end
  disclosure assertions.
- `docs/INDEX.md`, `CLAUDE.md` — status/architecture lines updated to the 3-tier reality.
**Diff budget:** ~40–90 lines across 4 files

**Reuse:**
- The existing `## Source reading` rollup in `harness/report.py` — do NOT add a new
  disclosure section for the researcher tier.

**Out of scope:**
- New report sections; per-researcher attribution in the report (angles are internal
  structure, not reader-facing).

**Tests (write first, confirm red):**
- [x] A scripted 3-tier run's report discloses digested / fallback / unread counts that
  match the registry state (the wiring test `test_report.py` alone cannot prove).

**Details:**
Red→green. This step is small by design: it exists to prove R7 at the report boundary after
Step 3 moved the machinery, and to reconcile the docs.

**Acceptance criteria:**
- [ ] Live run per the central plan's Phase 2 checklist (researcher fan-out visible,
  disclosure section plausible).

### Step 5: Consolidated verification verdict
**Risk:** none
**Test-first:** required
**Requirements:** developer request 2026-08-15 (report readability; accepted tradeoff)
**Files:**
- `harness/verify.py` — after the per-paragraph loop, ONE extra call on the `verifier` role:
  feed it all per-paragraph verdicts, get back a short reviewer-analysis paragraph (rollup
  counts plus a sentence naming each claim that was not fully supported).
- `harness/report.py` — drop the per-paragraph `Sources:`/`Verdict:` blocks from
  `## Answer`; render the reviewer paragraph under `## Sources`.
- `tests/test_verify.py`, `tests/test_report.py` — consolidation-call and rendering tests.
**Diff budget:** ~80–150 lines across 4 files

**Reuse:**
- Per-paragraph verification is UNCHANGED (verdicts are still computed — they are the
  consolidator's input and the debugging granularity). Do NOT wrap verification in
  agents/subagents: verifiers are judge-input-in/verdict-out plain model calls; a manager
  agent adds per-agent prompt+tool overhead with zero capability gained (decided with the
  developer 2026-08-15 over a proposed verifier-manager subagent tree).

**Contracts:**
- `verify_paragraphs` keeps returning per-paragraph verdicts; the consolidated paragraph is
  additive output, not a replacement.

**Out of scope:**
- Multi-judge panels / judge calibration (parent non-goal); changing verification pooling;
  per-source verdicts.

**Tests (write first, confirm red):**
- [x] The consolidation call runs on the `verifier` role and receives every per-paragraph
  verdict.
- [x] The rendered report has no `Sources:`/`Verdict:` lines inside `## Answer`, and the
  reviewer paragraph appears under `## Sources` naming each non-supported claim.

**Details:**
Accepted tradeoff (developer, 2026-08-15): claim-level verdict pointers survive only as
prose in the reviewer paragraph, not beside each claim — the consolidator naming each
problem claim is what keeps that tolerable.

**Acceptance criteria:**
- [ ] A run with at least one `partially supported` paragraph names that claim in the
  `## Sources` reviewer paragraph.

## Verification
- [ ] Quality gates: see the central plan's `## Verification`.
- [ ] Live end-to-end run per the central plan's Phase 2 checklist.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Struck text
above stays visible; entries here are the authoritative correction. -->

2026-08-15 — Step 3 (Drift A): deepagents 0.7.5 has NO per-subagent recursion_limit route —
`SubAgent` TypedDict has no such field, `create_sub_agent` applies no `.with_config`, and the
task-tool invocation config sets no recursion key (the lead's ambient 9,999 passes through;
`TaskToolSchema` accepts only description/subagent_type). Developer decision: run-level
bounds only (wall clock, lead round cap, ambient 9,999 crash-stop) PLUS prompt-side budget
guidance in the researcher contract (bounded searches/reader dispatches per angle). The D6
recursion-bound test is struck; CompiledSubAgent stays rejected per D5.

2026-08-15 — Step 3 (Drift C): moving search_web/fetch_pages inside the nested researcher
makes `__main__`'s stream loop blind to them — the R2 wall clock never arms (it keyed on
those tool names in TOP-LEVEL updates) and a mid-run `SearchUnavailableError` is stringified
into a soft `RESEARCHER FAILED` ToolMessage by the lead's ToolErrorMiddleware, violating the
documented three-failures abort invariant. Developer decision: fix in-scope now —
`__main__.py` arms the wall clock on `task(subagent_type="researcher")` dispatch (that IS
"research started" in the 3-tier design), and the task error middleware re-raises
`SearchUnavailableError` instead of converting it; the 5 affected tests migrate to the new
topology preserving their subjects. `__main__.py` joins Step 3's file list by amendment.

2026-08-15 — Step 3 (Drift B): subagent.md's Tools section named search_web + fetch_pages
direct, contradicting R5's delegated reading. Developer decision: "frozen" scopes the
research-contract semantics (mission, output shape), not tool mechanics — the Tools section
is updated to search_web + task(subagent_type="reader"). reader.md already assumed a
researcher caller. Escalated to the parent plan's Reconciliations (R5 wording).

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->

### 2026-08-15 — Step 1: Live model & rate-limit checks
- Done: Live preflight passed for both kimi-k3 and deepseek-v4-flash (existing region opt-in
  suffices); outcomes plus the rate-limit disposition recorded in docs/decisions.md. The
  three series plan files also moved into docs/plans/source-hygiene-and-hierarchy/
  (developer-requested reorg, same commit).
- Learned: OpenCode dashboard exposes no RPM/TPM figures; developer decided to ignore limits
  until one is hit, so Step 3's researcher-count guidance uses a conservative default fan-out
  rather than a measured bound.
- Drift: none.
- Watch-next: Step 2 renames role keys (head/researcher/reader/verifier, subagent retired) —
  test-first required; both models it commits to harness.toml are now verified live.

### 2026-08-15 — Step 2: Role keys and routing
- Done: Role keys are head/researcher/reader/verifier (subagent retired, no alias); harness.toml
  carries kimi-k3 head, deepseek-v4-pro researcher, deepseek-v4-flash reader. Red→green
  (417 tests), all gates clean, quality scan clean.
- Learned: config.py's load-required tuple shrank to ("head",) so missing new roles surface as
  ModelError at build_agent (the plan's contract, verifier precedent); agent.py carries a bare
  `build_chat_model(config, "researcher")` whose return value Step 3 consumes — it exists now
  only to fail loud. report.py's "Subagent Model" line was in scope via the acceptance grep
  (now Researcher/Reader Model lines) despite not being in the step's file list.
- Drift: none.
- Watch-next: __main__ still preflights only head+verifier — an undeclared researcher/reader
  fails at build_agent instead; when Step 3 wires the researcher spec, consume the bare build's
  return value there.

### 2026-08-15 — Step 3: Researcher tier wiring
- Done: 3-tier hierarchy live — lead declares only `researcher`; researcher nests the reader
  via SubAgentMiddleware and owns search_web/fetch_raw plus the relocated digest/retry/error
  middleware; prompts rewritten (angle delegation, budget guidance); wall clock arms on
  task(researcher) dispatch and SearchUnavailableError re-raises to __main__'s abort path.
  422 tests green, gates clean, flagged 3F review passed (2 Minors logged to parent
  Discoveries: researcher interrupt_on pin missing; diff-budget overrun accounted by drift).
- Learned: THREE reconciliations this step (see ## Reconciliations): no per-subagent
  recursion bound exists in deepagents 0.7.5 (run-level bounds + prompt guidance instead);
  subagent.md's Tools section rewritten (frozen = research semantics, not tool mechanics);
  __main__'s arm trigger/abort passthrough re-pointed. Also: hand-nested SubAgents get NO
  auto-injected middleware — reader's FilesystemMiddleware is now explicit. Worker
  co-developed tests with code (disclosed); reviewer found no tautologies.
- Drift: Reconciliations Drift A, B, C (this file) + parent-plan entries.
- Watch-next: Step 4 proves R7 disclosures at the report boundary end-to-end and reconciles
  docs/INDEX.md + CLAUDE.md to the 3-tier reality; add the researcher interrupt_on test pin
  while in there. Then Step 5 (consolidated verdict) before the live run.

### 2026-08-16 — Step 4: Disclosures end-to-end
- Done: End-to-end mixed digested/unread disclosure proven against the WRITTEN report text
  (test_delegation_e2e.py); report.py needed no change (the plan's predicted outcome — both
  new tests were immediate-green regression pins, disclosed). Researcher interrupt_on pin
  added; docs/INDEX.md + CLAUDE.md reconciled to the 3-tier, four-role reality. 424 green,
  gates clean after one orchestrator lint wrap.
- Learned: Step 4's live-run acceptance box and the phase's ## Verification live run are the
  same event — sequenced after Step 5 so it exercises the finished phase.
- Drift: none.
- Watch-next: Step 5 (consolidated verification verdict) — verify.py gains ONE pooled
  consolidation call on the verifier role; report.py drops per-paragraph Sources:/Verdict:
  from ## Answer and renders the reviewer paragraph under ## Sources. Then the live run.

### 2026-08-16 — Step 5: Consolidated verification verdict
- Done: ONE pooled consolidation call on the verifier role (reusing the client already built
  for per-paragraph work) produces a prose reviewer paragraph carried as
  `VerificationResult.reviewer_summary` and rendered under `## Sources`; per-paragraph
  `Sources:`/`Verdict:` blocks are gone from `## Answer`. Per-paragraph verdicts still
  computed and returned unchanged (frozen contract). 423 green, all gates clean.
- Learned: the consolidator's "Paragraph N" numbering must match what a reader can COUNT in
  the rendered `## Answer`, not raw list position — citation-only paragraphs render nothing,
  so raw numbering pointed at the wrong claim (caught by 3F, fixed via the new shared
  `renders_content` predicate in @harness/paragraphs.py). With per-claim verdicts gone from
  `## Answer`, that prose anchor is the reader's ONLY pointer — it is load-bearing. Also:
  consolidation is best-effort, its failure disclosed as `consolidated summary: ...` in
  check_failures so it never reads as an unchecked paragraph.
- Drift: none (D-A..D-E were orchestrator design decisions inside plan scope).
- Watch-next: ALL implementation steps are done — only the phase's live end-to-end run
  remains (needs SearXNG up and real model calls): researcher fan-out visible, disclosure
  section plausible, and a run with a `partially supported` paragraph naming that claim in
  the `## Sources` reviewer paragraph. Four items sit in the parent plan's Discoveries.
