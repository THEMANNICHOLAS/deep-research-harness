# PLAN: Source Hygiene and Agent Hierarchy — Phase 2: Agent hierarchy

**Status:** Not started
**Created:** 2026-08-15
**Parent:** `PLAN-source-hygiene-and-hierarchy.md`
**Phase:** 2 of 2

## Context
Wires the researcher tier: the lead plans research angles and delegates each to a parallel
researcher subagent, which searches and delegates page reading to reader subagents; models
are rerouted per D7 after live availability checks. See the parent plan for Intent (R5–R7),
Codebase Map (deepagents nesting facts), and Design Decisions D5–D7.

## Progress
- [ ] Step 1: Live model & rate-limit checks
- [ ] Step 2: Role keys and routing
- [ ] Step 3: Researcher tier wiring
- [ ] Step 4: Disclosures end-to-end
- [ ] Step 5: Consolidated verification verdict
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
- [ ] `preflight` against `kimi-k3` succeeds, or its failure mode is recorded.
- [ ] `preflight` against `deepseek-v4-flash` succeeds (region opt-in confirmed), or its
  failure mode is recorded.
- [ ] OpenCode dashboard rate limits (RPM/TPM) read and recorded in docs/decisions.md.

**Details:**
This step is the phase gate (!#3): if kimi-k3 or v4-flash is unavailable, STOP and decide
substitute assignments with the developer before Step 2 — the parent plan's R6 model list is
the intent, not an assumption to improvise around.

**Acceptance criteria:**
- [ ] docs/decisions.md entry exists naming all three outcomes with the check date.

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
- [ ] The reader subagent's model resolves from role `reader` and the researcher's from
  `researcher` (per-role assertion via `patch_models_by_role`).
- [ ] A config still declaring only `head`/`subagent` fails with a `ModelError` naming the
  missing role (loud rename, no silent fallback).

**Details:**
Red→green. Mechanical rename plus one new role; keep the diff boring.

**Acceptance criteria:**
- [ ] `grep -r "subagent" harness/ --include="*.py"` finds no role-key usages (prompt
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
  the task-scoped retry/error middleware, and an explicit recursion bound (D6); the lead's
  `subagents` list declares the researcher; `search_web` moves to researcher tools; the
  lead keeps planning/workspace/`ask_user` tools only.
- `harness/prompts/orchestrator.md` — lead now plans angles and delegates via
  `task(subagent_type="researcher")`; researcher-count guidance bounded per Step 1's
  recorded rate limits (!#5).
- `harness/prompts/subagent.md` — variables filled if the frozen contract carries any;
  content semantics unchanged (parent non-goal).
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
  or round-cap redesign; the general-purpose subagent (stays disabled).

**Tests (write first, confirm red):**
- [ ] Lead → researcher → reader: a scripted run where the researcher's reader digest
  reaches the lead, and the digested source is marked `digested` (R7's mechanism moved, not
  broken).
- [ ] A researcher crash surfaces as a `RESEARCHER FAILED (...)` error ToolMessage to the
  lead; the run continues (existing error middleware, new tier).
- [ ] The researcher's graph carries its own recursion bound, not the ambient 9,999 (D6).
- [ ] The lead's tool surface no longer includes `search_web`/direct fetch routes.

**Details:**
Red→green. Build reader model/spec first, embed in the researcher's middleware, keep
`build_agent`'s signature unchanged. The recursion bound rides the researcher's
`SubAgent.middleware` or its model config — whichever deepagents 0.7.5 actually honors;
confirm against site-packages before implementing (the explorer confirmed inheritance, not
the override mechanism — if no per-subagent bind exists, bound it via the task-tool config
and record the reconciliation).

**Acceptance criteria:**
- [ ] Scripted end-to-end test: two researchers dispatched in one lead turn actually run
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
- [ ] A scripted 3-tier run's report discloses digested / fallback / unread counts that
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
- [ ] The consolidation call runs on the `verifier` role and receives every per-paragraph
  verdict.
- [ ] The rendered report has no `Sources:`/`Verdict:` lines inside `## Answer`, and the
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

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->
