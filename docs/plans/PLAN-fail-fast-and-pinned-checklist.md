# PLAN: Fail-Fast Runs and Pinned Rich Checklist

**Status:** In Progress
**Created:** 2026-08-14
**Type:** Single plan

## Intent

**True goal:** A run that cannot do its job (SearXNG down, hard error, out of time with no
answer) fails loudly and early instead of burning its token budget and emitting a garbage
report; while a run is in progress, the terminal shows a pinned checklist of the agent's
tasks that stays put as events stream beneath it.

**Binding outcomes:**
- **R1** — Before the agent loop starts, the configured SearXNG endpoint is health-checked
  over HTTP; unreachable or bad status → clear error message, nonzero exit, no run started,
  no report written.
- **R2** — N consecutive connection-level failures of the search tool mid-run abort the run
  as a hard error (default N=3).
  - Fetch-tool failures (bot blocks, 403s, timeouts on target sites) do NOT count — those
    remain best-effort + disclose, per the existing invariant.
- **R3** — A failed run writes no report file: hard errors (including R1/R2 aborts) and a
  wall-clock expiry that fires before a final synthesis exists produce only an error message
  and a nonzero exit. Nothing appears in reports/; nothing is promised about the workspace.
  - Wall-clock expiry AFTER a final synthesis exists is NOT a failure: the report is written
    with a "cut short" disclosure, same as the round cap.
- **R4** — The round/tool-call cap is raised to 50; hitting it remains a reported,
  disclosed outcome ("run cut short"), never a failure.
- **R5** — The run renders as a full-screen TUI (alternate screen buffer): the agent's todo
  list is a checklist pinned at the top, items visibly checking off as they complete, with
  the event log in a fixed panel beneath and a footer carrying exactly one exit
  instruction ("Ctrl+C to exit").
  - Before the first todo event the pinned area is empty or a placeholder; if the agent
    rewrites its list, the pinned region re-renders the latest version.
  - When stdout is not a real terminal (piped/redirected), fall back to plain sequential
    printing.
  - The alternate screen vanishes on exit, so after every run (success or failure) a
    compact post-run summary — outcome, and report path or error — is printed to the
    normal terminal.
  - Ctrl+C mid-run is a user abort: hard error semantics (no report), clean exit from the
    alternate screen.
  - Agent clarifying questions (`ask_user`) suspend the alternate screen for the prompt
    and restore it after.
- **R6** — Visuals: blue theme; pending-status text in `#207d99`; a gray horizontal rule
  separates the pinned checklist from the scrolling event log beneath it.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- Checklist styling resembles the pasted screenshot: box-style checkboxes under a bold
  heading, indented items.
- The R1 error message tells the user to start the SearXNG container.

**Non-goals:**
- No Docker awareness in the harness — the health check is HTTP against the configured
  endpoint (the same one the compose file exposes); the compose file is never read at
  runtime.
- No partial/stub "failure report" files in reports/.
- No retry/wait loop for SearXNG at startup or mid-run — down is down.
- No interactive Resolved/Abort prompt on mid-run search failure (considered and dropped:
  SearXNG rarely self-heals, and pausing holds a live agent loop open while the wall clock
  question gets murky).
- No Textual (or other TUI framework) dependency — the TUI is built with the already-pinned
  Rich; no in-app log scrolling, no key handling beyond Ctrl+C.

**Constraints & assumptions:**
- SearXNG URL comes from `harness.toml` config only — never hardcoded (repo invariant).
- The harness UI runs on the same machine as SearXNG (now and planned), so a localhost
  HTTP check is representative.
- R3 narrows the repo's "best-effort + disclose" invariant (wall-clock-without-answer was
  previously a disclosed outcome); CLAUDE.md/docs must be updated to match.
- Must render sanely in Windows Terminal and over SSH.

**Open questions:**
- none (the round-cap-location question was resolved in design — see D5).

## Codebase Map
- Entry point: `harness/__main__.py:main` — argv → `load_config()` → `preflight(config, "head")` →
  `build_renderer()`/`StageTracker` → `build_agent()` → agent `astream` loop under
  `asyncio.timeout` → `write_report(outcome, config)` called UNCONDITIONALLY in a
  `try/finally`; exit code `1 if cut_short == "error" else 0`.
