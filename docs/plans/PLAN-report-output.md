# PLAN: Report Output and Default Location

**Status:** In Progress
**Created:** 2026-08-13
**Type:** Single plan

## Intent

**True goal:** A finished report should read as clean prose that a non-technical
reader can follow, with the evidence and a plain-language verdict attached to each
paragraph rather than interrupting its sentences — and everything a run produces
should land in one predictable directory instead of two repo-relative ones.

**Binding outcomes:**
- **R1** — Workspace and reports both default under a home-relative `deep-research`
  directory (`C:\Users\nperez\deep-research` on Windows, `~/deep-research` on the
  homelab Linux box), created on first run.
  - An explicit path in `harness.toml` still wins over the default.
  - Captured sources keep their existing per-run layout beneath it, so a source lands
    at `<deep-research>/workspace/sources/<run_id>/S1.md`.
- **R2** — Report prose carries no per-sentence verification markers and no rendered
  link inside a paragraph.
  - A source marker written mid-sentence disappears from the prose entirely; its link
    is carried to the paragraph's end.
- **R3** — Each paragraph that cites sources ends with a `Sources:` line of
  space-separated markdown links.
  - Links are deduplicated per paragraph and ordered by first appearance in it.
  - A paragraph citing nothing gets no `Sources:` line.
- **R4** — Each paragraph that cites sources carries a `Verdict:` line beneath its
  `Sources:` line, reading one of `supported`, `partially supported`, `not supported`,
  or `no sources cited`, followed by one plain sentence for a non-technical reader.
  - The verdict judges the paragraph as a whole against the sources it cites, not
    sentence by sentence.
  - A list block gets one `Sources:`/`Verdict:` pair for the whole list, whose verdict
    sentence opens with an `n/m bullets verified` rollup; every bullet the sources did
    not support is marked with a trailing `*`.
  - When verification did not run or failed, the line reads `not verified` plus the
    reason rather than being omitted.
- **R5** — A paragraph whose cited sources contradict one another on its content is
  flagged as such in the report.
- **R6** — The run metadata section names the Lead Model and the Subagent Model.
  - The subagent tier is configured but unwired, so this reports what is configured
    for each role regardless of whether that role was invoked.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- Verdict wording stays readable to a production or field technician — no verdict
  vocabulary that assumes knowledge of the harness.
- The claim-boundary work should delete more of `harness/verify.py` than it adds; the
  per-line block machinery and the sentence splitter exist to serve a sentence-sized
  unit that R4 retires.

**Non-goals:**
- Fixing claim-boundary accuracy (orphaned pronouns, abbreviation splits, stripped
  heading scope). The developer's call: an agent architect and better prompting are
  the right tools for that, later.
- Migrating or deleting the existing repo-relative `workspace/` and `reports/`
  contents — new runs use the new location, old files stay where they are.
- Wiring the researcher/reader subagent tiers. R6 reports configuration only.
- Context-overflow guards on the pooled verification prompt. See Constraints.
- Changing what search or fetch do, or how sources are captured.

**Constraints & assumptions:**
- The existing degraded-coverage disclosure — fetch failures, unresolved citation
  markers, rate-limited searches, dead branches — survives untouched. This is the
  project's best-effort-and-disclose invariant and is not what R2 removes.
- Assumes a paragraph cites one or two sources, so a prompt pooling their captured
  text stays far below the model's context window. The developer judged an overflow
  guard to be handling for a case that cannot happen; no such guard is planned.
- `harness.toml` remains the one place a deployment overrides paths; no endpoint,
  model ID, or path is hardcoded.

**Open questions:**
- ~~What happens to an unregistered `[S9]`~~ — settled in D4: dropped from prose and
  from the `Sources:` line; `_gaps_section` continues to disclose it.

## Background

`docs/decisions.md` does not use `D<n>` IDs — the `D3`/`D4`/`D10` references found in
source comments are plan-local IDs in `PLAN-harness-substrate.md` and
`PLAN-research-loop.md`. The `D<n>` entries in THIS plan are their own namespace and do
not renumber those. `PLAN-research-loop.md` D3 is the one this plan deliberately
reverses (see D3 below); `docs/backlog.md` carries an item about `extract_claims`
splitting on bare `.`/`!`/`?`, which Phase 1 retires.

