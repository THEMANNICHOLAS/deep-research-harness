# PLAN: Rich CLI Output

**Status:** In Progress
**Created:** 2026-08-14
**Type:** Single plan

## Intent

**True goal:** Make `uv run python -m harness "question"` pleasant to watch — styled, progressing
inline output in the normal terminal flow (scrollback preserved, no alternate-screen takeover)
for the solo operator: local Windows terminal today, SSH to the homelab later.

**Binding outcomes:**
- **R1** — While a research stage runs, the terminal shows that stage as a header with a spinner
  beside it, and live activity lines (tool actions as they happen) indented below it.
- **R2** — Stages are visually separated; a completed stage collapses to a single line
  (check mark + name + elapsed time) with its activity lines removed, so scrollback reads as a
  compact run timeline.
- **R3** — The `ask_user` interrupt renders as a lightly styled question panel with a text input;
  the animation halts while awaiting input and resumes after.
- **R4** — Run end prints a short styled summary (stages, timings, source count, disclosures)
  plus the report file path.
  - Rate-limit/fetch degradations appear in this summary and in the report's disclosure
    sections only — never as live-feed noise (best-effort + disclose is honored at the end,
    not inline).
- **R5** — When stdout is not an interactive terminal (pipe, CI, dumb terminal), output
  auto-degrades to plain sequential text — no ANSI, no spinner, no flags to remember.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- Library choice open but leaning Rich; evaluated with evidence in the design conversation.
- Ollama-style spinner feel; light color; restrained formatting.
- A clean event seam between loop and display is desirable: the developer expects a future
  frontend (possibly Ink/Node-based) to consume the same progress events.

**Non-goals:**
- Full-screen TUI / alternate-screen app (rules out Textual-style takeover and serving the TUI
  over a browser).
- The future web/frontend UI itself.
- Verbosity/quiet flags.
- Rendering the full report as markdown in the terminal.
- Any change to research behavior, stage semantics, or report content.

**Constraints & assumptions:**
- Python-native only — no Node/Ink sidecar (Ink was considered and deferred to the future
  frontend, per the developer).
- The display must observe the loop without violating the invariant that adding capability
  touches no agent-loop internals.
- Must work on Windows Terminal (dev) and Linux over SSH (deployment).

**Open questions:**
- ~~What the stage boundaries actually are in today's loop~~ — answered by exploration: implicit
  in `harness/__main__.py` (interrupt loop → first research-tool proposal → verification call →
  report write); see `## Codebase Map`.
- ~~How the `ask_user` interrupt currently surfaces~~ — answered: `_answer_questions` +
  daemon-thread `input()` bridge; see `## Codebase Map`.

## Codebase Map
- Entry point: `harness/__main__.py` — `main()` drives the whole run; the ONLY console I/O in
  the project is bare `print()` calls here (plus one stderr warning in `harness/tools/fetch.py`).
  No `logging` anywhere.