- Outcome representation: `cut_short: CutShortReason | None` where
  `CutShortReason = Literal["round_cap", "wall_clock", "error"]` (`harness/report.py`);
  set in three `except` clauses in `__main__.py` (`TimeoutError`+`clock.expired()`,
  `GraphRecursionError`, generic `Exception`). `cut_short` is decided BEFORE the final
  answer is extracted from messages.
- Todos: arrive as `node_update["todos"]` (list of `{"content", "status"}` dicts) from
  deepagents' `TodoListMiddleware`; `__main__.py` currently flattens them into
  `Activity(f"[{status}] {content}")` text events. No structured todo display event exists.
- Display: `harness/display.py` — `Renderer` protocol (`emit`, `suspend`, `close`);
  `build_renderer()` TTY-dispatches to `RichRenderer` / `PlainRenderer`. Events:
  `StageStarted`, `StageCompleted`, `Activity`, `Question`, `RunFinished`. `RichRenderer`
  uses one `rich.live.Live` (`transient=True`) holding a spinner + last-8-activity tail;
  `suspend()` tears down and restarts `Live` around `ask_user` prompts.
- Search tool: `build_search_tool(config) -> BaseTool` in `harness/tools/search.py` —
  NEVER raises; failures become `SearchFailure(reason: Literal["unreachable", "bad_status",
  "malformed"], detail)` returned as the tool artifact (`response_format="content_and_artifact"`).
- Config: `AgentSettings.max_rounds: int = Field(default=20, gt=0)`,
  `wall_clock_seconds: int = Field(default=1800, gt=0)`, `SearchSettings.base_url: str`
  (`harness/config.py`); TOML keys `[agent] max_rounds` / `wall_clock_seconds`,
  `[search] base_url` (all present in `harness.toml`). Recursion limit is
  `max_rounds * 2 + 1` in `__main__.py`.
- Failure convention: config/model errors print `f"error: {exc}"` to stderr and `return 1`
  before any agent work (`harness/__main__.py`) — the pattern R1 extends. Model preflight
  exemplar: `async def preflight(config, role) -> None` raising `ModelError`
  (`harness/models.py`).
- Tests: pytest, offline/fixture-based. `tests/test_display.py` (Console over StringIO,
  `force_terminal=True, width=80`, ANSI stripped for assertions), `tests/test_report.py`,
  `tests/test_agent.py`. `tests/conftest.py` fixtures: `ScriptedChatModel`,
  `patch_run(monkeypatch, config, model, skip_preflight=...)` for full `main()` tests,
  `install_search_transport(monkeypatch, handler)` (swaps `harness.tools.search`'s
  `httpx.AsyncClient` for `MockTransport`), `make_config` (tmp_path-rooted config).
- Commands: `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` /
  `uv run mypy .` (CI adds a 90% coverage floor on `harness/`).

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- No bottom-pinned or non-fullscreen "Live block" layout — both were considered and
  superseded by the full-screen TUI decision (D1).
- No editing of `PLAN-rich-cli-output.md`'s content beyond its stale Status line (Phase 5
  housekeeping); its display shape is superseded by this plan, not rewritten.

## Design Decisions
### D1: Display architecture for the pinned checklist
- **Chosen:** Full-screen Rich TUI — `Live(screen=True)` on the alternate buffer with a
  layout of checklist panel (top, pinned), gray rule, fixed-height event-log panel (last N
  lines), footer with the single exit hint "Ctrl+C to exit". Todos flow via a new
  structured display event instead of flattened `Activity` text.
- **Rejected:** Textual app — buys in-app log scrolling and `q`-to-quit at the cost of a
  new pinned dependency and rewriting display + run-loop integration as a hosted async app
  (~2x diff). Single Live block (checklist + tail, no alt screen) — initially chosen, then
  superseded by the developer's explicit call for a real TUI. Bottom-pinned checklist —
  Rich-idiomatic but visually inverted from the requirement.