## Codebase Map

- Entry points: `harness/__main__.py:353-372` — runs `verify_claims`, builds
  `RunOutcome(question, answer, registry, usage, cut_short, cut_short_detail, todos,
  started_at, verification)`, calls `write_report(outcome, config)`.
- `harness/report.py:438` `write_report(outcome, config) -> Path` — makes `reports_dir`,
  computes filename, reads `config.roles["head"].model`, delegates to
  `_render_body(outcome, config, model_label, now)` (`391-435`).
- `_render_body` section order: `# {question}` → `## Run metadata` (`_usage_lines`,
  `115-126`) → `## Run cut short` (`_cut_short_section`, `170-193`) → `## Answer`
  (`_annotate`, `272-312`) → `## Working notes` (`_notes_section`, `196-240`) →
  `## Conflicting sources` (`_conflicts_section`, `315-333`) → `## Gaps and
  disclosures` (`_gaps_section`, `336-388`) → `## Sources` (`_sources_section`,
  `149-167`).
- Module boundaries: `report.py` imports `VerificationResult`/`ClaimCheck` from
  `verify.py` — a one-way edge today, and the reason a paragraph type cannot live in
  `report.py` (D1). Both import from `sources.py`.
- Ordering constraint: `report.py:274-276` — markers must be placed BEFORE
  `registry.resolve()`, since resolving rewrites `[S1]` into a link and breaks claim
  matching. Phases 1-3 replace this constraint rather than honor it.
- Reuse targets: `SourceRegistry.link(source_id) -> str` (`sources.py:124`),
  `marker_ids(text) -> list[str]` (`sources.py:19`), `MARKER_RE` (`sources.py:14`),
  `build_chat_model(config, role)` (`models.py:21`), `render(name, **vars)`
  (`prompts.py:32`), `is_failed_capture` / `_sources_dir` (`tools/fetch.py`).
- Config: `AgentSettings.workspace_dir: Path = Field(default=Path("workspace"))` and
  `reports_dir: Path = Field(default=Path("reports"))` (`config.py:80-81`); loaded by
  `load_config` (`config.py:110-126`) via `tomllib` + `model_validate`. No
  `expanduser()`/`resolve()` exists anywhere in `config.py` for these fields.
  `harness.toml:37-38` pins BOTH keys explicitly.
- Readers of those fields: `agent.py:83-84`, `tools/fetch.py:60`, `report.py:221-229`,
  `report.py:446-448`. Nothing but `report.py` writes to `reports_dir`.
- Tests: `tests/test_report.py` (39 tests), `tests/test_verify.py` (24),
  `tests/test_config.py`, `tests/test_sources.py`, `tests/test_agent.py`, shared
  `tests/conftest.py` — fixtures `ScriptedChatModel` (`28-110`), `patch_model`/
  `patch_run` (`113-158`, patches `build_chat_model` in `harness.verify` too),
  `verify_reply` (`161-167`), `write_source_capture`/`write_failed_capture`
  (`176-204`), `make_config` (`220-265`, already overrides both dirs to `tmp_path`).
- Commands: `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` /
  `uv run mypy .`

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- Preserving `ClaimCheck`, `Conflict`, or sentence-level verdicts as a public shape —
  Phase 2 replaces them outright rather than keeping a compatibility layer.
- Deduplicating links ACROSS paragraphs — dedupe is per paragraph only (R3); the
  existing `## Sources` section remains the run-wide list.

## Design Decisions

### D1: Home for the paragraph unit
- **Chosen:** a new `harness/paragraphs.py` owning `Paragraph`, `split_paragraphs`,
  and `strip_markers`. Single purpose, imported by `__main__`, `verify`, and `report`.
- **Rejected:** `report.py` — `report.py` already imports `VerificationResult` from
  `verify.py`, so `verify.py` importing `Paragraph` back is a circular import that
  fails at load. **Rejected:** `sources.py` (the recommendation) — no new file and it
  already owns `MARKER_RE`/`marker_ids`, but the developer chose a single-purpose
  module over growing `sources.py` past citation identity.
