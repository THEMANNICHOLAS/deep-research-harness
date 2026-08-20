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
  line editing, Enter runs it) and supports exactly `/help`, ~~`/sources`,~~ `/model`
  (an up/down-navigable picker over `roles.head.choices` in `harness.toml`, session-
  only — never written to disk). ~~`/sources`~~ dropped 2026-08-19 — see
  `## Reconciliations`; the command dispatch table must stay trivially extensible so it
  can be added later without reshaping the welcome loop. Invoking with a question already on argv
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
**MOOT as of 2026-08-19 — `/sources` dropped from scope; see `## Reconciliations`. The
entire decision below is struck and NOT implemented. No `sources.json` is written and
`report.py`'s `_is_usable` stays report-private.**
- ~~**Chosen:** at run end, write `<workspace_dir>/<run_id>/sources.json`~~
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
| R2 | Task meta, round/elapsed, tool-call log, reader strip | Phase 3 (ledger/stage), Phase 6 (reader strip + tool-call log) |
| R3 | Interactive welcome + ~~3~~ 2 commands (`/sources` dropped) | Phase 1 (input foundation), Phase 2 (welcome+commands) |
| R4 | ask_user in-place overlay | Phase 5 |
| R5 | Finished summary + inline report path | Phase 4 |
| R6 | Clean exit on new input paths | Phase 1 (foundation), Phase 5 (overlay Ctrl+C) |
| R7 | Modern truecolor terminals | Final verification (manual, WezTerm) |

## Progress
- [x] Phase 1: Raw-key input foundation
- [x] Phase 2: Welcome screen and slash commands
- [x] Phase 3: Running pane — task meta, stage round/elapsed (~~structured tool log~~ → Phase 6)
- [x] Phase 4: Finished summary — inline report path
- [x] Phase 5: ask_user in-place overlay
- [ ] Phase 6: Reader-strip visibility hook + structured tool-call log
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
~~`/sources`,~~ `/model` (up/down picker over `roles.head.choices`) dispatch; argv
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
- [ ] Slash parsing: `/help`, ~~`/sources`,~~ `/model` dispatch; unknown `/x` renders an
  error hint; leading non-slash text is a question. Add: an unregistered-but-reserved
  name is treated as unknown, proving the dispatch table is data, not branches.
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
(e.g. "14 sources"), the stage line shows elapsed time and `round N/max_rounds`, ~~and
the free-text activity tail becomes a structured tool-call log (tool / arg summary /
result / timing, retry rows styled distinctly)~~ — the tool-call log MOVED TO PHASE 6
on 2026-08-19 (see `## Reconciliations`); it needs data this top-level stream cannot
see.
**Requirements:** R1, R2
**Assumes:**
- ~~None beyond what already exists on `development`.~~ FALSE for the tool log — see
  `## Reconciliations`. True for the remaining scope (task meta + stage line), which
  needs nothing new.
**Files:**
- `harness/display.py` — modify: `TodoItem` gains optional `meta: str | None`; stage
  line rendering gains elapsed+round; ~~new `ToolCall` event (tool, arg_summary,
  result_summary, elapsed_seconds, retry: bool) replacing free-text `Activity` for
  tool invocations~~ (moved to Phase 6). `Activity` is UNCHANGED and keeps its five
  existing non-tool-call emission sites.
- `harness/__main__.py` — modify: ~~emit `ToolCall` events from the existing
  `node_update` parsing (where `_RESEARCH_TOOLS`/tool-call proposals are already
  read) instead of collapsing them to text;~~ pass `rounds_used`/`max_rounds` alongside
  `StageStarted`/existing stage tracking so the stage line can show them.
- `tests/test_display.py`, `tests/test_agent.py` (or equivalent stream-parsing tests)
  — modify.