- **Consequences:** No key handling (Ctrl+C is the only control); log history beyond the
  panel is not recoverable on screen, so the post-run summary printed to the normal
  terminal after leaving the alternate screen is the only visible forensic trail (besides
  the workspace). `suspend()` must exit/re-enter the alternate screen around `ask_user`.

### D2: Failure taxonomy and report gating
- **Chosen:** Hard errors (exceptions, R1/R2 aborts, Ctrl+C user abort) and wall-clock
  expiry with no final answer → no report file, stderr error, exit 1. Round cap, and
  wall-clock expiry after a final answer exists → report written with the existing
  "cut short" disclosure. Requires extracting the final answer BEFORE the report decision
  (today `cut_short` is decided first and `write_report` is unconditional).
- **Rejected:** "any cap = failure" — discards a completed synthesis over a timer;
  "hard errors only" (keep wall-clock reporting always) — developer explicitly wants
  answer-less wall-clock runs to produce nothing.
- **Consequences:** Narrows the repo's "best-effort + disclose" invariant; CLAUDE.md and
  docs must be reworded (Phase 5). Exit code becomes 1 for every no-report outcome.

### D3: Mid-run SearXNG guard mechanism
- **Chosen:** A consecutive-failure counter inside the search tool's closure: each
  connection-level failure (`SearchFailure.reason` in `{"unreachable", "bad_status"}`)
  increments, any success resets, and on reaching the configured limit the tool RAISES a
  dedicated exception. The agent loop's existing generic exception handling maps it to a
  hard error — no agent-loop changes, preserving the tools-don't-touch-the-loop invariant.
- **Rejected:** Inspecting `ToolMessage` artifacts per round in `__main__.py` — threads
  tool-specific knowledge through the loop, exactly what the invariant forbids, and is
  more fragile against stream-shape changes. Interactive Resolved/Abort prompt — dropped
  in intent.
- **Consequences:** `"malformed"` failures do not count toward the abort (they indicate a
  SearXNG response, not an outage). The counter is per-tool-instance, i.e. per run.

### D4: Startup check target
- **Chosen:** HTTP GET against the configured `[search] base_url` requesting a JSON search
  (`/search?q=ping&format=json`), asserting 200 + parseable JSON — this catches container
  down AND the documented "stock container is HTML-only" misconfiguration in one probe.
- **Rejected:** Docker container inspection — hardcodes a deployment detail, fails for
  remote/un-containerized SearXNG, adds docker as a runtime assumption.
- **Consequences:** The check shares the search tool's HTTP path, so
  `install_search_transport` tests it offline for free.

### D5: Where the round-cap value lives
- **Chosen:** Both — `AgentSettings.max_rounds` default becomes 50 AND `harness.toml`'s
  `max_rounds` is set to 50, per the project convention that config is explicit.
- **Rejected:** TOML-only (leaves a stale code default) or code-only (leaves the checked-in
  config overriding it back to 20).
- **Consequences:** none beyond a two-line diff.

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | SearXNG startup health check, fail fast | Phase 2 |
| R2 | Mid-run abort after N consecutive search failures | Phase 3 |
| R3 | Failed run writes no report | Phase 4 |
| R4 | Round cap raised to 50, still disclosed | Phase 2 |
| R5 | Full-screen TUI with pinned checklist | Phase 1 |
| R6 | Blue theme, #207d99 pending, gray rule | Phase 1 |

## Progress
- [x] Phase 1: Full-screen TUI renderer
- [x] Phase 2: Startup search preflight + cap raise
- [x] Phase 3: Consecutive-search-failure abort
- [x] Phase 4: Report gating and abort semantics
- [x] Phase 5: Docs reconciliation
- [ ] Final verification

## Phases