- **Consequences:** one more module than the fewer-files rule prefers; `sources.py`
  stays focused; every consumer shares one definition of a paragraph by construction.

### D2: Split once, hand paragraphs forward
- **Chosen:** `__main__` calls `split_paragraphs(answer)` once, passes the list into
  `verify_paragraphs`, and stores it on `RunOutcome`. Verdicts come back index-aligned
  with the input list. Nothing ever re-splits or matches text.
- **Rejected:** a shared splitter with both modules splitting independently and
  aligning by index — same result but two split sites that can silently diverge.
  **Rejected:** `verify.py` returning paragraphs with verdicts attached — forces
  `report.py` to keep a second rendering path for runs where verification never ran.
- **Consequences:** `_place_marker` and its whitespace-tolerant matching
  (`report.py:248-269`) are deleted, not ported. `RunOutcome` gains a `paragraphs`
  field, and `write_report` renders from it whether or not `verification` is set.

### D3: One pooled verification call per paragraph
- **Chosen:** one model call per paragraph carrying ALL that paragraph's usable
  captured sources, returning a paragraph-level verdict, a contradiction flag, and the
  indices of unsupported bullets.
- **Rejected:** paragraph × source (one call per source, isolation preserved,
  conflicts still derived deterministically from disagreeing verdicts). Token cost is
  near-identical — source text dominates and each page is sent once per paragraph
  either way — so the pooled form buys round-trips, not tokens. The developer accepted
  that trade: per-source attribution is not wanted, "does this paragraph hold up" is.
- **Consequences:** deliberately reverses `PLAN-research-loop.md` D3's one-source
  isolation. Source-vs-source contradiction (R5) becomes model-reported rather than
  derived, so `Conflict` and the `by_claim` disagreement scan are deleted. A future
  session must not "restore" isolation without revisiting this entry.

### D4: Failing bullets identified by index, unregistered markers dropped
- **Chosen:** the model returns `unsupported_items` as zero-based indices into the
  paragraph's bullet list; `report.py` places `*` by index. An `[Sn]` with no registry
  entry is stripped from prose and contributes no link.
- **Rejected:** having the model quote the failing bullet text — reintroduces exactly
  the fragile text matching D2 deletes. **Rejected:** leaving an unresolved `[S9]`
  visible in prose — `_gaps_section` already discloses unresolved IDs, so the signal
  has a home that is not the reader's prose.
- **Consequences:** an out-of-range index from the model must be ignored rather than
  crash the render (Phase 3 test).

### D5: Home-relative default, and the TOML that currently masks it
- **Chosen:** `Path.home() / "deep-research" / "workspace"` and `.../ "reports"` as the
  pydantic field defaults, AND the two pinned keys removed from `harness.toml` so the
  default is what this deployment actually uses.
- **Rejected:** changing the field defaults alone — `harness.toml:37-38` sets both
  keys, so pydantic never reaches the default and the change would be invisible here
  while silently altering behavior for any deployment without those keys.
- **Consequences:** `harness.toml` no longer documents the paths by example;
  `docs/guides/setup.md:246-248` must state the new defaults and that the keys are
  optional overrides.

## Requirements Coverage

| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Home-relative workspace + reports default | Phase 4 (defaults resolve under `Path.home()/deep-research`; TOML keys removed) |
| R2 | No inline markers or inline links in prose | Phase 3 (rendered answer contains no `[Sn]` and no link inside a paragraph) |
| R3 | Per-paragraph `Sources:` line | Phase 3 (space-separated, deduped, first-appearance order) |
| R4 | Per-paragraph `Verdict:` line | Phase 1 (unit), Phase 2 (verdict), Phase 3 (rendering + list rollup) |
| R5 | Contradiction flagged | Phase 2 (`sources_conflict` in reply), Phase 3 (section rewired) |
| R6 | Lead + Subagent model in metadata | Phase 4 |

## Progress
- [x] Phase 1: Paragraph unit
- [ ] Phase 2: Pooled paragraph verification
- [ ] Phase 3: Report rendering
- [ ] Phase 4: Default paths and run metadata
- [ ] Final verification

