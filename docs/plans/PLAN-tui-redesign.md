# PLAN: TUI Redesign

**Status:** In Progress
**Created:** 2026-08-17
**Type:** Single plan

## Intent

**True goal:** Update the already-built, already-tested TUI (`harness/display.py`, a
Rich `Live(screen=True)` renderer wired to a working `harness/agent.py` research loop)
to match the developer's new five-screen HTML mockup — an interactive welcome screen,
a richer running pane, an in-place `ask_user` overlay, and a reader-subagent strip —
without changing the loop's reasoning, stage semantics, or verification behavior.

**Binding outcomes:**
- **R1** — The five mockup screens (welcome, running head-only, running with readers
  in flight, question overlay, finished) render faithfully to the mockup's layout,
  palette, and information hierarchy; terminal-impossible details (shadows, radii)
  are skipped, not imitated.
  - Authoritative design source: `docs/design/deep-research-tui.html` (2026-08-17
    revision — the one where the question is an in-pane overlay).
- **R2** — While running, one pane shows: the task ledger (extends the existing
  `TodosUpdated` checklist with per-task meta text), the completed-stage timeline
  (exists), the current stage line now including elapsed time and round budget
  (`round N/max_rounds`), warning lines (exists, unchanged), a structured live
  tool-call log (tool name / argument summary / result / timing, replacing the
  current free-text activity tail), and a reader-subagent strip present only while
  reader tasks are in flight.
- **R3** — The welcome screen accepts the research question interactively (arrow-key
  line editing, Enter runs it) and supports exactly `/help`, `/sources`, `/model`
  (an up/down-navigable picker over `roles.head.choices` in `harness.toml`, session-
  only — never written to disk). Invoking with a question already on argv
  (`python -m harness "<question>"`) skips the welcome screen entirely — the existing
  scripted/CI path is unchanged.
- **R4** — `ask_user` renders as an in-place cyan overlay inside the running frame
  (task ledger and stage line stay visible, the overlay covers the tool-log region),
  the stage clock pauses, the answer is typed inline with arrow-key editing, and on
  submit the overlay retracts and the run resumes in place — replacing today's
  suspend-to-normal-screen behavior.
- **R5** — The finished summary (already implemented via `RunFinished`) keeps
  reporting stage timings, source counts, cut-short/verification/incident counts, and
  additionally states the report path inline (today it is a separate `print`
  elsewhere) so the spec's single reportpath block is one visual unit.
- **R6** — The terminal is always restored cleanly under the NEW input paths too:
  Ctrl+C while the welcome screen is being typed into, and Ctrl+C while the `ask_user`
  overlay is open, both restore the terminal exactly as the existing running-state
  Ctrl+C path already does.
- **R7** — Renders correctly on modern truecolor terminals broadly (WezTerm is the
  reference terminal; already true for the existing renderer — confirmed again for
  the new screens).

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- Blinking caret, overlay retract motion, and other animation niceties where cheap
  inside the existing `Live` refresh model.
- The block-glyph split-color wordmark as drawn in the mockup.
- Status bar content (cwd:searxng endpoint, version) as shown on the welcome screen.

**Non-goals:**
- Any change to `harness/agent.py`'s reasoning graph, stage order, or verification
  logic — R2's stage order (`clarifying → researching → verifying → writing`) is
  already the real, tested behavior of `harness/display.py`'s `Stage` type and is
  rendered as-is, not redesigned.
- Full command palette (opencode-style filterable menu) — only the three commands.
- REPL loop — one-shot lifecycle is the existing, frozen entrypoint contract.
- Any web-frontend (React/Vite) code.
- Mouse support.
- 256-color / no-unicode terminal fallbacks.
- User-configurable theming.
- Rate-limit retry, PDF classification, or any other existing `docs/backlog.md` item.

**Constraints & assumptions:**
- The loop is ALREADY BUILT and live-tested (`harness/agent.py`, `models.py`,
  `verify.py`, `report.py`, `runlog.py`) — this plan touches it ONLY through the one
  narrow, explicit hook Phase 6 adds for reader visibility; every other phase is
  confined to `harness/display.py`, a new `harness/input.py`, and `harness/__main__.py`.
  - CONFIRMED BINDING 2026-08-17: the reader-strip hook (R2) is IN SCOPE, developer-
    approved, scoped to Phase 6 only.
- Roles are `head` / `researcher` / `reader` / `verifier` per the real
  `harness.toml` (NOT the head/subagent split assumed before real exploration);
  `/model` maps to `roles.head.model`.