### Phase 1: Full-screen TUI renderer
**Risk:** flagged (!#1, !#3)
**Test-first:** required
**Goal:** Replace `RichRenderer`'s transient single-region Live with a full-screen
alternate-buffer TUI — pinned checklist top, gray rule, fixed event-log panel, exit-hint
footer — fed by a new structured todos display event.
**Requirements:** R5, R6
**Files:**
- `harness/display.py` — new `TodosUpdated` event; `RichRenderer` rework to
  `Live(screen=True)` + layout; blue theme, `#207d99` pending style, gray rule, footer;
  `suspend()` exits/re-enters the alternate screen; post-run summary printed to the normal
  terminal on `RunFinished`; `PlainRenderer` handles `TodosUpdated` as plain lines.
- `harness/__main__.py` — emit `TodosUpdated(todos)` from `node_update["todos"]` instead of
  flattening todos into `Activity` text.
- `tests/test_display.py` — updated/new renderer tests.
**Diff budget:** ~250-400 lines across 3 files

**Reuse:**
- Extend the existing `DisplayEvent` dataclasses and `Renderer` protocol in
  `harness/display.py` — do NOT create a parallel event system.
- Pattern to mirror: `tests/test_display.py`'s Console-over-StringIO + ANSI-strip
  assertion style.

**Contracts:**
- `TodosUpdated` display event carrying the ordered todo list, each item with `content: str`
  and `status: str` (deepagents statuses: `pending` / `in_progress` / `completed`) — Phase 4
  reuses the renderer unchanged; nothing else may re-flatten todos into `Activity`.
- On `RunFinished`, after leaving the alternate screen, the renderer prints a compact
  post-run summary (outcome + report path or error) to the normal terminal — Phase 4
  assumes this is where its no-report error message becomes visible.

**Out of scope:**
- No changes to outcome semantics, `write_report`, or exit codes (Phase 4).
- No changes to `harness/report.py`, tools, or config.
- No key handling, in-app scrolling, or Textual.
- Do not restyle the report file's markdown — terminal only.

**Tests (write first, confirm red):**
- [x] `TodosUpdated` renders a checklist: completed items checked, pending items styled
      `#207d99`, in_progress visually distinct; a later `TodosUpdated` replaces the list.
- [x] Layout order: checklist above rule above log; footer contains the exit hint.
- [x] `suspend()` restores the TUI afterward and the pending `Question` prompt is readable
      (mirrors the existing suspend tests).
- [x] `RunFinished` produces a post-run summary on the normal screen.
- [x] `PlainRenderer` (non-TTY) prints todo updates sequentially — no alt-screen codes.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the display rework, then the `__main__.py` emission change.
3. Run the tests; confirm they PASS (green).
4. Manual smoke: `uv run python -m harness "test question"` in Windows Terminal — checklist
   pinned, log rolls beneath, Ctrl+C leaves a clean normal screen.

**Acceptance criteria:**
- [ ] Manual smoke above observed (pinned checklist, clean exit, summary line visible after
      exit). — NOT performed: no live SearXNG/model endpoints in the implement environment;
      deferred to the developer.
- [x] `uv run pytest tests/test_display.py` green.

### Phase 2: Startup search preflight + cap raise
**Risk:** none
**Test-first:** required
**Goal:** Health-check the configured SearXNG endpoint before any agent work, failing fast
in the existing config/model-error style; raise the round cap to 50.
**Requirements:** R1, R4
**Files:**
- `harness/tools/search.py` — new `preflight_search(config)` async function per D4.
- `harness/__main__.py` — call it alongside the existing model `preflight`, same
  error-print-and-exit-1 handling.
- `harness/config.py`, `harness.toml` — `max_rounds` default and value → 50 (D5).
- new tests beside the existing search-tool tests.
**Diff budget:** ~60-120 lines across 4 files

**Reuse:**
- Pattern to mirror: `preflight` in `harness/models.py` (fail-fast, typed error) and the
  `error: {exc}` + `return 1` pre-run handling in `harness/__main__.py`.
- Tests reuse `install_search_transport` from `tests/conftest.py` — do NOT build a new HTTP
  mock.

**Out of scope:**
- No retry/wait loop; one probe, pass or fail.
- No changes to the search tool's runtime behavior (Phase 3).
- No docker/compose awareness.

**Tests (write first, confirm red):**
- [x] Preflight passes on a 200 JSON response; fails (typed error) on connection error,
      non-200, and non-JSON (HTML-only container) responses.
- [x] `main()` with SearXNG down exits 1, prints an error mentioning SearXNG/the container,
      starts no run, and writes nothing to the reports dir (via `patch_run`).
- [x] Config default `max_rounds == 50`.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `preflight_search`, wire it into `main()`, bump both cap locations.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] With the SearXNG container stopped, `uv run python -m harness "q"` exits nonzero
      within seconds, no report file created. — NOT performed live (no endpoints in the
      implement environment); the offline `main()`-level test covers the same path.
      Verify at final verification.