## Phases

### Phase 1: Paragraph unit

**Risk:** none
**Test-first:** required
**Goal:** One deterministic definition of a paragraph, its cited source IDs, and its
bullets, shared by every consumer.
**Requirements:** R4 (unit only)
**Files:**
- `harness/paragraphs.py` — new. The shared unit; cannot live in `report.py` or
  `verify.py` without a circular import (D1).
- `tests/test_paragraphs.py` — new. Mirrors `tests/test_sources.py` in shape.
**Diff budget:** ~120-180 lines across 2 files

**Reuse:**
- Extend `marker_ids` and `MARKER_RE` in `harness/sources.py` — do NOT write a second
  marker scanner.
- Pattern to mirror: `harness/sources.py` — pure module, pydantic `BaseModel` with
  `ConfigDict(extra="forbid")`, module docstring stating what it is NOT responsible for.
- Port the code-fence strip and blank-line block split from `verify.py:139-147`; the
  per-line `_block_units` machinery and the sentence splitter are NOT ported.

**Contracts:**
- `class Paragraph(BaseModel)` — `text: str` (block verbatim, markers intact),
  `source_ids: list[str]` (deduped, first-appearance order), `items: list[str]`
  (bullet texts with their list marker stripped; empty for a non-list block).
- `split_paragraphs(answer: str) -> list[Paragraph]`
- `strip_markers(text: str) -> str` — removes every `[Sn]` and collapses the
  whitespace it leaves behind.

**Out of scope:**
- Any change to `verify.py`, `report.py`, or `sources.py`.
- Rendering, links, verdicts — this phase produces data only.
- Fixing pronoun/abbreviation accuracy (Intent non-goal).

**Tests (write first, confirm red):**
- [ ] Blocks split on blank lines; fenced code blocks are removed entirely and never
  become a paragraph.
- [ ] `source_ids` collects every `[Sn]` in the block, deduplicated, in first
  appearance order; a block with no marker yields an empty list.
- [ ] A bullet list yields one `Paragraph` whose `items` holds each bullet in order
  with the list marker stripped; mixed `-`/`*`/`1.` markers and a lead-in line
  directly above the list are all handled.
- [ ] A heading line stays inside its block's `text` rather than being dropped, so a
  heading with prose beneath it and no blank line is one paragraph.
- [ ] `strip_markers` removes markers mid-sentence and at end of line without leaving
  double spaces or a space before punctuation.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `harness/paragraphs.py`.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `uv run mypy .` passes with the new module.
- [x] `harness/paragraphs.py` imports nothing from `report.py` or `verify.py`.

### Phase 2: Pooled paragraph verification