- Event seam (already exists): `main()`'s `agent.astream(stream_input, config=run_config,
  stream_mode=["updates", "values"])` loop — the single point where todo changes
  (`node_update.get("todos")`), tool-call proposals (`_proposes_research_tool_call`, names in
  `_RESEARCH_TOOLS = {"search_web", "fetch_pages"}`), and `ask_user` interrupts
  (`"__interrupt__"` key) surface. It lives outside `harness/agent.py` — the loop invariant
  holds with zero agent changes.
- Stage boundaries (implicit, inferred in `main()`): clarification = the astream/interrupt loop;
  research begins at first research-tool proposal (same line arms the wall clock via
  `clock.reschedule`); verification = explicit `verify_paragraphs(...)` call after the stream
  loop; report = `write_report(outcome, config)` then `print(path)`.
- Frozen output contract: the report path is the LAST line of stdout — "Nothing may print after
  it" (`harness/__main__.py` module docstring). Errors and the answer prompt go to stderr.
- ask_user today: interrupt caught in `main()` → `_answer_questions(interrupt)` reads
  `interrupt.value["action_requests"]`, `print(question)` per question, then `_read_answer()` —
  a daemon thread writes `"> "` to stderr and blocks on `input()`, bridging the result to an
  asyncio Future so the wall clock can fire mid-prompt. Resume via
  `Command(resume={"decisions": [...]})`.
- End-summary data: `RunOutcome` (`harness/report.py`) has `usage`, `cut_short` /
  `cut_short_detail`, `todos`, `paragraphs`, `verification` (incl. `check_failures`); source
  counts via `outcome.registry.all()` + `_is_usable` (`harness/sources.py`). NO timing fields
  and NO central degradation collector — the display layer computes timings itself and tallies
  disclosures from `RunOutcome`/registry.
- Config pattern: `harness/config.py` — pydantic `_StrictModel` per settings group (not needed
  here; zero-config decision D4).
- Tests: pytest, `asyncio_mode = "auto"`; `tests/test_agent.py` drives the real compiled graph
  with `ScriptedChatModel` (`tests/conftest.py`) and asserts stdout via `drain_stdout(capsys)`
  (~8 call sites assert on todo lines / path line); `test_the_clarification_prompt_never_reaches_stdout`
  pins the prompt to stderr. Coverage: `source = ["harness"]`, 90% floor enforced in CI only.
- Rich: already in `uv.lock` at 15.0.0 as a transitive dep; NOT a direct dependency yet.
- Commands: `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` /
  `uv run mypy .`

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- No `logging` framework introduction — the display layer replaces prints, it does not add logs.
- No token-level streaming display — the stream exposes per-node granularity only; activity
  lines are per tool call / todo change.
- No `DisplaySettings` config surface (D4) — a knob can be added later if a real need appears.

## Design Decisions

### D1: Display library — Rich, pinned `==15.0.0`
- **Chosen:** Rich as a direct dependency, pinned exactly per project convention. Verified
  against Rich 15 docs (context7): `console.status()` provides the Ollama-style spinner;
  `Live` + `live.console.print` supports lines above a live region; `Console(file=StringIO())`
  and `capture()` are the test seams; a `Live` on a non-TTY prints only the final frame —
  which is WHY the plain renderer branch exists (R5) rather than relying on Rich degradation.
  Rich honors `NO_COLOR` and handles Windows terminals natively.
- **Rejected:** Ink — Node/React; would need a Node runtime + IPC sidecar for display only
  (developer defers Ink to the future frontend). Textual — alternate-screen takeover, an Intent
  non-goal. yaspin/halo — spinners only, no live regions/panels. Hand-rolled ANSI — more code
  for less capability.
- **Consequences:** `rich==15.0.0` added to `[project]` dependencies; already resolved in
  `uv.lock` transitively, so lockfile churn is minimal.

### D2: Typed event objects + renderer protocol in a new `harness/display.py`
- **Chosen:** Frozen dataclass events (see Phase 1 Contracts) emitted by `main()` from the
  existing astream loop — the emitter infers stage transitions from the same signals it already
  reads. A `Renderer` protocol with two implementations: `RichRenderer` (TTY) and
  `PlainRenderer` (non-TTY), selected by autodetect. Developer chose typed events explicitly so
  a future frontend can consume the same event stream.
- **Rejected:** Method-call seam without event objects — lighter today, but the developer
  weighed it and wants frontend-consumable events. Inline Rich in `__main__.py` — no seam,
  tangles rendering with loop-driving, hardest to cover at 90%. Agent-middleware observer —
  touches `harness/agent.py` (invariant) and cannot see verification/report stages, which run
  outside the graph.
- **Consequences:** `__main__.py` is the sole emitter; display state (current stage, timings)
  lives in the display layer; later phases add event types additively without protocol changes.

### D3: Activity feed = tool calls + todo changes
- **Chosen:** One activity line per `search_web`/`fetch_pages` proposal (query / URL count) and
  per todo-status change — both already surface in the stream; todos are the agent's own plan
  (research-loop R10 visibility is preserved, now styled).
- **Rejected:** Tool calls only — loses the mid-run plan visibility the harness has today.
- **Consequences:** `format_todos` output stops going to stdout verbatim; existing tests
  asserting it are updated in Phase 1.

### D4: Zero configuration
- **Chosen:** No config surface. TTY → `RichRenderer`, non-TTY → `PlainRenderer`
  (`sys.stdout.isatty()`); Rich honors `NO_COLOR` on its own.
- **Rejected:** `[display]` section in `harness.toml` — one more surface to maintain with no
  present need.
- **Consequences:** R5 requires no flags; tests select renderers directly, not via env tricks.

### D5: `ask_user` input path unchanged; display only renders and yields
- **Chosen:** Keep `_read_answer`'s daemon-thread `input()` bridge exactly as is (it exists so
  the wall clock can fire mid-prompt). The display layer renders the question panel and
  suspends the live region around the blocking read; the answer path and
  `Command(resume=...)` flow are untouched.
- **Rejected:** Rich `Prompt.ask` / `console.input` as the read mechanism — would re-solve the
  blocking-read-vs-wall-clock problem the thread bridge already solves.
- **Consequences:** R3's "animation halts, resumes after" is a renderer `suspend()` concern,
  not an input-flow change; the prompt stays on stderr (pinned by an existing test).

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Stage header + spinner + live activity | Phase 2 |
| R2 | Completed stage collapses to one timeline line | Phase 2 |
| R3 | Styled ask_user panel, animation pauses | Phase 3 |
| R4 | End summary + report path | Phase 4 |
| R5 | Non-TTY plain-text auto-degrade | Phase 1 (plain renderer), Phase 2 (autodetect switch) |

## Progress
- [x] Phase 1: Event seam + plain renderer
- [x] Phase 2: Rich live display
- [x] Phase 3: ask_user question panel
- [x] Phase 4: End-of-run summary
- [ ] Final verification

## Phases

### Phase 1: Event seam + plain renderer
**Risk:** flagged (!#1)
**Test-first:** required
**Goal:** Introduce the typed display events and a plain sequential renderer, and route ALL of
`main()`'s current console output through them — behavior-preserving in substance, so this is
the tracer bullet that retires the stage-inference risk.
**Requirements:** R5
**Files:**
- `harness/display.py` — new: event dataclasses, `Renderer` protocol, `PlainRenderer`,
  `build_renderer` factory (new file because display is a new module boundary per D2).
- `harness/__main__.py` — modify: emit events from the astream loop (stage transitions per
  `## Codebase Map`), replace todo/verification prints; keep error prints on stderr and the
  frozen final path line.