- Left/right arrows move the cursor within the input buffer; up/down navigate
  multi-line input and the `/model` picker. No mouse support. No command history.
- `rich==15.0.0` and `langchain-openai==1.4.2` are ALREADY direct, pinned
  dependencies — no new TUI-framework dependency is needed.
- Performance: existing `refresh_per_second=4` default is sufficient.
- Deployment target is the homelab Linux box over SSH; development happens on
  Windows (WezTerm).

**Open questions:**
- None — resolved by direct exploration of `development`'s real `harness/display.py`,
  `harness/agent.py`, `harness/__main__.py`, `harness/config.py`, `harness/report.py`.

## Codebase Map
Subagent-confirmed 2026-08-17 by direct exploration of the `development` branch (NOT
this stale worktree — see `## Reconciliations`).

- Display (`harness/display.py`, 355 lines): typed events —
  `StageStarted(stage)`, `StageCompleted(stage, elapsed_seconds)`, `Activity(text)`,
  `Question(text)`, `Alert(text)`, `RunFinished(stage_timings, usable_sources,
  unusable_sources, cut_short, verification_failures, incidents)`,
  `TodosUpdated(todos: tuple[TodoItem, ...])` where `TodoItem(content, status)`.
  `Renderer` protocol: `emit`, `suspend() -> AbstractContextManager[None]`, `close()`.
  `RichRenderer` owns one `Live(screen=True, refresh_per_second=4)`; `PlainRenderer`
  is the non-TTY fallback (`build_renderer()` picks by `sys.stdout.isatty()`).
  `Question` today is HELD then printed via `suspend()` — Live stops, a cyan `Panel`
  prints on the NORMAL screen, then resumes — this is what R4 replaces.
  `StageTracker` owns stage timing state outside the renderer.
- Entry (`harness/__main__.py`): `python -m harness "<question>"` — question is a
  REQUIRED positional argv arg today, no welcome screen exists. Preflights `head`
  and `verifier` roles. Drives `agent.astream(...)`, reading `node_update.get("todos")`
  and `"__interrupt__"`. `ask_user` answer today comes from `_answer_questions` →
  `_read_answer()`, a daemon-thread `input()` bridge to an asyncio Future (line-
  buffered stdin, not raw-key). `rounds_used`/`max_rounds` are tracked locally
  (`_note_model_turns`) but NOT currently passed to any display event — R2's
  round/elapsed display needs a small new event field or event, not a new tracker.
  Report path is printed as the frozen LAST line of stdout — R5 must not disturb
  that contract, only add it to the `RunFinished` render too.
  Terminal restore: `try`/`finally` around the Live-owning renderer (module docstring:
  "the live region owns terminal state").
- Loop topology (`harness/agent.py`, 374 lines): nested deep-agents-style tiers —
  head → researcher (`_researcher_spec`) → reader (`_reader_spec`), built via
  `SubAgentMiddleware` wrapping the `task` tool. Confirmed by the module's own Drift
  C comment: "the lead's own search_web/fetch_pages calls moved onto the nested
  researcher/reader tiers, **which this top-level stream never sees**" — reader
  dispatch is invisible to `__main__.py`'s stream BY DESIGN. `_ReaderDigestMiddleware`
  (wraps `awrap_tool_call`) is the EXISTING pattern for hooking a `task` call —
  Phase 6's reader-visibility hook mirrors this, not agent.py's core graph.
  `ask_user` (`harness/tools/ask_user.py`) never executes on the real path — intercepted
  by `interrupt_on`/`HumanInTheLoopMiddleware`, which is why `Question` in display.py
  is fed from `_answer_questions`, not the tool itself.
- Config (`harness/config.py`): `RoleConfig(provider, model)` — no `choices` field
  yet, must be added. Real `harness.toml` roles: `[roles.head]` (model `kimi-k3`),
  `[roles.researcher]` (`deepseek-v4-pro`), `[roles.reader]` (`deepseek-v4-flash`),
  `[roles.verifier]` (`gpt-5.6-luna`). `[agent]` already carries `max_rounds = 50`,
  `wall_clock_seconds = 1800` — matches the mockup's "50 rounds / 30 min" exactly;
  budgets are NOT new scope, they already exist and are already enforced by
  `harness/__main__.py`/`agent.py`.
- Persistence (`harness/report.py`, `harness/config.py`): `run_workspace_dir(config,
  run_id) -> <workspace_dir>/<run_id>` already holds `sources/S<n>.md` capture files;
  `write_report` writes `<reports_dir>/YYYY-MM-DD-HHMMSS-<slug>.md`. No structured
  per-run sources manifest exists yet — `/sources` needs ONE new small artifact
  (a manifest written alongside the report), not a from-scratch persistence layer.