**Risk:** flagged (!#1)
**Test-first:** required
**Goal:** Replace the per-(sentence x source) pass with one pooled model call per
paragraph returning a paragraph verdict, a contradiction flag, and failing bullet
indices.
**Requirements:** R4, R5
**Assumes:**
- Phase 1's `Paragraph` contract exists as specified.
**Files:**
- `harness/verify.py` — modify. `verify_paragraphs` replaces `verify_claims`;
  `extract_claims`, `_block_units`, `_check_one`, `ClaimCheck`, `Conflict` and the
  `by_claim` scan are deleted.
- `harness/prompts/verify.md` — modify. Rewritten for one paragraph and pooled sources.
- `tests/test_verify.py` — modify. The 4 call-count assertions (`106`, `173`, `206`,
  `484-501`) encode per-(claim x source) granularity and are rewritten, not deleted.
- `tests/conftest.py` — modify. `verify_reply` emits the new JSON envelope.
**Diff budget:** ~200-280 lines across 4 files

**Reuse:**
- Extend `verify.py`'s existing structure — keep the sequential `for` loop with
  `await` inside (no `gather`, no `TaskGroup`), the module-level `VerifyError`, the
  `_parse_reply` fence-tolerant JSON parse, and the broad-`except` best-effort stance
  that records a failure line instead of raising.
- Reuse `is_failed_capture` and `_sources_dir` from `harness/tools/fetch.py`,
  `build_chat_model(config, "head")`, and `render("verify", ...)` unchanged.
- Pattern to mirror: the existing `_check_one` error handling — every failure path
  returns a verdict plus an optional `check_failures` line.

**Contracts:**
- `Verdict = Literal["supported", "partially_supported", "not_supported",
  "no_sources_cited", "not_verified"]` — the last two are assigned deterministically,
  never by the model.
- `class ParagraphVerdict(BaseModel)` — `verdict: Verdict`, `detail: str`,
  `sources_conflict: bool = False`, `unsupported_items: list[int]` (zero-based indices
  into `Paragraph.items`), `source_ids: list[str]` (the sources actually pooled).
- `class VerificationResult(BaseModel)` — `verdicts: list[ParagraphVerdict]`
  (index-aligned with the input list), `check_failures: list[str]`.
- `verify_paragraphs(paragraphs: list[Paragraph], config: HarnessConfig, registry:
  SourceRegistry) -> VerificationResult`
- Model reply JSON: `{"verdict": "supported"|"partially_supported"|"not_supported",
  "detail": "<one sentence>", "sources_conflict": true|false, "unsupported_items":
  [<int>, ...]}`
- `harness/prompts/verify.md` variables: `$paragraph`, `$sources`.

**Out of scope:**
- Any rendering — this phase returns data; `report.py` is untouched.
- Context-window guards, truncation, or source-count caps (Intent non-goal).
- Concurrency: the pass stays strictly sequential.

**Tests (write first, confirm red):**
- [ ] One model call per paragraph regardless of how many sources it cites, and the
  prompt for a paragraph contains every one of its usable captured sources.
- [ ] Deterministic verdicts bypass the model entirely: a paragraph with no markers,
  or whose markers are all unregistered, returns `no_sources_cited` with zero calls.
- [ ] A source whose capture is a `FETCH FAILED` stub or missing file is excluded from
  the pooled prompt; a paragraph left with no usable source returns `not_verified`
  with the reason.
- [ ] A malformed reply, an unknown verdict value, or a raised exception yields
  `not_verified` for that paragraph plus a `check_failures` line, and the loop
  continues to the next paragraph.
- [ ] `sources_conflict` and `unsupported_items` round-trip from the reply into the
  returned `ParagraphVerdict`, and results are index-aligned with the input list.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Rewrite `harness/prompts/verify.md` for one paragraph plus pooled `$sources`,
   keeping the existing supported/contradicts/silent distinction and adding the
   contradiction flag and bullet-index instructions.
3. Rewrite `verify.py`'s pass and delete the retired symbols.
4. Update `tests/conftest.py`'s `verify_reply` to the new envelope.
5. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] `extract_claims`, `_block_units`, `_check_one`, `ClaimCheck` and `Conflict` no
  longer exist anywhere in `harness/`.
- [ ] `uv run ruff check .` reports no unused imports left behind by the deletions.

### Phase 3: Report rendering

**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** Render the answer as clean prose with a per-paragraph `Sources:` and
`Verdict:` line, and wire the split-once flow through `__main__`.
**Requirements:** R2, R3, R4, R5
**Assumes:**
- Phases 1 and 2 landed their contracts as specified.
**Files:**
- `harness/report.py` — modify. `_annotate` and `_place_marker` deleted; a paragraph
  renderer replaces them; `_conflicts_section` reads `sources_conflict`;
  `_gaps_section` and `_sources_section` are NOT changed.
- `harness/__main__.py` — modify. Split once, pass paragraphs to `verify_paragraphs`
  and onto `RunOutcome`.
- `tests/test_report.py` — modify. The 15 `**[...]**` marker assertions are rewritten
  against the new format.
- `tests/test_agent.py` — modify. One marker assertion at `1047`.
**Diff budget:** ~220-300 lines across 4 files

**Reuse:**
- Extend `RunOutcome` with a `paragraphs` field — do NOT introduce a parallel outcome
  type.
- Reuse `SourceRegistry.link` for every rendered link; do NOT format markdown links
  anywhere else.
- Pattern to mirror: the existing section builders (`_sources_section`,
  `_cut_short_section`) — a function returning a string, assembled by `_render_body`.