### Phase 3: Consecutive-search-failure abort
**Risk:** none
**Test-first:** required
**Goal:** The search tool raises after N consecutive connection-level failures, aborting
the run as a hard error, per D3.
**Requirements:** R2
**Files:**
- `harness/tools/search.py` — failure counter in the tool closure; new
  `SearchUnavailableError`; count `unreachable`/`bad_status`, reset on success, ignore
  `malformed`.
- `harness/config.py`, `harness.toml` — `[search] max_consecutive_failures` (default 3).
- new tests beside the existing search-tool tests.
**Diff budget:** ~80-150 lines across 4 files

**Reuse:**
- Extend `build_search_tool` in `harness/tools/search.py` — the counter lives in its
  closure; do NOT wrap the tool from outside or touch `harness/agent.py`.
- Tests reuse `install_search_transport`.

**Contracts:**
- `SearchUnavailableError(Exception)` importable from `harness.tools.search`, message names
  SearXNG and the failure count — Phase 4 relies on it reaching the loop's generic
  exception handler (hard error, no report) without special-casing.

**Out of scope:**
- No changes to `harness/agent.py` or the astream loop.
- Fetch-tool failures remain untouched (best-effort + disclose).
- No report-gating logic (Phase 4).

**Tests (write first, confirm red):**
- [x] N consecutive `unreachable`/`bad_status` results raise `SearchUnavailableError`; a
      success in between resets the counter; `malformed` neither counts nor resets.
- [x] Limit comes from config (non-default value honored).

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement counter + config key.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Full-`main()` test: scripted model issuing search calls against a down transport ends
      the run with exit 1 and no report file (may land with Phase 4's gating tests if
      sequencing is cleaner — note it in the handoff either way).

### Phase 4: Report gating and abort semantics
**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** Decide report-vs-no-report per D2: extract the final answer before the outcome
decision; skip `write_report` for hard errors, user abort (Ctrl+C), and answer-less
wall-clock expiry; keep disclosed reports for round cap and post-answer wall-clock.
**Requirements:** R3
**Assumes:**
- Phase 1's post-run summary path exists (the no-report error message surfaces there).
**Files:**
- `harness/__main__.py` — reorder answer extraction ahead of the report decision; gate the
  currently unconditional `write_report` call; handle `KeyboardInterrupt` as user abort
  (exit 1, no report, clean TUI exit); exit 1 on every no-report outcome.
- `tests/test_report.py` and/or a `main()`-level test module — gating tests via `patch_run`.
**Diff budget:** ~100-180 lines across 2-3 files

**Reuse:**
- `patch_run` + `ScriptedChatModel` from `tests/conftest.py` for full-run outcome tests.
- The existing `cut_short` disclosure rendering in `harness/report.py` — unchanged for the
  still-reported outcomes.

**Out of scope:**
- No changes to `harness/report.py` rendering or `RunOutcome`'s shape beyond what gating
  strictly needs.
- No workspace cleanup/preservation promises.
- No new outcome kinds beyond mapping Ctrl+C onto the existing hard-error path.

**Tests (write first, confirm red):**
- [x] Outcome table: hard error → no file in the reports dir, exit 1; wall clock without
      answer → no file, exit 1; wall clock with answer → file with disclosure, exit 0;
      round cap → file with disclosure, exit 0; clean finish → file, exit 0.
- [x] `KeyboardInterrupt` mid-stream → no file, exit 1.
- [x] The no-report paths still emit `RunFinished` so the summary shows the error.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Reorder answer extraction, gate `write_report`, add `KeyboardInterrupt` handling.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Live check: stop SearXNG mid-run — run aborts, normal terminal shows the error
      summary, the reports dir gained no file. — NOT performed live (no endpoints in
      the implement environment); the offline full-main() abort test covers the same
      path. Verify at final verification.