- `tests/test_display.py` — new: unit tests for events + `PlainRenderer` (new test file for the
  new module).
- `tests/test_agent.py` — modify: update the ~8 `drain_stdout` assertions to the plain
  renderer's output.
**Diff budget:** ~250-400 lines across 4 files

**Reuse:**
- Emit from the EXISTING astream loop in `harness/__main__.py` — do NOT add callbacks,
  middleware, or any change to `harness/agent.py`.
- Stage-detection signals already in `main()`: `"__interrupt__"` key,
  `_proposes_research_tool_call`, the `verify_paragraphs` and `write_report` call sites.
- Test pattern to mirror: `tests/test_agent.py` + `drain_stdout` (`tests/conftest.py`).

**Contracts:**
- `Stage = Literal["clarifying", "researching", "verifying", "writing"]`
- Events (frozen dataclasses in `harness/display.py`): `StageStarted(stage: Stage)`,
  `StageCompleted(stage: Stage, elapsed_seconds: float)`, `Activity(text: str)` — later phases
  ADD event types (`Question`, `RunFinished`) without changing these.
- `Renderer` protocol: `emit(event) -> None`, `suspend() -> context manager` (no-op in plain),
  `close() -> None`.
- `build_renderer() -> Renderer` — picks the implementation (Phase 1: always `PlainRenderer`;
  Phase 2 adds the TTY branch).