**Diff budget:** ~~~400-600~~ ~200-320 lines across 3 files (tool log moved out)
**Reuse:** `StageTracker` (extend, don't fork); existing `Console(record=True)`-style
assertion pattern in `tests/test_display.py`.
**Contracts:** none new external — purely internal event/rendering extension.
**Out of scope:** Reader strip (Phase 6); the structured tool-call log (MOVED to Phase 6
2026-08-19 — it requires the `agent.py` middleware Phase 6 already owns); ask_user
overlay (Phase 5); any change to what tools exist or how they're invoked.
**Tests (write first, confirm red):**
- [ ] Task ledger renders per-task meta when present, omits it when absent.
- [ ] Stage line renders `HH:MM · round N/max_rounds` alongside the existing spinner
  and stage name.
- [ ] ~~Tool-call log renders tool/arg/result/timing columns; a retried call is styled
  distinctly; overlong arg/result text truncates with ellipsis instead of wrapping.~~
  MOVED to Phase 6.
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
- `tests/test_display.py`, ~~`tests/test_main_*.py`~~ — modify (existing "report path is
  the last line of stdout" pinned test must still pass unchanged for argv mode). The
  `test_main_*.py` glob is struck 2026-08-20 — no such file holds the pin; see
  `## Reconciliations` for where it actually lives and what it actually asserts.
**Diff budget:** ~120-200 lines across 3 files
**Reuse:** existing `_summary_lines` helper (extend, don't fork).
**Contracts:** none new — additive field on an existing event.
**Out of scope:** Any change to report content or the frozen stdout-last-line
contract itself.
**Tests (write first, confirm red):**
- [x] `RunFinished` with a `report_path` renders it inline in both renderers.
- [x] Existing "report path is the last line of stdout" test still passes unchanged.
**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the field and wiring.
3. Run the tests; confirm they PASS (green).
**Acceptance criteria:**
- [x] ~~`uv run pytest tests/test_display.py tests/test_main_*.py` green.~~ Amended
  2026-08-20 (`## Reconciliations`): `uv run pytest tests/test_display.py
  tests/test_ask_user.py tests/test_agent.py` green — the files that actually pin the
  last-line contract.

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
- `harness/input.py` — modify: ADDED to this phase 2026-08-20, developer-approved; gains an
  idempotent `restore_terminal()`. See `## Reconciliations`.
- `tests/test_display.py`, `tests/test_input.py`, `tests/test_ask_user.py`,
  `tests/test_agent.py` — modify. (~~`tests/test_main_*.py`~~ struck: that glob matches only
  Phase 2's unrelated welcome-screen tests, as Phase 4 already established.)
**Diff budget:** ~350-500 lines across ~~3~~ 4 source files (source came in at 397)
**Reuse:** Phase 1 `LineBuffer`/key reader (D3); existing `StageTracker`; Phase 2's per-row
cursor placement, extracted to a shared `_build_cursor_rows`.
**Contracts:** none new external.
**Out of scope:** Multi-question queuing (one pending question at a time, as today);
overlay fade animation beyond appear/retract; any change to WHEN `ask_user` may be
called (still clarifying-stage-only, per the existing `interrupt_on` registration).
**Tests (write first, confirm red):**
- [x] With a question pending, the running-screen render shows the cyan overlay in
  place of the log region while ledger and stage line remain, and typed characters
  echo inline.
- [x] After submit, the overlay retracts and the log/stage line resume; the stage
  clock elapsed time excludes the paused interval.
- [x] Ctrl+C while the overlay is open still restores the terminal cleanly (R6).
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
**ABSORBED FROM PHASE 3 (2026-08-19 — see `## Reconciliations`):** the structured
tool-call log. The same middleware that reports reader dispatch must also report every
tool call from the nested tiers (tool name, argument summary, result summary, elapsed,
retry flag) as a `ToolCall` event, replacing the free-text activity tail for tool
invocations. This is why the log lives here and not in Phase 3: the nested tiers' tool
calls never reach `__main__.py`'s top-level stream, so the log needs the very hook this
phase adds. `Activity` keeps its five non-tool-call emission sites. Add to this phase's
tests: tool/arg/result/timing columns render, a retried call is styled distinctly, and
overlong arg/result text truncates with an ellipsis rather than wrapping. Revised diff
budget: ~550-750 lines across 4 files.
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
- 2026-08-19 — Phase 2: R3 bound "exactly `/help`, `/sources`, `/model`", but the plan
  assigned `/sources` a data source (D5's per-run `sources.json`) that NO phase's file
  list ever writes — `/sources` would have read a manifest nothing produced. Raised as a
  plan gap rather than improvised. **Developer decision: drop `/sources` entirely — "not
  needed yet" — and instead require that command dispatch be extensible enough to add it
  later without reshaping the welcome loop.** Amendment, authoritative over the struck
  text above: Phase 2 ships exactly TWO commands, `/help` and `/model`. D5 is MOOT and
  not implemented — no `sources.json` is written, and `report.py`'s `_is_usable` stays
  report-private (so Phase 2 no longer touches `report.py` at all). Command dispatch must
  be a DATA structure (name → handler mapping), not an if/elif chain, so adding
  `/sources` later is one table entry plus its handler; a test asserts an unregistered
  name falls through to the unknown-command hint. Consequence for the mockup: the
  welcome tip line "Run /sources to see what the last run captured" would advertise a
  command that does not exist, so it renders the `/help` tip instead — a Preference-level
  deviation from R1's mockup fidelity, permitted by `## Intent`'s Preferences clause.
- 2026-08-19 — Phase 3: the phase's step "emit `ToolCall` events from the existing
  `node_update` parsing (where `_RESEARCH_TOOLS`/tool-call proposals are already read)"
  rests on a premise this plan contradicts elsewhere. There is no `_RESEARCH_TOOLS`
  constant, and the tool calls the mockup's log shows (`search_web`, `fetch_pages`,
  retry rows) execute inside the researcher/reader subgraphs and NEVER reach
  `harness/__main__.py`'s top-level `astream` — exactly as this plan's own
  `## Codebase Map` records for `agent.py` ("which this top-level stream never sees").
  Only `task(subagent_type="researcher")` dispatches are observable there, so Phase 3's
  `**Assumes:** None beyond what already exists` was false and the log could not be built
  as written. **Developer decision: split Phase 3 — ship task meta + stage round/elapsed
  now, and MOVE the structured tool-call log to Phase 6**, whose already-sanctioned
  `agent.py` middleware (D4) is the only place the nested tiers' tool calls are visible.
  Deciding axis: whether the log must show real nested tool activity (it must, for R1/R2
  fidelity), which forces it behind an `agent.py` hook. This keeps the plan's
  one-`agent.py`-touch constraint intact — one middleware, one flagged review — instead of
  adding a second instrumentation point ahead of Phase 6. R2 is now covered by Phase 3
  (ledger/stage) plus Phase 6 (reader strip + tool log); Phase 3's diff budget drops to
  ~200-320 lines and Phase 6's rises to ~550-750.
- 2026-08-19 — Phase 2: the mockup's roles line (`subagent … · verifier … · budget …`)
  encodes the stale two-role model that `## Constraints` already overrides with the real
  four roles. Developer decision: the roles line renders `researcher · reader ·
  verifier · budget`, all read from config — the head model is omitted there because the
  input box's mode row already shows it. Same four-slot shape as the mockup, no
  duplication, no hidden role.
- 2026-08-20 — Phase 4: two premises in the phase spec were false, one of them
  constraining the design. (1) `**Files:**` named `tests/test_main_*.py` as the home of the
  "report path is the last line of stdout" pin; the only matching file is
  `tests/test_main_welcome.py` (Phase 2's welcome-screen tests, unrelated). The pin actually
  lives in `tests/test_display.py` (4 sites), `tests/test_ask_user.py` (3 sites) and
  `tests/test_agent.py` (1 site), so the acceptance command as written would have run the
  welcome tests and MISSED every file this phase endangers. (2) The pin is STRONGER than the
  plan's paraphrase: `tests/test_agent.py:1128-1132` does not merely check
  `endswith(".md")` — it takes `lines[-1].strip()` as a path, asserts `Path(...).exists()`
  and that its parent is `reports_dir`. So the last stdout line must stay a BARE, existing
  path; any label prefix (`report: <path>`) on that line breaks it, and so does anything
  printed to stdout after the summary. **Developer decision (deciding axis: whether mockup
  fidelity may cost a frozen machine-readable contract — it may not):** render the mockup's
  `reportpath` block as a dim `report written` label line followed by the bare path on its
  OWN line in the accent color, as the summary's trailing lines; drop the mockup's accent
  LEFT-BORDER on the path line rather than imitate it, since the border character would sit
  exactly where the bare path must start. This is a Preference-level mockup deviation,
  permitted by `## Intent`'s Preferences clause. The mockup's dim explanatory sentence BELOW
  the reportpath block stays out of scope (it would also take the last-line slot).
  `harness/__main__.py`'s bare `print(path)` is removed as the phase spec directs; both
  renderers' `close()` print nothing, so the summary genuinely becomes the last stdout
  output. Amended file list and acceptance command are struck in place above.
- 2026-08-20 — Phase 5: the phase's `**Files:**` list covered only `display.py`,
  `__main__.py` and tests, but the overlay's key reader has to run on a daemon thread (the async
  side must only ever `await`, or the event loop is blocked and the `asyncio.timeout` wall clock
  can never fire — risk #2's failure mode reached from an unexpected direction). Raw mode is
  process-global state owned by whoever set it, and a thread parked in a blocking read cannot be
  made to give it back: `read_keys()`'s generator is EXECUTING, so it cannot be closed from the
  main thread, and a wall-clock cancellation abandons that daemon with its `finally` unrun —
  leaving the operator's shell in raw mode with no echo. **Developer decision (deciding axis:
  whether Phase 5 may widen its file list to make terminal restore structural — it may):** add
  `harness/input.py` to the phase, with a module-level registered restore closure and an
  idempotent `restore_terminal()` that `read_keys()`' own `finally` and the overlay's `finally`
  both call. One restore path, safe to call twice and from either thread. The rejected
  alternative — accept a raw tty on wall-clock expiry and log it — was declined; pausing the
  wall clock while the overlay is open was never on the table, being forbidden by the phase
  spec.

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
- 2026-08-19 — Phase 2 review, DEFERRED (developer chose to defer): `/model` on a role
  whose config omits `choices` enters picker mode before checking it, so the user sees an
  empty "Select model" panel with inert up/down and a silent Enter, and no notice saying
  why. Unreachable from the shipped `harness.toml` (which now defines 19 choices) but
  reachable from any config omitting the key, which `RoleConfig`'s validator deliberately
  permits. Fix when touched: check `choices` BEFORE setting `mode="model_picker"` and set a
  notice instead.
- 2026-08-19 — Phase 2 review, SIMPLIFY (deferred): `harness/display.py`'s `_OK` / `_CYAN`
  palette constants are defined but unused this phase. Kept deliberately — the palette is
  transcribed from the mockup once so it lives in exactly one place (CLAUDE.md), and
  Phases 3/5/6 render the ok/cyan states. Drop them if those phases land without using
  them. Second item: `state.panel = build_model_picker(...)` in `_run_welcome` is
  recomputed by `_view()` on every path where `choices` is non-empty, so the picker is
  built twice per keystroke; the eager assignment only matters in the empty-choices case
  above, and both should be resolved together.
- 2026-08-19 — Phase 3 review, DEFERRED: `RoundsUpdated` is emitted BEFORE the round-overrun
  check in `harness/__main__.py`, so an overrun turn paints `round 51/50` in the stage line
  for one frame. Harmless, but reads as a bug to an operator. Fix when that area is touched:
  emit after the check.
- 2026-08-19 — Phase 3, KNOWN LIMITATION of the task-meta wiring: `meta` is recomputed only
  when the todo list itself changes (the `todos != last_todos` dedupe), so the `N sources`
  count can lag behind reality between todo updates. Re-emitting `TodosUpdated` on every
  stream chunk would spam the frame, so the dedupe stays. Phase 6 owns the mockup's other
  meta variant (`3 in flight`, which needs reader visibility) and will need live-updating
  meta anyway — resolve both together there.
- 2026-08-20 — Phase 4 review, FIXED (recorded because Phases 5 and 6 will hit it):
  `Console.print(some_str, style=...)` does NOT reliably apply that style. Rich runs a raw
  string through console MARKUP parsing (a `[` in the text raises `MarkupError` from inside
  `emit`) and through `ReprHighlighter`, whose per-token colours OVERRIDE the `style=`
  argument. For a filesystem path this is platform-dependent: on POSIX the highlighter claims
  the whole path and paints it magenta, so the requested colour never appears; on Windows the
  backslash form does not match its path pattern, so the colour survives on the separators
  while the date digits come out repr-number cyan. The Phase 4 accent test passed on the
  Windows dev box against exactly that bug and would have gone RED on the Linux CI runner.
  Two lessons: (1) render any dynamic, styled string as `Text(value, style=...)` — the
  convention `harness/display.py` already uses at its `Question` and `Alert` sites — and add
  `soft_wrap=True` when the value must stay on one copy-pasteable line; (2) an
  `assert "38;2;r;g;b" in raw` style assertion is too weak to catch this, because a shredded
  line still contains the escape somewhere. Assert the whole value inside ONE span:
  `f"[38;2;{r};{g};{b}m{value}[0m" in raw`. Phase 5's overlay and Phase 6's tool-call
  log and reader strip all render dynamic text with explicit styles.
- 2026-08-20 — Phase 5: `Renderer.suspend()` / `RichRenderer._suspend()` are now
  PRODUCTION-UNREACHABLE. `_answer_questions` was their only caller and the overlay replaced it.
  Deliberately kept: `suspend` is a `Renderer` protocol member and four tests still pin its
  Live start/stop behavior, so removing it plus its tests is scope creep inside a flagged phase.
  Delete it in a later cleanup if nothing claims it.
- 2026-08-20 — Phase 5 review, SIMPLIFY (report, correctly deferred): the
  `KeyEvent` -> `LineBuffer` dispatch chain now exists TWICE — the overlay's key loop in
  `_read_answer` and Phase 2's welcome loop. That is the second occurrence, and CLAUDE.md's rule
  is to factor out when the same lines are about to appear a THIRD time, so both stay inline for
  now. A shared `_apply_key(buffer, event)` collapses them when a third consumer appears.
- 2026-08-20 — Phase 5 review, FIXED, and the lesson generalises: a test asserting the flagged
  risk's OUTER symptom is not a test of the risk. The wall-clock tripwire patches `_read_answer`
  away wholesale and the Ctrl+C test's fake key source yields immediately, so BOTH stayed green
  against a rewrite that read keys synchronously on the loop thread — the exact regression risk
  #2 names. The test that actually discriminates blocks the fake key source on a
  `threading.Event` and asserts `asyncio.wait_for(..., 0.2)` raises `TimeoutError`, i.e. the loop
  was alive to time out, with a watchdog releasing the block so a blocking implementation fails
  rather than hangs. It was verified BOTH ways before being trusted: it passes against the
  shipped shape and fails against a deliberately blocking one. Phase 6's middleware is also
  concurrency-shaped — hold its tests to the same standard.
- 2026-08-20 — Phase 5, KNOWN OPERATOR HAZARD (accepted, disclosed on screen): the clock the
  overlay freezes is RUN elapsed, while the WALL clock that terminates the run keeps counting.
  A run can therefore be cut short at a wall time the visible `MM:SS` never displayed. R4 and the
  mockup both require the pause, so it stays; the overlay's note line reads `clock paused while
  the agent waits` rather than the mockup's `stage clock paused ...`, because what freezes is not
  a per-stage timer.

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

### 2026-08-19 — Phase 2: Welcome screen and slash commands
- Done: Welcome screen per the mockup (verbatim block-glyph wordmark, accent-bar input box
  with reverse-video cursor, hints, roles line, tip, status bar), `_COMMANDS` dispatch
  TABLE with `/help` + `/model`, `RoleConfig.choices` + 19 slug IDs under `[roles.head]`,
  and `nargs="?"` so argv mode is untouched. `/model` mutates the session config only —
  a test asserts `harness.toml` is byte-identical after a pick. 589 tests pass; all four
  gates clean; coverage 97% (display 99%, config 100%, `__main__` 94%).
- Learned: (1) The implementation subagent DIED on an API error mid-verification; all six
  files were already written, so I finished the gates myself rather than respawning —
  check `git status` before assuming a dead worker did nothing. (2) The review caught a
  Major the whole green gate missed: `cursor_col` is a PER-ROW column, and rendering it
  against the newline-joined text drew the cursor on the wrong line. Fixed by returning
  one `Text` per line (`_build_ask_rows`); the regression guard that actually catches it
  through the public API is the accent-bar count test. (3) A non-tty stdin now gets
  argparse's usage error instead of a `termios` traceback — so any test exercising the
  welcome loop must `monkeypatch.setattr("sys.stdin.isatty", lambda: True)`.
- Drift: YES — `/sources` dropped and D5 made MOOT by developer decision; two
  `## Reconciliations` entries are authoritative over the struck text. Phase 2 ships TWO
  commands, not three, and never touches `report.py`.
- Watch-next: Phase 3 renders the running pane. Reuse the palette constants already in
  `harness/display.py` (`_OK`/`_CYAN` are defined and waiting) rather than adding literal
  colors — if Phase 3 ends up not using them, drop them per the logged SIMPLIFY item.
  `harness/input.py` now exports `scoped_keys`, which Phase 5 must use for the overlay's
  key source instead of writing its own teardown.

### 2026-08-19 — Phase 3: Running pane (task meta + stage round/elapsed)
- Done: `TodoItem.meta` (defaulted, rendered in `_MUTED`, wired from a new `_sources_read`
  helper counting registry sources with `read_mode != "unread"`, attached to the
  `in_progress` row only), a `RoundsUpdated` event, and the stage line's right-aligned
  `MM:SS · round N/max_rounds`. Palette gained `_ACCENT_2`/`_FG_2`/`_RULE`/`_PENDING`, and
  the checklist/rule/panel/heading styles moved off raw Rich strings onto those constants.
  601 tests pass; all four gates clean; coverage 97%.
- Learned: (1) The tool-call log was SPLIT OUT to Phase 6 — the nested tiers' tool calls
  never reach `__main__.py`'s top-level `astream`, which the plan's own Codebase Map already
  recorded. Check that map before planning anything that reads tool activity from the lead's
  stream. (2) `Live` redraws whatever renderable it HOLDS, so a pre-built `Group` freezes any
  render-time value — the elapsed clock only advanced when an event arrived. `Live` must be
  constructed with `get_renderable=<builder>` and callers must use `_live.refresh()`, never
  `_live.update(...)`, which would silently reintroduce the freeze. (3) A green gate proved
  nothing about either defect: no test repainted without an event, and no test checked that
  anything in production actually SETS `meta`. Both were caught by review, not by tests.
- Drift: YES — Phase 3 split, tool-call log moved to Phase 6 (`## Reconciliations`,
  developer-approved). Phase 3's diff budget dropped to ~200-320 lines; Phase 6 rose to
  ~550-750 and absorbed the log.
- Watch-next: Phase 4 (finished summary, inline report path) is small and unflagged — the
  pinned "report path is the last line of stdout" test must keep passing UNCHANGED for argv
  mode. Then Phase 5 (overlay) needs `harness/input.py`'s `scoped_keys` and must preserve the
  wall-clock-while-answering behavior named in risk #2. Phase 6 now owns THREE things, not
  one: reader strip, tool-call log, and live-updating task meta.

### 2026-08-20 — Phase 4: Finished summary (inline report path)
- Done: `RunFinished` gained `report_path: Path | None`, `_summary_lines` appends a
  zero-indent `report written` label plus the bare path as the summary's trailing two lines,
  and `harness/__main__.py`'s standalone `print(path)` is gone (its two stderr branches
  preserved by inverting the condition to `if path is None:`). The path line renders as
  `Text(line, style=_ACCENT)` with `soft_wrap=True`. 609 tests pass; all four gates clean.
- Learned: (1) The "report path is the last line of stdout" contract is STRONGER than the
  plan said — `tests/test_agent.py:1128-1132` constructs a `Path` from `lines[-1]` and asserts
  it exists under `reports_dir`, so that line must stay bare. That is why the mockup's accent
  left-border on the path line was dropped rather than imitated. (2) A styled raw string
  handed to `Console.print` loses its style to Rich's `ReprHighlighter`, platform-dependently
  — see the `## Discoveries` entry; this cost a Blocker that the green gate could not see
  because the test asserted the escape appeared ANYWHERE rather than around the whole value.
  (3) The plan's file globs are not trustworthy: `tests/test_main_*.py` matches only Phase 2's
  unrelated welcome-screen tests.
- Drift: YES — 2026-08-20 entry in `## Reconciliations`: Phase 4's `**Files:**` named the wrong
  test file for the last-line pin and understated what the pin asserts; the file list and the
  acceptance command are struck in place and amended, and the render design (bare accent path
  line, no left border) was approved off that.
- Watch-next: Phase 5 (ask_user overlay) is flagged (!#2) — the wall clock must keep running
  while a question is being answered, which is the behavior the daemon-thread `input()` bridge
  in `_answer_questions` exists for; assert it, do not rediscover it. Use
  `harness/input.py`'s `scoped_keys` for the overlay's key source rather than writing new
  teardown, and render every dynamic overlay string through `Text(...)` per the new
  `## Discoveries` entry.

### 2026-08-20 — Phase 5: ask_user in-place overlay
- Done: The question now renders as a cyan `ask_user` panel INSIDE the running `Live` frame,
  replacing the activity lines only — checklist, timeline and stage line stay visible (R4).
  New `AnswerDraft`/`QuestionAnswered` events; a shared `_PausableClock` freezing both the
  displayed `MM:SS` and `StageTracker`'s recorded timings while the overlay is open; Phase 2's
  per-row cursor placement extracted to `_build_cursor_rows` and reused; `harness/input.py`
  gained an idempotent `restore_terminal()`. `_read_answer(renderer, prompt)` now branches on
  `sys.stdin.isatty()` — non-TTY keeps the old `input()` bridge byte-for-byte, TTY runs
  `read_keys()` on a daemon thread forwarding through an `asyncio.Queue`. 619 tests pass; all
  four gates clean; source diff 397 lines against a ~350-500 budget.
- Learned: (1) The three time concepts are NOT interchangeable and only two may pause — wall
  clock (`asyncio.timeout`) never, displayed `MM:SS` and recorded stage timings both. (2) The
  wall clock's real failure mode is STRUCTURAL: a key loop that blocks the event loop thread
  stops the timeout from firing at all, which no amount of "don't call reschedule" discipline
  prevents. The async side must only ever `await`. (3) Green tests asserting a risk's symptom
  can leave the risk itself unpinned — see the `## Discoveries` entry; the replacement test was
  verified to fail against a deliberately blocking implementation before being trusted. (4) The
  review caught a 7th `_read_answer` call site the implementor missed, passing only because the
  non-TTY branch ignores `renderer` and `prompt` defaults to the same string; mypy does not
  check untyped test bodies, so no gate would ever have caught it.
- Drift: YES — 2026-08-20 entry in `## Reconciliations`: `harness/input.py` added to the phase's
  file list, developer-approved, so terminal restore is structural rather than dependent on a
  daemon thread's unrun `finally`.
- Watch-next: Phase 6 is flagged (!#3) and now owns THREE deliverables, not one — reader strip,
  the structured tool-call log absorbed from Phase 3, AND live-updating task meta (Phase 3's
  logged limitation). It is the plan's only `harness/agent.py` touch: mirror
  `_ReaderDigestMiddleware`'s existing `awrap_tool_call` shape, do not invent a new one, and keep
  reader dispatch/retry/failure semantics untouched. Its tests are concurrency-shaped like Phase
  5's — hold them to the same "verified to fail against the broken shape" standard. The manual
  WezTerm acceptance criteria for Phases 2, 3 and 5 are all still unticked and need a real
  terminal.