### Phase 5: Docs reconciliation
**Risk:** none
**Test-first:** N/A — documentation-only phase
**Goal:** Bring the written invariants in line with D2 and record the decisions.
**Requirements:** none (consequence of R3/R4)
**Files:**
- `CLAUDE.md` — reword the "best-effort + disclose" invariant to scope it to degraded
  coverage within a run that finishes; failed runs (hard error, answer-less wall clock)
  produce no report.
- `docs/INDEX.md` — status line reflects fail-fast behavior and the TUI.
- `docs/decisions.md` — entries for D1-D4 (one-liners referencing this plan).
- `docs/plans/PLAN-rich-cli-output.md` — Status line only: mark Complete/Superseded (its
  work merged in PR #12; display shape now owned by this plan).
**Diff budget:** ~30-60 lines across 4 files

**Reuse:**
- Follow `docs/decisions.md`'s existing entry format.

**Out of scope:**
- No content rewrites of prior plans; Status line only.
- No new guide documents.

**Manual verification:**
- [x] `grep -n "best-effort" CLAUDE.md docs/INDEX.md` — reworded text present, old absolute
      phrasing gone.

**Steps:**
1. Apply the four doc edits.
2. Run the manual verification.

**Acceptance criteria:**
- [x] A reader of CLAUDE.md alone would correctly predict that a SearXNG-down run writes
      no report.

## Verification
- [ ] `uv run pytest` (CI also enforces the 90% coverage floor on `harness/`)
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] End-to-end: container stopped → immediate nonzero exit, no report; container up →
      TUI run with pinned checklist, report written on clean finish.

## Notes
- Reader-delegation work (PLAN-reader-delegation.md) merged into this branch AFTER the
  exploration pass — `harness/agent.py`, `harness/__main__.py`, and `harness/report.py`
  locations cited in the Codebase Map may have shifted; re-verify before editing (shapes
  were confirmed, positions may drift).
- The search preflight probes the JSON API specifically (D4) because a stock SearXNG
  container serves HTML only — a bare 200 on `/` is not proof the tool will work.
- N (consecutive-failure limit) default is 3 via `[search] max_consecutive_failures`.

## Risks
#1. **Alternate-screen lifecycle around prompts and exit is the fragile core of Phase 1** —
    `Live(screen=True)` must be suspended/re-entered for `ask_user` input and torn down
    cleanly on success, hard error, and Ctrl+C, in Windows Terminal and over SSH. The
    current `suspend()` already tears down/restarts a transient Live, but the alternate
    buffer adds terminal-state restoration; a crash that skips teardown leaves the user's
    terminal stuck on the alt screen. Confirm Rich restores state via its context
    management on exceptions; keep teardown in a `finally`.
#2. **"Answer exists at wall-clock expiry" depends on what the interrupted stream left
    behind** — the timeout can fire mid-message, and answer extraction from accumulated
    messages may see a partial or empty synthesis. The gate must treat "extractor returned
    empty/whitespace" as no-answer (no report) rather than crash, and the Phase 4 outcome
    table must include a wall-clock-with-partial-messages case.
#3. **Concurrent merges may have moved the display/main seams** — reader-delegation landed
    mid-planning (see Notes); if `__main__.py`'s todo flattening or event emission changed,
    Phase 1's emission edit targets the new location, and any mismatch with the Codebase
    Map is a reconciliation, not a license to improvise.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

### 2026-08-14 — Ctrl+C inside a model/tool call escapes as CancelledError (deferred)
Phase 4's KeyboardInterrupt handling covers a Ctrl+C that raises in `main()`'s own
`async for` loop, but a Ctrl+C landing INSIDE a langgraph node (model or tool call —
arguably the likeliest moment) surfaces at `main()`'s boundary as
`asyncio.CancelledError`, which neither `except Exception` nor `except
KeyboardInterrupt` catches — a raw traceback would escape. The same window exists
around `verify_paragraphs` (outside the guarded try). Handling `CancelledError` is a
design decision not covered by this plan (it also cancels legitimate task
cancellation), so DEFERRED to a follow-up decision rather than patched here.
Disposition: defer (blanket-approval session; developer to confirm).