- Unchanged external contract: the report path remains the LAST line of stdout; stderr keeps
  errors and the `"> "` prompt.

**Out of scope:**
- Any Rich import or styled output (Phase 2). The `rich` dependency is NOT added here.
- The ask_user question path (`_answer_questions` still uses `print`) — Phase 3.
- End-summary content — Phase 4.
- Refactors of `main()` beyond routing output through the renderer; no changes to
  `harness/report.py` or `harness/agent.py`.

**Tests (write first, confirm red):**
- [ ] Each event type renders to the expected plain line via `PlainRenderer` (table-driven).
- [ ] A skipped stage (run with no clarifying questions, or no research tool calls) emits no
  spurious stage events and does not crash — drive the real graph with `ScriptedChatModel`.
- [ ] A full scripted run's stdout still ends with the report path as its final line.
- [ ] Todo changes and research tool proposals each produce an `Activity` line.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `harness/display.py`; rewire `main()` to emit events; update `tests/test_agent.py`
   assertions.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] Full suite green (`uv run pytest`), including the updated `test_agent.py` assertions.
- [x] `test_the_clarification_prompt_never_reaches_stdout` untouched and green.

### Phase 2: Rich live display
**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** Add `RichRenderer` — stage header with spinner, indented live activity lines beneath
it, completed stages collapsing to one `✓ name (elapsed)` scrollback line — selected
automatically on a TTY.
**Requirements:** R1, R2, R5
**Assumes:**
- Phase 1's events and `Renderer` protocol are in place unchanged.
**Files:**
- `pyproject.toml` — modify: add `rich==15.0.0` to `[project]` dependencies (D1); `uv.lock`
  re-resolves (lockfile churn excluded from the diff budget).
- `harness/display.py` — modify: add `RichRenderer`; `build_renderer` gains the
  `sys.stdout.isatty()` branch (D4).
- `tests/test_display.py` — modify: `RichRenderer` tests via injected
  `Console(file=StringIO(), force_terminal=True)`.
**Diff budget:** ~200-320 lines across 3 files (+ lockfile)

**Reuse:**
- Rich primitives per D1: a `Live` region holding the current stage's header + spinner +
  activity lines; collapse by printing the one-line summary above the region
  (`live.console.print`) and resetting the renderable.
- Inject the `Console` into `RichRenderer` (default: real stdout console) — this is the test
  seam D2's consequences require for the 90% floor.

**Contracts:**
- `RichRenderer(console: Console | None = None)` — injectable console, consumed by Phase 3/4
  tests.