**Contracts:**
- `RunOutcome.paragraphs: list[Paragraph]` — index-aligned with
  `RunOutcome.verification.verdicts` when verification ran.
- Rendered paragraph format: stripped prose, then `Sources: <link> <link>` when
  `source_ids` is non-empty, then `Verdict: <label> - <detail>` where `<label>` is the
  verdict with underscores replaced by spaces. List blocks carry `*` on each
  `unsupported_items` bullet and open `<detail>` with `n/m bullets verified`.

**Out of scope:**
- `_gaps_section`, `_sources_section`, `_notes_section`, `_usage_lines` — the coverage
  disclosure is an invariant and does not change here.
- Cross-paragraph link dedupe.
- Any change to `verify.py` or `paragraphs.py`.

**Tests (write first, confirm red):**
- [ ] A rendered answer contains no `[Sn]` marker and no markdown link inside a
  paragraph's prose; every link appears on a `Sources:` line.
- [ ] A citing paragraph renders `Sources:` with space-separated, deduped links in
  first-appearance order, followed by `Verdict:`; a non-citing paragraph renders
  neither line.
- [ ] A list block renders one pair, with `*` on exactly the `unsupported_items`
  bullets and an `n/m bullets verified` rollup opening the detail sentence.
- [ ] `verification is None`, a short `verdicts` list, or an out-of-range bullet index
  renders `not verified` (or omits the marker) rather than raising.
- [ ] A paragraph whose verdict carries `sources_conflict` appears in
  `## Conflicting sources`, and `## Gaps and disclosures` still lists unresolved IDs
  and fetch failures unchanged.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the paragraph renderer in `report.py`; delete `_annotate` and
   `_place_marker`; rewire `_conflicts_section`.
3. Wire `__main__` to split once and pass paragraphs forward.
4. Run the full suite; confirm green.

**Acceptance criteria:**
- [ ] A full `uv run pytest` passes, with all 16 former marker assertions rewritten
  rather than removed.
- [ ] `_place_marker` and `_annotate` no longer exist in `harness/`.

### Phase 4: Default paths and run metadata

**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** Both output directories default under a home-relative `deep-research`
directory, and run metadata names both configured models.
**Requirements:** R1, R6
**Files:**
- `harness/config.py` — modify. Both `AgentSettings` path defaults.
- `harness.toml` — modify. Remove the two pinned `[agent]` path keys (D5).
- `harness/report.py` — modify. `_usage_lines` / `_render_body` gain Lead Model and
  Subagent Model lines.
- `tests/test_config.py` — modify. `373-378` asserts the documented defaults.
- `docs/guides/setup.md` — modify. `246-248` documents both paths.
**Diff budget:** ~60-100 lines across 5 files

**Reuse:**
- Extend `AgentSettings` in `harness/config.py` — do NOT add a path helper module for
  two `Field(default_factory=...)` values.
- Read model names via `config.roles["head"].model` / `config.roles["subagent"].model`,
  the same accessor `write_report` already uses.

**Contracts:**
- `AgentSettings.workspace_dir` default resolves to `Path.home()/"deep-research"/
  "workspace"`; `reports_dir` default resolves to `Path.home()/"deep-research"/
  "reports"`. Both remain plain `Path` fields overridable from `harness.toml`.
- `## Run metadata` contains a `Lead Model` line and a `Subagent Model` line.

**Out of scope:**
- Migrating or deleting existing `workspace/` and `reports/` contents.
- `expanduser()` handling of a `~`-prefixed value supplied IN `harness.toml` — the
  default is computed, not parsed.
- Wiring the subagent tier (Intent non-goal).

**Tests (write first, confirm red):**
- [ ] With no `[agent]` path keys present, both defaults resolve under
  `Path.home()/"deep-research"` — asserted against `Path.home()`, never a literal
  `C:\Users\...`, so the test passes on the Linux runner.
- [ ] An explicit `workspace_dir`/`reports_dir` in TOML still overrides the default.
- [ ] `## Run metadata` names both configured models, including when head and subagent
  are configured to the same model.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Change the two defaults, remove the pinned keys from `harness.toml`, add the two
   metadata lines.