- Tests: `tests/test_display.py` mirrors `display.py` — asserts on `RichRenderer`
  output via a `Console`/`StringIO` pair with an ANSI-stripping helper, and on
  `PlainRenderer` via `capsys`; `tests/conftest.py` has `drain_stdout`,
  `install_search_transport`, `patch_run`, `verify_reply`, `write_source_capture`,
  `write_failed_capture` fixtures to reuse. CI enforces 90% coverage on `harness/`.
- Commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`.

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- No change to `harness/agent.py`'s `SubAgent` specs, tool wiring, or interrupt
  registration beyond Phase 6's additive visibility hook.
- No change to `harness/verify.py`, `harness/paragraphs.py`, `harness/report.py`'s
  claim-checking behavior.
- No new TUI framework or dependency — Rich is already pinned and in use.
- No change to the frozen "report path is the last line of stdout" contract for the
  argv-question invocation path.

## Design Decisions

### D1: Module layout — extend `display.py` in place; new sibling `input.py`
- **Chosen:** `harness/display.py` keeps owning events + renderers (extended, not
  replaced); a new `harness/input.py` holds raw-key reading and the pure line-buffer
  editor (insert-at-cursor, backspace, arrows, Ctrl+J), reused by BOTH the welcome
  screen (Phase 2) and the `ask_user` overlay's inline answer (Phase 5).
- **Rejected:** a `harness/display/` subpackage — the repo's convention is flat,
  single-purpose modules (`config.py`, `sources.py`, `report.py`, `agent.py`); only
  `tools/` is a package, justified by multiple distinct tool implementations, which
  doesn't apply here.
- **Consequences:** `input.py`'s line-buffer/parser logic is pure and testable
  without a terminal; only its raw-mode read loop is I/O and thin.

### D2: Welcome screen is argv-optional, not argv-replacing
- **Chosen:** `python -m harness "<question>"` behaves exactly as it does today
  (skips welcome, runs immediately, frozen stdout contract intact). `python -m
  harness` with no argv question shows the interactive welcome screen instead.
- **Rejected:** always-interactive (breaks the existing scripted/CI invocation and
  the pinned `test_the_clarification_prompt_never_reaches_stdout`-style stdout
  contract tests); a new `--interactive` flag (extra surface for no real benefit
  when "no argv" is already the natural signal).
- **Consequences:** two entry paths through `main()` converge before the stage
  machine starts; tests must cover both.

### D3: `ask_user` overlay reuses the input-handling foundation, not a second one
- **Chosen:** Phase 5 (overlay) depends on Phase 2's `input.py` line editor for the
  inline answer field, and replaces `_read_answer`'s daemon-thread `input()` bridge
  with the same raw-key loop feeding the `Live` frame.
- **Rejected:** keeping the daemon-thread `input()` bridge and just changing where
  the `Panel` prints (doesn't give inline typed echo, and leaves two separate input
  mechanisms in the codebase).
- **Consequences:** the async Future bridge pattern in `_answer_questions` is
  replaced; the wall-clock-while-prompting behavior it existed for must be preserved
  (Risk #2).

### D4: Reader-strip visibility — additive middleware hook, mirroring `_ReaderDigestMiddleware`
- **Chosen:** developer-approved. A new middleware wrapping `awrap_tool_call` on the
  researcher's nested `task(subagent_type="reader")` dispatch (same shape as the
  existing `_ReaderDigestMiddleware`) emits reader-start/reader-done events through a
  shared sink threaded down like `SourceRegistry`/`RunLog` already are, surfaced to
  `harness/display.py` as a new `ReadersUpdated` event.
- **Rejected:** trimming the reader strip out of scope (developer explicitly chose
  to include it); walking LangGraph's nested subgraph stream directly (couples
  display code to graph internals the loop doesn't expose as a stable seam).
- **Consequences:** this is the plan's only `harness/agent.py` touch — narrow,
  additive, and reviewed as its own phase; a reader crash must still not kill the
  run (existing `_reader_failure_message` behavior is unchanged).

### D5: `/sources` reads a new small per-run manifest, not raw capture files
- **Chosen:** at run end, write `<workspace_dir>/<run_id>/sources.json`
  (`{run_id, question, sources: [{id, url, title, outcome}], usable, unusable}`)
  alongside the existing `sources/S<n>.md` captures; `/sources` reads the newest
  run's manifest by directory mtime.
- **Rejected:** inferring usability by re-parsing `sources/*.md` filenames/content
  (fragile, duplicates logic `report.py`'s `_is_usable` already owns); a full
  event-trace persistence layer (no requirement asks for replay here).
- **Consequences:** one small write added to the existing end-of-run path in
  `harness/__main__.py`/`report.py`; `_is_usable` becomes shared, not report-private.

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Five screens faithful to mockup | Phase 2 (welcome), Phase 3 (running pane), Phase 5 (overlay), Phase 1/existing (finished) |
| R2 | Task meta, round/elapsed, tool-call log, reader strip | Phase 3 (ledger/stage/log), Phase 6 (reader strip) |
| R3 | Interactive welcome + 3 commands | Phase 1 (input foundation), Phase 2 (welcome+commands) |
| R4 | ask_user in-place overlay | Phase 5 |
| R5 | Finished summary + inline report path | Phase 4 |
| R6 | Clean exit on new input paths | Phase 1 (foundation), Phase 5 (overlay Ctrl+C) |
| R7 | Modern truecolor terminals | Final verification (manual, WezTerm) |

## Progress
- [x] Phase 1: Raw-key input foundation
- [ ] Phase 2: Welcome screen and slash commands
- [ ] Phase 3: Running pane — task meta, stage round/elapsed, structured tool log
- [ ] Phase 4: Finished summary — inline report path
- [ ] Phase 5: ask_user in-place overlay
- [ ] Phase 6: Reader-strip visibility hook
- [ ] Final verification

## Phases

### Phase 1: Raw-key input foundation
**Risk:** flagged (!#1)
**Test-first:** required
**Goal:** A pure, testable line-buffer editor (insert-at-cursor, backspace, left/right,
up/down, Ctrl+J, Enter) plus a thin raw-mode key reader, usable by both the welcome
screen and the ask_user overlay.
**Requirements:** R3, R6
**Files:**
- `harness/input.py` — new: `KeyEvent` model, platform raw-key reader (`msvcrt` /
  `termios`+`tty`), pure `LineBuffer` editor (reason: D1 — shared by Phases 2 and 5).
- `tests/test_input.py` — new: mirrors `input.py`.
**Diff budget:** ~220-320 lines across 2 files
**Reuse:** none for the editor itself (genuinely new capability); mirror
`harness/display.py`'s module-docstring convention citing rationale.
**Contracts:**
- `KeyEvent(kind: Literal["char","enter","newline","backspace","left","right","up",
  "down","interrupt"], char: str | None)` and a blocking generator `read_keys() ->
  Iterator[KeyEvent]` — Phases 2 and 5 consume this.
- `LineBuffer` — pure class: `insert(ch)`, `backspace()`, `move_left/right/up/down()`,
  `text() -> str`, `cursor_col`/`cursor_row` — no I/O.
**Out of scope:** No screens, no slash parsing, no ask_user wiring; no key kinds
beyond the contract (Home/End/Delete/history are not in any R).
**Tests (write first, confirm red):**
- [ ] Byte/escape-sequence decoding maps inputs to `KeyEvent`s (table-driven: ANSI
  `ESC [ A-D` and Windows `\xe0`-prefixed scancodes for all four arrows, plus
  Enter/Ctrl+J/Backspace/Ctrl+C).
- [ ] `LineBuffer` ops (insert at cursor, backspace, left/right incl. boundaries,
  up/down across Ctrl+J lines with column clamping) produce expected buffer/cursor
  state (table-driven).
**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the decoder (pragma-excluded blocking read loop per the project's
   existing coverage posture — see `## Notes`) and `LineBuffer`.
3. Run the tests; confirm they PASS (green).
**Acceptance criteria:**
- [x] `uv run mypy .` clean on the new module.

### Phase 2: Welcome screen and slash commands
**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** The welcome screen per the mockup (wordmark, styled input box, hints, roles
line, tip, status bar), driven by Phase 1's `LineBuffer`/key reader; `/help`,
`/sources`, `/model` (up/down picker over `roles.head.choices`) dispatch; argv
invocation still skips it entirely (D2).
**Requirements:** R1, R3
**Assumes:**
- Phase 1's `KeyEvent`/`LineBuffer` exist.
**Files:**
- `harness/display.py` — modify: welcome-screen renderables (wordmark, input box,
  hints, roles line, tip, status bar) and command-output panels.
- `harness/__main__.py` — modify: when no argv question, drive the welcome loop
  (read keys, edit buffer, dispatch commands, resolve to a question) before entering
  the existing run path unchanged (D2).
- `harness/config.py` — modify: `choices: list[str] | None = None` on `RoleConfig`
  (validated non-empty strings when present).
- `harness.toml` — modify: `choices` under `[roles.head]`, developer-supplied
  display names (2026-08-17): Grok 4.5, GLM-5.3, GLM-5.2, GLM-5.1, GPT 5.6 Luna,
  Kimi K3, Kimi K2.7 Code, Kimi K2.6, MiMo-V2.5, MiMo-V2.5-Pro, MiniMax M3,
  MiniMax M2.7, Qwen3.8 Max, Qwen3.7 Max, Qwen3.7 Plus, Qwen3.6 Plus,
  DeepSeek V4 Pro, DeepSeek V4 Flash, Hy3 (unconfirmed — verify with developer;
  map display names to real `roles.head`-compatible model IDs at implement time).
- `tests/test_display.py`, `tests/test_config.py`, a new `tests/test_main_welcome.py`
  — modify/new.
**Diff budget:** ~500-700 lines across 6 files
**Reuse:** Phase 1 `input.py`; existing `RichRenderer`/`Console` construction pattern
in `display.py`; `_StrictModel`/`ConfigDict(extra="forbid")` convention in `config.py`.
**Contracts:**
- `[roles.head] choices = ["...", ...]` config key — `/model` reads it; picking a
  model mutates ONLY the in-memory session `HarnessConfig.roles["head"].model`,
  never writes `harness.toml`.
**Out of scope:** Command palette/filtering; command history; Home/End/word-jump
editing; any other slash command; writing `harness.toml`; changing what argv-mode
invocation does.
**Tests (write first, confirm red):**
- [ ] Slash parsing: `/help`, `/sources`, `/model` dispatch; unknown `/x` renders an
  error hint; leading non-slash text is a question.
- [ ] `/model` picker: up/down moves the highlight through `roles.head.choices` with
  boundary clamping; Enter applies the highlighted model to the session config only.
- [ ] Welcome renderable contains hints, roles line, and the CURRENT head model name
  read from config (not hardcoded).
- [ ] `RoleConfig.choices` validates present/absent/empty-list cases.
- [ ] `python -m harness` (no argv) reaches the welcome loop;
  `python -m harness "question"` does not (D2 regression guard).
**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement welcome renderables, command dispatch, config key, `__main__.py` branch.
3. Run the tests; confirm they PASS (green).
**Acceptance criteria:**
- [ ] Manual on WezTerm: typing feels immediate; `/model` pick changes the model name
  shown in the input box mode row; Enter on a typed question starts the existing run
  path unchanged.

### Phase 3: Running pane — task meta, stage round/elapsed, structured tool log
**Risk:** none
**Test-first:** required
**Goal:** Extend the existing running screen: task items carry optional meta text
(e.g. "14 sources"), the stage line shows elapsed time and `round N/max_rounds`, and
the free-text activity tail becomes a structured tool-call log (tool / arg summary /
result / timing, retry rows styled distinctly).
**Requirements:** R1, R2
**Assumes:**
- None beyond what already exists on `development`.
**Files:**
- `harness/display.py` — modify: `TodoItem` gains optional `meta: str | None`; stage
  line rendering gains elapsed+round; new `ToolCall` event (tool, arg_summary,
  result_summary, elapsed_seconds, retry: bool) replacing free-text `Activity` for
  tool invocations (`Activity` stays for non-tool-call activity lines, if any remain).
- `harness/__main__.py` — modify: emit `ToolCall` events from the existing
  `node_update` parsing (where `_RESEARCH_TOOLS`/tool-call proposals are already
  read) instead of collapsing them to text; pass `rounds_used`/`max_rounds` alongside
  `StageStarted`/existing stage tracking so the stage line can show them.
- `tests/test_display.py`, `tests/test_agent.py` (or equivalent stream-parsing tests)
  — modify.
**Diff budget:** ~400-600 lines across 3 files
**Reuse:** `StageTracker` (extend, don't fork); existing `Console(record=True)`-style
assertion pattern in `tests/test_display.py`.
**Contracts:** none new external — purely internal event/rendering extension.
**Out of scope:** Reader strip (Phase 6); ask_user overlay (Phase 5); any change to
what tools exist or how they're invoked.
**Tests (write first, confirm red):**
- [ ] Task ledger renders per-task meta when present, omits it when absent.
- [ ] Stage line renders `HH:MM · round N/max_rounds` alongside the existing spinner
  and stage name.
- [ ] Tool-call log renders tool/arg/result/timing columns; a retried call is styled
  distinctly; overlong arg/result text truncates with ellipsis instead of wrapping.
**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the event/model extensions and `__main__.py` wiring.
3. Run the tests; confirm they PASS (green).
**Acceptance criteria:**
- [ ] Manual on WezTerm: running screen visibly matches the mockup's running screens
  (structure, palette, hierarchy) side-by-side with `docs/design/deep-research-tui.html`.

### Phase 4: Finished summary — inline report path
**Risk:** none
**Test-first:** required
**Goal:** The existing `RunFinished` summary additionally states the report path as
part of the same visual block (today it prints separately), matching the mockup's
single reportpath unit; no other change to finished-screen content.
**Requirements:** R5
**Files:**
- `harness/display.py` — modify: `RunFinished` gains `report_path: Path | None`;
  `_summary_lines`/`RichRenderer`/`PlainRenderer` render it as one trailing block.
- `harness/__main__.py` — modify: pass the report path into the existing
  `RunFinished` emission instead of a separate `print`.
- `tests/test_display.py`, `tests/test_main_*.py` — modify (existing "report path is
  the last line of stdout" pinned test must still pass unchanged for argv mode).
**Diff budget:** ~120-200 lines across 3 files
**Reuse:** existing `_summary_lines` helper (extend, don't fork).
**Contracts:** none new — additive field on an existing event.
**Out of scope:** Any change to report content or the frozen stdout-last-line
contract itself.
**Tests (write first, confirm red):**
- [ ] `RunFinished` with a `report_path` renders it inline in both renderers.
- [ ] Existing "report path is the last line of stdout" test still passes unchanged.
**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the field and wiring.
3. Run the tests; confirm they PASS (green).
**Acceptance criteria:**
- [ ] `uv run pytest tests/test_display.py tests/test_main_*.py` green.

### Phase 5: ask_user in-place overlay
**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** Replace the current suspend-to-normal-screen `Question` handling with an
in-place cyan overlay inside the running `Live` frame (task ledger + stage line stay
visible, overlay covers the tool-log region), stage clock paused, inline typed answer
via Phase 1's line editor, retracting on submit.
**Requirements:** R1, R4, R6
**Assumes:**
- Phase 1's `input.py` exists; Phase 3's stage-line/tool-log rendering exists (the
  overlay covers that region).
**Files:**
- `harness/display.py` — modify: overlay renderable; `RichRenderer` handles
  `Question`/answer-submit in-frame instead of via `suspend()`; `StageTracker` gains
  pause/resume.
- `harness/__main__.py` — modify: replace `_answer_questions`'s daemon-thread
  `input()` bridge with Phase 1's raw-key loop feeding the overlay's `LineBuffer`
  (D3); preserve the existing behavior that the wall clock keeps running while
  answering.
- `tests/test_display.py`, `tests/test_main_*.py` — modify.
**Diff budget:** ~350-500 lines across 3 files
**Reuse:** Phase 1 `LineBuffer`/key reader (D3); existing `StageTracker`.
**Contracts:** none new external.
**Out of scope:** Multi-question queuing (one pending question at a time, as today);
overlay fade animation beyond appear/retract; any change to WHEN `ask_user` may be
called (still clarifying-stage-only, per the existing `interrupt_on` registration).
**Tests (write first, confirm red):**
- [ ] With a question pending, the running-screen render shows the cyan overlay in
  place of the log region while ledger and stage line remain, and typed characters
  echo inline.
- [ ] After submit, the overlay retracts and the log/stage line resume; the stage
  clock elapsed time excludes the paused interval.
- [ ] Ctrl+C while the overlay is open still restores the terminal cleanly (R6).
**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the overlay, pause/resume, and the raw-key answer path.
3. Run the tests; confirm they PASS (green).
**Acceptance criteria:**
- [ ] Manual on WezTerm: a real clarifying run shows the overlay in place, accepts a
  typed answer with arrow-key editing, and resumes researching visibly.

### Phase 6: Reader-strip visibility hook
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** Surface reader-tier (`task(subagent_type="reader")`) dispatch and
completion up to the display as a `ReadersUpdated` event, rendered as a strip between
the stage line and the tool log, present only while readers are in flight — via one
narrow, additive middleware mirroring the existing `_ReaderDigestMiddleware` pattern.
**Requirements:** R2
**Assumes:**
- Phase 3's stage-line/tool-log layout exists (the strip sits between them).
**Files:**
- `harness/agent.py` — modify: a new middleware wrapping `awrap_tool_call` on
  reader `task` dispatch (mirrors `_ReaderDigestMiddleware`'s existing shape),
  emitting reader-start/reader-done through a shared sink threaded like
  `SourceRegistry`/`RunLog` already are (D4).
- `harness/display.py` — modify: `ReadersUpdated(readers: tuple[ReaderItem, ...])`
  event, `ReaderItem(id, brief, status_text, done: bool)`; strip renderable, present
  only when non-empty.
- `harness/__main__.py` — modify: build and pass the shared reader-activity sink into
  `build_agent` alongside the existing `SourceRegistry`/`RunLog`.
- `tests/test_agent.py`, `tests/test_display.py` — modify.
**Diff budget:** ~350-500 lines across 4 files
**Reuse:** `_ReaderDigestMiddleware`'s exact wrapping pattern (`harness/agent.py`) —
the phase's named reuse target; existing reader-failure handling
(`_reader_failure_message`) is untouched.
**Contracts:**
- `ReadersUpdated` event — the strip's only display-side input; a reader crash still
  surfaces via the EXISTING `_reader_failure_message`/incident path, not duplicated
  here.
**Out of scope:** Nested reader→reader visibility; any change to reader dispatch
concurrency, retry, or failure semantics; researcher-tier visibility (only the
reader tier gets a strip, per the mockup).
**Tests (write first, confirm red):**
- [ ] N reader dispatches (scripted researcher tool calls) each produce a start and
  a done/failed `ReadersUpdated` transition without ID collisions.
- [ ] The strip renders only while at least one reader is live; renders nothing
  otherwise (R2's presence rule).
- [ ] A reader that fails still completes the run (existing `_reader_failure_message`
  behavior unchanged) and is reflected as a failed, not stuck-live, strip row.
**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the middleware, event, sink wiring, and strip renderable.
3. Run the tests; confirm they PASS (green).
**Acceptance criteria:**
- [ ] Manual on WezTerm: a real run with parallel reader delegation shows the strip
  filling and clearing as readers complete, matching the mockup's fan-out screen.

## Verification
- [ ] `uv run pytest` green (CI enforces the 90% floor on `harness/`).
- [ ] `uv run ruff check .` && `uv run ruff format --check .` && `uv run mypy .` clean.
- [ ] Manual on WezTerm (dev) AND over SSH on the homelab: full walkthrough —
  `python -m harness` (welcome, `/help`, `/sources`, `/model`) → a real question →
  clarify overlay (answer it) → researching with visible readers → finished summary
  with inline report path; separately, `python -m harness "<question>"` still runs
  argv-mode unchanged with its stdout contract intact.
- [ ] Ctrl+C at: welcome-screen typing, running, and the ask_user overlay — each
  restores the terminal (R6).

## Notes
- This worktree was branched from a point before the real `harness/agent.py`,
  `display.py`, `report.py`, etc. existed; all exploration for this plan was done by
  reading `development` directly (`git show development:<path>`), not this
  worktree's checkout. Implementation must happen on a branch based on current
  `development`, not this stale worktree branch.
- Coverage policy for the two genuinely un-unit-testable lines (Phase 1's blocking
  key-read loop): `# pragma: no cover` with a one-line reason, matching how the repo
  already treats similarly thin I/O — no coverage-config `omit` entry, no broad
  exclusion.
- `docs/architecture.md` still has stale patches describing an older two-role
  (`head`/`subagent`) model and an outdated head model name, out of step with
  `harness.toml`'s real four roles (`head`/`researcher`/`reader`/`verifier`,
  `kimi-k3` for head) — pre-existing doc drift this plan does not need to fix, but
  implementers should read `harness.toml` directly rather than trust that doc's
  specifics.

## Risks
#1. **Raw-mode input is genuinely new capability threaded through an existing,
    working `Live` loop** — wrong teardown ordering (restore before the reader
    stops, or `Live.stop()` racing an in-flight key read) can leave the terminal raw.
    Mitigation: Phase 1 isolates and tests the pure editor separately from the thin
    I/O loop; Phases 2 and 5 each get an explicit Ctrl+C acceptance check before
    being considered done.
#2. **Replacing `_answer_questions`'s daemon-thread `input()` bridge (D3) touches a
    path that currently keeps the wall clock running while a question is being
    answered** — a naive rewrite could silently stop that. Mitigation: Phase 5's
    tests explicitly assert the wall clock is unaffected by the overlay being open,
    and the existing behavior is named as a precondition to preserve, not rediscover.
#3. **The reader-strip hook is this plan's only touch to `harness/agent.py`**, a
    file with a live, tested, nested-subagent graph. Mitigation: Phase 6 mirrors an
    EXISTING wrapping pattern (`_ReaderDigestMiddleware`) rather than inventing a new
    one, is additive only (no change to dispatch/retry/failure semantics), and ships
    as its own reviewable phase with existing reader-failure tests re-run unchanged.

## Reconciliations
- 2026-08-17 — Dropped `PLAN-agent-loop.md` entirely: exploration against this
  worktree's stale checkout (branched before ~30 commits landed on `development`)
  concluded "no agent loop exists," which was false for the real `development`
  branch. `development` already has a fully built, live-tested research loop
  (`harness/agent.py`, 7 phases, each with a recorded live-check) that meets or
  exceeds what that plan proposed to build. This plan was rewritten from scratch
  against `development`'s real code rather than patched.

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution. Append-only. -->
- 2026-08-19 — Phase 1: `## Notes`' coverage-policy line says Phase 1's `# pragma: no
  cover` "match[es] how the repo already treats similarly thin I/O". It does not — a
  repo-wide grep finds ZERO `pragma` occurrences in `harness/` and no
  `[tool.coverage.report] exclude_lines` in `pyproject.toml`. Phase 1 introduces the
  FIRST pragma in the codebase. The directive itself is unchanged and was followed as
  written (inline pragma + one-line reason, no coverage-config `omit`, no broad
  exclusion); only the "already exists" justification was false. Noted so a later phase
  does not cite Phase 1's pragma as long-standing precedent.
- 2026-08-19 — Phase 1 review, DEFERRED to Phases 2/5: `read_keys()` is a bare
  generator, so tty restore runs in its `finally` only when the consumer closes the
  iterator (loop exit or GC). A consumer that parks the iterator on an object and keeps
  running leaves the terminal raw — exactly risk #1's failure mode, left as a consumer
  obligation rather than made structurally impossible. Deferred deliberately: the plan's
  own risk #1 mitigation assigns terminal-restore proof to Phases 2 and 5's Ctrl+C
  acceptance checks. Preferred fix when Phase 2 wires it: wrap `read_keys` in a
  `contextlib.contextmanager` so raw mode is scoped by `with`, not by GC.
- 2026-08-19 — Phase 1 review, SIMPLIFY (deferred, behavior-changing): `read_keys`'
  POSIX branch builds a per-keystroke `pending` list and a nested `read_char` closure
  purely so the loop can peek for EOF before decoding. Now that both decoders return
  `None` on `""`, the loop could pass `sys.stdin.read(1)` directly and drop the closure —
  but only if the loop no longer needs to distinguish "EOF, stop iterating" from "ignore
  this key", which today it does. Not mechanical; revisit if Phase 2's contextmanager
  rework touches this loop anyway.

## Phase Handoff Log
<!-- Written by /implement at each phase gate. Append-only. MUST remain the LAST section. -->

### 2026-08-19 — Phase 1: Raw-key input foundation
- Done: New `harness/input.py` (`KeyEvent`/`KeyKind`, `decode_posix`, `decode_windows`,
  `read_keys`, `LineBuffer`) and `tests/test_input.py` (42 tests, three parametrized
  tables). Contracts landed exactly as pinned. Both decoders return `None` on the `""`
  EOF sentinel — a review fix applied before commit, since Phases 2 and 5 call them
  directly. 554 tests pass; ruff/format/mypy clean; `harness/input.py` at 91%, package
  total 97% against CI's 90% floor.
- Learned: (1) The stale-worktree warning in `## Notes` is RESOLVED — commit `de4c18f`
  merged `development` in, and `git diff development -- harness/` is empty, so this
  worktree is a valid implementation base after all. (2) `uv` cannot create a `.venv`
  inside this worktree — Windows Application Control blocks it. Every `uv` command must
  run with `UV_PROJECT_ENVIRONMENT=C:/Users/sting/Documents/ai-harness-fun-project/.venv`
  (developer-approved 2026-08-19); brief every implementation subagent with this or its
  first command fails. (3) `harness/` uses frozen dataclasses + `Literal` aliases, never
  pydantic, for small value types; tests use tuple-form `@pytest.mark.parametrize`.
- Drift: none. Two `## Discoveries` entries logged (false pragma precedent in `## Notes`;
  deferred `read_keys` teardown + simplify).
- Watch-next: Phase 2 wires `read_keys()` for the first time. Wrap it in a
  `contextlib.contextmanager` so raw-mode restore is scoped by `with` rather than by the
  consumer closing the generator — that is the deferred half of risk #1, and Phase 2's
  Ctrl+C acceptance check is where it must be proven. Also: Phase 2's `harness.toml`
  `choices` list of 19 model display names is marked UNCONFIRMED in the plan and must be
  confirmed with the developer before implementing, not invented.