- `build_renderer()`: TTY → `RichRenderer`, non-TTY → `PlainRenderer` (R5's switch, final form).

**Out of scope:**
- Question panel and `suspend()` behavior beyond a working no-op (Phase 3).
- Summary rendering (Phase 4).
- Any spinner/theme configurability; one chosen spinner, light color only.

**Tests (write first, confirm red):**
- [ ] `StageStarted` shows the stage header; `Activity` lines appear indented under it;
  `StageCompleted` leaves exactly one collapsed line with elapsed time in the recorded output.
- [ ] `build_renderer` picks `RichRenderer` on a TTY and `PlainRenderer` otherwise.
- [ ] Recorded output contains no leftover live-region frames after `close()`.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the dependency (`uv lock` / `uv sync`), implement `RichRenderer`, wire the autodetect
   branch.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Manual: a live run (or scripted-model run on a real terminal) shows spinner + activity,
  and completed stages collapse; scrollback reads as a timeline.
- [ ] Piping the same run to a file yields plain sequential text with no ANSI codes.

### Phase 3: ask_user question panel
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** The `ask_user` interrupt renders as a lightly styled question panel; the live region
and spinner suspend during the blocking read and resume after — input flow unchanged (D5).
**Requirements:** R3
**Assumes:**
- Phase 2's `RichRenderer` with injectable console is in place.
**Files:**
- `harness/display.py` — modify: add `Question(text: str)` event; implement `suspend()` for
  `RichRenderer` (stop/restart the live region); panel rendering.
- `harness/__main__.py` — modify: `_answer_questions` emits `Question` and wraps
  `_read_answer()` in `renderer.suspend()`; remove the bare `print(question)`.
- `tests/test_display.py`, `tests/test_agent.py` — modify: panel rendering + suspension tests;
  update clarification-flow assertions.
**Diff budget:** ~80-150 lines across 4 files

**Reuse:**
- `_read_answer`'s daemon-thread bridge stays byte-for-byte the input mechanism (D5).
- `Question` event follows Phase 1's event contract additively.

**Out of scope:**
- Any change to `_read_answer`, the resume `Command`, or interrupt handling in
  `harness/agent.py`.
- Multi-question layout polish beyond one panel per question.
- Rich `Prompt`/`console.input` (rejected in D5).

**Tests (write first, confirm red):**
- [ ] `Question` renders as a panel containing the question text (recorded console).
- [ ] `suspend()` stops the live region before the body runs and restores it after (observable
  via recorded output ordering).
- [ ] Scripted clarification round-trip still resumes the run and the prompt stays on stderr.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the event, panel, and suspension; rewire `_answer_questions`.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Manual: mid-run clarification shows the panel, spinner freezes during typing, resumes
  after answering.

### Phase 4: End-of-run summary
**Risk:** none
**Test-first:** required
**Goal:** On run end, both renderers print a short summary — stage timings, source counts,
disclosures (cut-short reason, verification check failures, unusable sources) — followed by the
report path as the frozen final stdout line.
**Requirements:** R4
**Assumes:**
- Phases 1-3 landed; `RunOutcome` fields as described in `## Codebase Map`.
**Files:**
- `harness/display.py` — modify: add `RunFinished` event carrying the summary data; render in
  both renderers; stage timings computed from the `StageStarted`/`StageCompleted` events the
  renderer already saw.
- `harness/__main__.py` — modify: build the `RunFinished` event from `RunOutcome` + registry
  after `write_report`, emit it, THEN print the path.
- `tests/test_display.py`, `tests/test_agent.py` — modify: summary rendering + end-of-run
  ordering tests.
**Diff budget:** ~120-200 lines across 4 files

**Reuse:**
- Disclosure data comes from existing fields only: `RunOutcome.cut_short`/`cut_short_detail`,
  `verification.check_failures`, registry usable/unusable partition (`_is_usable` semantics via
  `harness/sources.py`) — do NOT build a new degradation collector.

**Contracts:**
- `RunFinished` carries: per-stage elapsed times, usable/unusable source counts, cut-short
  reason (if any), verification failure count. Exact field names are 3C's call; the DATA SET is
  frozen here.
- The report path remains printed by `main()` after the summary — the summary never prints
  below it.

**Out of scope:**
- Rendering report content or verdict text in the terminal.
- New fields on `RunOutcome` (timings stay display-side).
- Inline/live degradation warnings (Intent R4 case: end-summary only).

**Tests (write first, confirm red):**
- [ ] Summary renders stage timings, source counts, and each disclosure kind when present, and
  omits empty sections (table-driven over both renderers).
- [ ] A cut-short run's reason appears in the summary.
- [ ] Full scripted run: summary appears, and the report path is still the last stdout line.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `RunFinished` construction and rendering.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Manual: end of a real run shows the styled summary; `python -m harness "q" > out.txt`
  still ends with the path line.

## Verification
- [x] `uv run pytest` — full suite green (324 passed).
- [x] `uv run ruff check .` and `uv run ruff format --check .` — clean.
- [x] `uv run mypy .` — clean.
- [x] CI coverage floor: 90% on `harness/` holds with `harness/display.py` counted (local
  spot-check: 98% total, display.py 100%).
- [ ] Manual e2e (needs API keys + SearXNG): one live run on a terminal — spinner, collapsing
  stages, question panel if a clarification fires, end summary, path last; one piped run —
  plain text, no ANSI.

## Notes
- CLAUDE.md forbids emoji in code; the check mark is `✓` (U+2713, a text symbol, not emoji).
  If the implementor finds it garbled on legacy Windows conhost, fall back to `[done]`/`ok` —
  Windows Terminal (the dev environment) renders it fine.
- `tests/test_agent.py` currently asserts `format_todos` lines verbatim on stdout; Phase 1
  deliberately changes that surface — update assertions, do not preserve the old format.
- `format_todos` in `harness/report.py` may end up with `__main__.py` as its only remaining
  caller gone; leave `report.py` untouched (report file still uses its own rendering) and let
  ruff flag any genuinely dead import.

## Risks
#1. **Stage inference is heuristic — skipped stages must not break the timeline.** Stages are
    inferred from stream signals, not declared by the loop (see `## Codebase Map`). A run with
    zero clarifying questions, zero research calls, or an error cut-short can skip stages or end
    mid-stage. Phase 1's emitter must treat every transition as optional and idempotent
    (entering "researching" twice, or finishing from any stage, is normal). The scripted-graph
    tests cover exactly these paths; if a real run surfaces an unmodeled transition, reconcile
    rather than patch around it.
#2. **Rich `Live` + asyncio + Windows terminal is the least-proven combination.** The spinner
    animates on Rich's refresh thread while the event loop awaits `astream`; contention shows up
    as flicker or torn frames, and Windows adds encoding/ANSI variables. Mitigation: keep ONE
    live region, low refresh rate, all prints routed through the same `Console`; Phase 2's
    manual acceptance run on Windows Terminal is the real gate.
#3. **Suspension around the blocking `input()` must actually stop the refresh thread.** If
    `suspend()` only hides the renderable but the refresh thread keeps painting, typed input
    interleaves with frames. `Live.stop()`/restart semantics (or a transient Live per stage) is
    the intended mechanism; the ordering test in Phase 3 plus the manual clarification run gate
    it. The wall clock firing MID-prompt must still tear down cleanly — `close()` while
    suspended is a legal call sequence.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

### 2026-08-14 — suspend() tests cannot catch a no-op suspend (deferred)
Phase 3 review (Minor): the recorded-console ordering tests for `RichRenderer.suspend()`
pass even if `suspend()` were a no-op (`console.print` repaints an active Live region
anyway), so the automated suite does not gate risk #3's real failure mode — typed input
interleaving with spinner frames. The manual clarification run in final verification is the
actual gate. A stronger test needs `auto_refresh=True` plus timing assertions (flaky-prone);
deferred by the developer.

### 2026-08-14 — RunFinished.cut_short is stringly-typed (deferred)
Phase 4 review (advisory): `RunFinished.cut_short: str | None` could reuse
`CutShortReason` (harness/report.py:42), but that adds a display->report import the event
layer deliberately avoids (display stays standalone for the future frontend). Revisit only
if the literals ever drift; moving the Literal into display (report imports it) would be
the clean direction.

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->

### 2026-08-14 — Phase 1: Event seam + plain renderer
- Done: `harness/display.py` (frozen event dataclasses, `Renderer` protocol, `PlainRenderer`,
  `StageTracker` with injectable clock, `build_renderer`); `main()` routes all stdout through
  the seam (path line stays last); `tests/test_display.py` (unit + graph-driven); review fix
  factored the httpx stub triplication into `tests/conftest.py::install_search_transport`.
- Learned: only two `test_agent.py` assertions were format-sensitive (clarifying-question
  ordering updated; todo-content substring survived unchanged). Reviewer confirmed risk #1
  retired: tracker idempotent, every cut-short path converges on `advance("writing")`,
  skipped-stage graph tests in place. `_proposes_research_tool_call` became
  `_research_tool_calls` + `_describe_tool_call` in `__main__.py`.
- Drift: none
- Watch-next: Phase 2 adds `rich==15.0.0` + `RichRenderer(console=...)`; the manual Windows
  Terminal run is the real gate for risk #2 (Live + asyncio + Windows).

### 2026-08-14 — Phase 2: Rich live display
- Done: `RichRenderer` (one lazy `Live`, `refresh_per_second=4`, `transient=True`, spinner
  header + dim 8-line activity tail, collapse via `console.print` after clearing the region);
  `build_renderer` TTY switch final; `rich==15.0.0` pinned in pyproject. Review fixes: pre-stage
  activities no longer cleared by `StageStarted` (initial todo plan now renders on TTY) +
  `Live.start(refresh=True)` so the first frame paints; tail-cap test asserts the rendered
  frame delta, not the private list.
- Learned: printing through an active `Live` repaints the current renderable after the printed
  line — the region must be cleared to empty BEFORE printing the collapse line, or stale
  activity text lands as the buffer's final content. `Live.start()` does not paint (refresh
  defaults False); with `auto_refresh=False` nothing renders until an update.