## Phase Handoff Log

### 2026-08-14 — Phase 1: Full-screen TUI renderer
- Done: `TodoItem`/`TodosUpdated` events; `RichRenderer` reworked to `Live(screen=True)`
  full-screen TUI (pinned checklist, gray rule, in-frame stage timeline, footer);
  `__main__.py` emits `TodosUpdated` instead of flattening todos; post-run summary prints
  to the normal terminal on `RunFinished`.
- Learned: (1) under `screen=True`, any `console.print` while the Live runs is discarded
  with the alt buffer — Question prints are held and emitted inside `suspend()` (3F
  Blocker fix), and stage-completion lines render inside the frame (3F Major fix). (2)
  Test consoles need `legacy_windows=False, color_system="truecolor", _environ={}` or
  Windows detection / ambient NO_COLOR silently strips the escapes under test.
- Drift: none.
- Watch-next: the manual smoke (real-terminal run: pinned checklist, clean Ctrl+C exit,
  summary after exit, readable ask_user prompt) was NOT performed — no live endpoints in
  the implement environment. Verify interactively before or at final verification.

### 2026-08-14 — Phase 2: Startup search preflight + cap raise
- Done: `preflight_search` (D4 JSON probe) + `SearchPreflightError` in
  harness/tools/search.py; third preflight block in `__main__.py`; `max_rounds` 50 in
  both config.py and harness.toml.
- Learned: `patch_run` now neutralizes the search preflight BY DEFAULT
  (`run_search_preflight=True` opts into the real probe against the mock transport) —
  the natural-seeming "tie it to skip_preflight" mechanism would have broken every
  default-path main() test.
- Drift: none (impl-plan-level mechanism correction only, documented in conftest).
- Watch-next: the live container-stopped acceptance check is deferred to final
  verification along with Phase 1's manual smoke.

### 2026-08-14 — Phase 3: Consecutive-search-failure abort
- Done: `SearchUnavailableError` + closure-local counter in `build_search_tool`
  (unreachable/bad_status count, success resets, malformed ignored); `[search]
  max_consecutive_failures` (default 3) in config.py + harness.toml.
- Learned: the 3F reviewer flags that langgraph's ToolNode may CATCH tool exceptions
  and return them as error ToolMessages instead of propagating — D3's "reaches the
  loop's generic exception handler" is UNVERIFIED until Phase 4's full-main() test.
- Drift: none.
- Watch-next: Phase 4's scripted-model down-transport main() test must observe exit 1
  and no report — if the exception is swallowed by ToolNode, that is a Drift
  Reconciliation on D3, not a test to weaken. The phase's own full-main() acceptance
  criterion was deferred to Phase 4 per the plan's note.

### 2026-08-14 — Phase 4: Report gating and abort semantics
- Done: D2 gate around `write_report` (`should_write_report`; exit 1 on every
  no-report outcome); `except KeyboardInterrupt` mapping to the hard-error path; full
  outcome-table tests plus the deferred Phase 3 search-abort main() test.
- Learned: D3 CONFIRMED empirically — deepagents/langgraph's default tool handling
  re-raises non-ToolInvocationError exceptions, so `SearchUnavailableError` reaches
  `main()`'s generic handler. Also: Ctrl+C inside a node surfaces as CancelledError
  (see `## Discoveries`, deferred).
- Drift: none.
- Watch-next: Phase 5 is docs-only; the two live checks (Phase 1 TUI smoke, mid-run
  container stop) remain for final verification.

### 2026-08-15 — Phase 5: Docs reconciliation
- Done: CLAUDE.md invariant scoped to runs that finish; INDEX.md status covers
  fail-fast + TUI; D1-D4 entries in docs/decisions.md; PLAN-rich-cli-output.md marked
  Complete/superseded.
- Learned: nothing new — docs matched shipped code on reviewer cross-check.
- Drift: none.
- Watch-next: final verification — quality gates offline, plus the LIVE checks only the
  developer can run (TUI smoke in a real terminal, container stopped at startup, and
  container stopped mid-run).
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->