3. Update `docs/guides/setup.md` to state the new defaults and that the TOML keys are
   optional overrides.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] `docs/guides/setup.md` no longer documents `workspace`/`reports` as the paths.
- [ ] A run started with no `[agent]` path keys writes its report under
  `Path.home()/"deep-research"/"reports"`.

## Verification
- [ ] `uv run pytest`
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] CI's 90% coverage floor still met (`.github/workflows/ci.yml`).
- [ ] End-to-end: `python -m harness "<question>"` writes a report under
  `~/deep-research/reports/` whose answer has no inline markers, whose citing
  paragraphs each carry `Sources:` and `Verdict:` lines, and whose captured sources
  are under `~/deep-research/workspace/sources/<run_id>/`.

## Notes
- The 16 marker assertions and 4 call-count assertions are ports, not deletions. If a
  behavior genuinely no longer exists, say so in `## Discoveries` rather than dropping
  the test silently.
- Phase 1 restores heading text to the verification prompt as a side effect (headings
  stay in the block rather than being dropped as they were in `_block_units`). This is
  an improvement, not a requirement — do not build on it.
- `tests/conftest.py`'s `patch_model`/`patch_run` patch `build_chat_model` in
  `harness.verify` by name; renaming the call site there breaks the fixture.

## Risks
#1. **The pooled reply contract is the riskiest assumption in the plan** — Phase 2
    asks one model call to return a verdict, a one-sentence detail, a contradiction
    boolean, and a list of bullet indices, from a prompt holding several full source
    captures. `deepseek-v4-flash` may return prose around the JSON, omit
    `unsupported_items`, or index bullets from 1. The parse must tolerate all three
    (fence-stripping already exists; treat a missing list as empty and ignore
    out-of-range indices) and the phase is sequenced second so this is answered before
    rendering is built on it. Confirm with a live run, not only scripted replies.
#2. **Changing the defaults silently relocates where a deployment writes** — the repo's
    `harness.toml` pins both keys today, so removing them (D5) is what actually moves
    the output on this machine. Any other checkout without those keys changes location
    on upgrade with no migration. Existing `workspace/` and `reports/` contents are
    deliberately left in place (Intent non-goal), so the developer will have two
    locations until they clean up manually.
#3. **20 existing assertions must be ported under pressure to weaken them** — 16 on the
    `**[...]**` marker format and 4 encoding per-(claim x source) call counts. The
    failure mode is rewriting an assertion into something that cannot fail (asserting
    a substring so generic it always matches) to get green. Each rewritten assertion
    must still name a specific expected string or count.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

### 2026-08-13 — Phase 1: uncompiled regexes in `strip_markers` (deferred)
The per-line loop in `harness/paragraphs.py` uses three inline `re.match`/`re.sub`
patterns, the module's only uncompiled ones; hoisting them beside `_FENCE_RE` would match
the file's own convention. The `indent_match ... else ""` guard is also unreachable
(`[ \t]*` always matches) and exists only for mypy. Cosmetic, no behavior change —
deferred rather than churn a green phase.

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. -->

### 2026-08-13 — Phase 1: Paragraph unit
- Done: added `harness/paragraphs.py` (`Paragraph`, `split_paragraphs`, `strip_markers`)
  and `tests/test_paragraphs.py` (11 tests). Nothing else changed. Full suite 281 passed;
  ruff, format, and mypy clean.
- Learned: the session opened on a worktree branched from `main`, where none of the files
  this plan modifies exist — they live only on `origin/research-loop`. The branch was
  rebased onto `origin/research-loop` (developer's call) before any code was written, and
  the Codebase Map then matched line-for-line. A later session must stay on that base.
  `_LIST_ITEM_RE` is now duplicated between `paragraphs.py` and `verify.py:38`; Phase 2
  deletes the consumers of the verify.py copy.
- Drift: none — the plan text was correct; the worktree base was wrong.
- Watch-next: Phase 2 is flagged (!#1). Confirm the pooled reply contract tolerates prose
  around the JSON, a missing `unsupported_items`, and 1-based bullet indices — and check
  it against a live run, not only scripted replies.