- Drift: none
- Watch-next: Phase 2's manual acceptance (live terminal run: spinner/collapse; piped run: no
  ANSI) is still PENDING — fold it into the final-verification manual e2e. Phase 3 implements
  real `suspend()` (stop/restart the Live) + `Question` panel; `close()` while suspended must
  stay legal (risk #3).

### 2026-08-14 — Phase 3: ask_user question panel
- Done: `Question` event (union extended additively); plain rendering byte-identical to the
  old `print(question)`; Rich renders a cyan-bordered `Panel`; real `suspend()` —
  `Live.stop()` (joins the refresh thread), restart in `finally` guarded by
  `was_running and self._stage is not None and not self._closed`; `_answer_questions(interrupt,
  renderer)` emits `Question` and wraps only `_read_answer()` in `suspend()`. D5 held:
  `_read_answer`, resume `Command`, stderr prompt untouched.
- Learned: the recorded-console suspend tests pass under a no-op suspend too (see
  `## Discoveries`) — risk #3's true gate is the manual clarification run, still pending.
  Wall clock firing mid-prompt: `finally` restarts the live briefly, then the normal
  teardown path closes it — clean, by design.
- Drift: none
- Watch-next: manual acceptance for Phases 2 AND 3 rides on the final-verification e2e run
  (spinner/collapse, panel + frozen spinner during typing, piped no-ANSI). Do NOT commit the
  stray `docs/plans/PLAN-reader-delegation.md` (concurrent planning session's file). Phase 4
  is unflagged: `RunFinished` summary in both renderers, path still last.

### 2026-08-14 — Phase 4: End-of-run summary
- Done: `RunFinished` event (stage timings tuple, usable/unusable counts, cut-short reason,
  verification failure count); `StageTracker` accumulates timings at both emit sites and
  exposes `timings()`; `_summary_lines` is the single content source for both renderers;
  `main()` emits between `finish()` and `close()`, path still last. Review verdict: clean;
  one advisory (stringly-typed cut_short) logged to `## Discoveries` as deferred.
- Learned: usable/unusable counts reuse `report._is_usable` via private import — sanctioned,
  keeps report.py untouched. Phase's test_agent.py edits proved unnecessary (graph tests
  landed in test_display.py instead; no existing test modified).
- Drift: none
- Watch-next: Final verification — automated gates + coverage spot-check, then the MANUAL
  e2e gates all three deferred manual acceptances (Phase 2 spinner/collapse + piped no-ANSI,
  Phase 3 frozen-spinner clarification, Phase 4 styled summary with path last).

### 2026-08-14 — Final verification (automated half)
- Done: all four automated verification items pass at HEAD (324 tests, ruff check/format,
  mypy, coverage 98% with display.py at 100%). All four phases committed.
- Learned: nothing new.
- Drift: none
- Watch-next: the ONLY open item is the manual e2e (needs API keys + SearXNG): one live
  terminal run — spinner, collapsing stages, question panel freezing the spinner if a
  clarification fires, styled summary, path last — and one piped run (`> out.txt`) showing
  plain text with no ANSI. That run also discharges risks #2/#3 and the manual acceptance
  boxes in Phases 2-4. Then set Status: Complete and check Final verification + the manual
  boxes.
