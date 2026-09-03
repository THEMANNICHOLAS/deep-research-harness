# PLAN: Interactive Lead Chat

**Status:** In Progress
**Created:** 2026-08-25
**Type:** Single plan

## Intent

**True goal:** Turn the one-shot research run into a live chat session with the lead agent —
the developer steers while researchers work in the background, and discusses the finished
report afterwards. A single-user tool run on the developer's local laptop.

**Binding outcomes:**
- **R1** — The session stays interactive throughout the run: a message typed at any time is
  queued and delivered on the lead's next turn — never lost, never interrupting a model call.
- **R2** — Each researcher return enters the lead's context as a message carrying the findings
  plus a roster (which of the lead's spawned researchers have finished, which remain); the lead
  narrates the return in chat in its own prose.
  - All pending returns and any queued user message drain into ONE lead turn, in arrival order.
- **R3** — Mid-run, a user message may ask a question (answered from what the lead already has)
  or redirect the final scope of the report; the lead keeps its tools throughout, including
  reading captured sources so it can quote exactly what a source says.
- **R4** — The lead can still ask the user a clarifying question mid-session, offering up to
  four answer choices, and continues once answered.
- **R5** — The report is written only when the lead decides research is done (or a cap fires),
  as today. After the report exists, chat continues over the same sources; no new research.
  - Quit before a report exists = failed run (no report, nonzero exit); quit after = clean exit,
    report stays.
- **R6** — The wall clock spans first question → report written only; post-report chat is
  unclocked. Round cap semantics are unchanged.
- **R7** — Slash commands: `/sources` lists every source captured this session with status;
  `/model` switches a role mid-session and the new model receives the full existing context;
  `/new` stops the wall clock, terminates running subagents and tool calls, discards the
  context, and returns to a fresh question screen.
- **R8** — A live roster of the lead's researchers (id, label, status, elapsed) is viewable in
  the TUI; the nested reader tier is not shown.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- `/budget` (rounds used, wall clock remaining) and `/report` (force the report now) commands.
- Visual shape follows the HTML mock: user turns as left-accented quotes, agent turns with a
  `research head <model>` byline, one-line tool calls with result summaries, a persistent task
  dock above the composer, a `/` command menu, sources counter top-right.
- Session crumb: first five words of the opening question.

**Non-goals:**
- Post-report research, a revised or second report, or reopening the loop from chat.
- A non-TTY / one-shot mode — chat is the only mode; `python -m harness "<question>"` piped
  or scripted is dropped.
- Reader-tier (nested subagent) visibility in the roster.
- Interrupting or cancelling the lead's in-flight model call from the keyboard.
- Multi-user or remote (homelab) deployment.

**Constraints & assumptions:**
- Runs on the developer's local laptop for now; the homelab deployment is a future step.
  CLAUDE.md and docs/INDEX.md currently say "homelab Linux machine over SSH" and must be
  updated to say local laptop as part of this work.
- Model roles stay config-declared (`harness.toml` `[roles.*]`), never hardcoded; `/model`
  switches among configured values.
- Today's fail-fast rules (SearXNG preflight, browser preflight, consecutive search failures)
  are kept as-is.

**Open questions:**
- ~~Whether deepagents' `task` tool can yield a per-researcher return message at all.~~ Resolved
  in exploration: it cannot (see D1). The lead-turn-per-return model gets its own dispatch tool.

## Background

- deepagents 0.7.5 `task` (`deepagents/middleware/subagents.py:542-596`) `ainvoke`s the
  subagent graph to completion inside the tool call and returns a `Command` whose `ToolMessage`
  content is the subagent's last non-empty `AIMessage` text. N `task` calls in one `AIMessage`
  are `asyncio.gather`ed by `langgraph/prebuilt/tool_node.py:828-860` and the node completes
  only when all finish. deepagents has no in-process background mode (its
  `AsyncSubAgentMiddleware` requires a remote LangGraph Platform server).
- Agent state is a `messages` channel (reducer `add_messages`) persisted per `thread_id` in the
  `InMemorySaver`; `agent.aget_state(config).values["messages"]` reads it and
  `agent.aupdate_state(config, {"messages": [...]})` appends. Passing
  `{"messages": [HumanMessage(...)]}` as the next `astream` input appends before the model turn.
- `SubAgent` is a TypedDict (`subagents.py:36-165`) with `name, description, system_prompt,
  tools, model, middleware, interrupt_on, ...`; its `model` is resolved once at graph compile.
- Whether cancelling the lead's `astream` task propagates into `task`-run subagents is not
  documented anywhere in deepagents/langgraph — one reason the harness owns dispatch (D1).

## Codebase Map

- Entry points: `harness/__main__.py` — `main(argv)` (line 619): argparse, `_run_welcome` (524,
  raw-key welcome screen with a pre-run `/model` picker `_handle_model` 467), `build_agent`
  (838), one `thread_id` (847), the `astream` loop (917-1079) under `asyncio.timeout(None)`
  with the wall clock armed at the first `task(subagent_type="researcher")` call (953), round
  cap via `_note_model_turns` (879), synthesis margin `_margin_reached` (152) → bounded second
  `astream` with `_SYNTHESIZE_NOW`, interrupt handling `_answer_questions` (402) / `_read_answer`
  (219-276: daemon thread `read_keys()` → `asyncio.Queue`), resume via
  `Command(resume={"decisions": [...]})` (1019), report gate (1153-1212): no report on
  `"error"` (includes Ctrl-C) or answer-less wall clock, exit 1; `asyncio.run(main())` (1216).
- `harness/agent.py` — `build_agent(config, registry, run_log, sink, browser) -> Runnable`
  (599) = `create_deep_agent(..., subagents=[researcher_spec], checkpointer=InMemorySaver())`
  (673); `_researcher_spec` (547) nests `_reader_spec` via a hand-built `SubAgentMiddleware`
  (590); `_INTERRUPT_ON = {ASK_USER_TOOL_NAME: InterruptOnConfig(allowed_decisions=["respond"])}`
  (78-80); `_task_dispatch_guard` (142) wraps `task` with `ToolErrorMiddleware`+`ToolRetryMiddleware`;
  `awrap_tool_call` middlewares on `task`: `_ReaderDispatchCapMiddleware` (200),
  `_ReaderDigestMiddleware` (280), `_ToolActivityMiddleware` (361, researcher/reader tiers only);
  `_middleware()` (700) installs the deepagents summarizer wrapper;
  `_register_no_shell_profile(model)` (453) registers `HarnessProfile(excluded_tools={"execute"},
  general_purpose_subagent disabled)` keyed `provider:model` — must be re-run per new model.
- `harness/activity.py` — `ActivitySink.readers()`/`.records()` (239/235) live per-reader
  dispatch/done state pushed via `on_change`; exemplar for a researcher roster.
- `harness/display.py` — `RichRenderer` (438): `rich.live.Live` alternate screen;
  `_build_renderable` (642) composes `_build_checklist` (502), `_build_reader_strip` (564),
  alerts, `sources: N` counter (627), `_build_tool_log` (538), footer. `PlainRenderer` (267).
- `harness/input.py` — `KeyEvent`, `LineBuffer` (pure multi-line editor), `decode_posix` /
  `decode_windows`, blocking `read_keys()` (POSIX termios 145-189, Windows msvcrt 138-144;
  `pragma: no cover`).
- `harness/models.py` — `build_chat_model(config, role) -> BaseChatModel` (21), `ModelError`
  (17), `async preflight(config, role)` (64).
- `harness/config.py` — `RoleConfig.choices: list[str] | None` (77, only `head` sets it —
  the `/model` picker list); `AgentSettings.max_rounds` (138), `wall_clock_seconds` (139),
  `synthesis_margin_seconds` (143); `run_workspace_dir(config, run_id)` (232).
- `harness/tools/__init__.py` — `ToolSets(lead, researcher, reader)` (20), `build_tools(config,
  registry, run_log=None, browser=None) -> ToolSets` (28); lead gets `[build_ask_user_tool]`.
- `harness/tools/ask_user.py` — `build_ask_user_tool(config)` (21), schema `AskUserInput(question:
  str)` only (28-36); body never runs (46) — the interrupt is the mechanism.
- `harness/sources.py` — `SourceRegistry.all() -> list[Source]` (322), `.count()` (326),
  `.link(id)` (333); `Source.id/url/title/read_mode` (220-228).
- `harness/report.py` — `RunOutcome` (74: question, answer, registry, usage, cut_short, todos,
  started_at, paragraphs, verification, incidents); `CutShortReason` Literal (42);
  `write_report(outcome, config) -> Path` (561) is the only write entry.
- `harness/runlog.py` — `RunLog.record(kind, detail)` / `.incidents()` (34-39).
- `harness/prompts/orchestrator.md` — lead prompt: dispatch via `task(subagent_type=
  "researcher")`, `write_todos` as the visible plan (34-40), `ask_user` pre-research only
  (21, 78-83), report/title rules under `# Output` (63-75).
- Tests: pytest + pytest-asyncio (`asyncio_mode = "auto"`), coverage floor 90% on `harness/`.
  `tests/conftest.py`: `ScriptedChatModel` (234, `ChatOpenAI` subclass with scripted replies),
  `ConcurrencyTrackingModel` (321), `patch_run()` (379-442: patches `load_config`, `preflight`,
  `preflight_search`, `BrowserSession.start` — the seam for end-to-end `main()` tests).
  `tests/test_agent.py` (3342 lines), `tests/test_display.py` (2372, `RichRenderer` on a
  `StringIO` console), `tests/test_main_welcome.py`, `tests/test_input.py`.
- Commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .` (all verified working after `uv sync --locked`).
- Comparable prior art: the `ask_user` interrupt round-trip (`__main__.py` 402/219-276) for
  keyboard-in-the-loop; `_ToolActivityMiddleware` + `ActivitySink` for live subagent state.

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- Deleting `PlainRenderer` or the non-TTY renderer path's tests — a backlog note, not this plan.
- Durable (on-disk) checkpointing of the conversation; `/model` reseeds in memory (D4).
- Changing the researcher→reader `task` nesting or the reader/researcher prompts beyond the
  return-shape lines D1 requires.
- A generic "background tool" abstraction — `dispatch_researcher` is the one call site.

## Design Decisions

### D1: Researcher dispatch — harness-owned async tool, not deepagents `task`
- **Chosen:** The lead loses `task` (`subagents=[]`); it gets `dispatch_researcher(label,
  objective, output_format, boundaries)`, which starts the compiled researcher graph as an
  `asyncio.Task` owned by the session and returns `"researcher/N (label) started"` at once.
  One call per researcher, so the lead may fire several in a turn and add more after a return.
  The researcher graph is compiled once per session in `harness/agent.py` from the same pieces
  `_researcher_spec` uses today (researcher model, `ToolSets.researcher`, `subagent.md`, the
  nested reader `SubAgentMiddleware`, `_task_dispatch_guard`, summarizer), via `create_deep_agent`
  with no checkpointer. A config cap `agent.max_researchers` makes the tool answer
  `"refused: N researchers already running — wait for a return"` instead of dispatching.
- **Rejected:** keep `task` and batch returns — `task` is atomic per tool node (Background), so
  no per-return narration or mid-fan-out steering, and cancellation propagation is unconfirmed.
  Python-owned `plan_research` fan-out — same loop, strictly less lead agency. deepagents'
  `AsyncSubAgentMiddleware` — needs a remote LangGraph Platform server.
- **Consequences:** two dispatch mechanisms coexist (lead → our tool, researcher → `task` for
  readers); the lead-tier wrappers `_task_dispatch_guard` gives `task` must be applied to the new
  tool by hand, and the lead tier now needs its own `_ToolActivityMiddleware`-style hook for the
  roster (R8). Wall-clock arming keys on `dispatch_researcher`, not `task`.

### D2: Turn scheduling — one lead turn per drained event batch
- **Chosen:** A new `harness/session.py` owns an `asyncio.Queue` of events (`ResearcherReturn`,
  `UserMessage`) plus the researcher task handles. Loop: await ≥1 event, drain all pending,
  build one `HumanMessage` (returns in arrival order — header, findings verbatim, then the user
  text — closed by one `Roster:` line), run `agent.astream({"messages": [msg]})` on the single
  `thread_id`, render, repeat. The first turn's input is the question. A turn that ends with
  researchers still running and an empty queue simply waits.
- **Rejected:** one turn per event — more head-model calls, roster stale by one event. Growing
  `__main__.py` in place — it is 1216 lines and the loop is a new long-lived state machine.
- **Consequences:** `__main__.py` shrinks to CLI + welcome + `Session` hand-off; round cap,
  wall clock, synthesis margin and exit gating move into the session; every later phase talks
  to `Session` through the Phase 1 contracts.

### D3: End of research — explicit `submit_report(answer)` tool
- **Chosen:** The lead calls `submit_report` when its roster is empty and it is satisfied.
  The session then stops the wall clock, runs verification + `write_report` exactly as today's
  end-of-run path, prints the report path into the transcript, and enters post-report chat:
  same thread, `dispatch_researcher` refuses (`"research is closed — the report is written"`),
  no clock. The synthesis-margin path injects "call submit_report now" instead of `_SYNTHESIZE_NOW`.
- **Rejected:** heuristic "roster empty + final-answer-shaped message" — indistinguishable from a
  narration turn.
- **Consequences:** `RunOutcome.answer` comes from the tool argument, not the last `AIMessage`;
  the round cap and answer-less wall clock still end the run as failures per R5/R6.

### D4: `/model` — rebuild the agent, reseed the thread from the checkpointer
- **Chosen:** `/model <role> <choice>` (choices from `RoleConfig.choices`) reads
  `aget_state(config).values["messages"]`, builds a fresh agent with the new model (re-running
  `_register_no_shell_profile`), and seeds a new `thread_id` with the same message list via
  `aupdate_state`. Researcher/reader role switches take effect for researchers dispatched after
  the switch (their graph is recompiled); running ones finish on the old model.
- **Rejected:** hot-swapping the model inside the compiled graph — deepagents resolves models at
  compile time (Background).
- **Consequences:** a switch mid-turn is applied after the current turn ends (queued like a
  user message). Evicted (summarized) history stays on disk in the run workspace and is
  untouched — the seeded list is whatever the checkpointer holds.

### D5: Keyboard — permanent composer on the existing daemon-thread key reader
- **Chosen:** Generalize `_read_answer`'s pattern: one daemon thread runs `read_keys()` for the
  whole session, pushing `KeyEvent`s into an `asyncio.Queue`; `LineBuffer` is the composer.
  Enter → `UserMessage` (or a slash command). While an `ask_user` interrupt is open the same
  composer answers it (digit 1-4 picks a choice; free text otherwise). Rich `Live(screen=True)`
  stays (decisions.md: Textual rejected).
- **Rejected:** adding Textual/prompt_toolkit — already ruled out in decisions.md; a new dependency
  for one widget.
- **Consequences:** `harness/input.py` gains nothing new in kind; the TUI redraw and the key
  thread contend for the terminal only through Rich's `Live`, so the composer is drawn as part
  of the renderable, never printed directly.

### D6: `/new` — cancel our own tasks, rebuild the run
- **Chosen:** `/new` cancels every running researcher `asyncio.Task` (awaiting their
  `CancelledError`), disarms the clock, drops the agent/thread, mints a new `run_id` +
  workspace, and returns to the welcome screen; the `BrowserSession` is reused.
- **Rejected:** exiting the process and relaunching — loses the warm browser and the TUI.
- **Consequences:** cancellation reaches the researcher graph as `CancelledError` inside its
  `ainvoke`; any half-written capture files stay in the abandoned run's workspace (isolated by
  `run_id`, so harmless).

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | queued user messages, next turn | Phase 3 |
| R2 | per-return message + roster, one turn per batch | Phase 1 (message), Phase 2 (drain + roster line) |
| R3 | ask/redirect mid-run, lead keeps tools | Phase 3 (prompt + delivery), Phase 1 (tools) |
| R4 | ask_user with ≤4 choices mid-session | Phase 4 |
| R5 | lead-decided report, post-report chat, quit semantics | Phase 1 (submit_report), Phase 2 (exits), Phase 3 (post-report chat) |
| R6 | wall clock = research only | Phase 2 |
| R7 | /sources /model /new | Phase 6 |
| R8 | live researcher roster in TUI | Phase 2 (data), Phase 5 (view) |

## Progress
- [x] Phase 1: Session tracer — dispatch tool, return injection, submit_report
- [x] Phase 2: Budgets, roster data and run exits inside the session
- [x] Phase 3: Composer — queued user messages and post-report chat
- [x] Phase 4: ask_user with choices
- [x] Phase 5: Chat TUI — transcript, task dock, researcher roster
- [x] Phase 6: Slash commands — /sources, /model, /new
- [x] Phase 7: Prompts, docs and supersession
- [ ] Final verification

## Phases

### Phase 1: Session tracer — dispatch tool, return injection, submit_report
**Risk:** flagged (!#1, !#5)
**Test-first:** required
**Goal:** A headless run (scripted models, no keyboard) where the lead dispatches researchers
through `dispatch_researcher`, receives each return as its own message, and ends with
`submit_report` producing today's report — proving D1/D2/D3 end to end before any TUI work.
**Requirements:** R2, R3, R5
**Assumes:**
- `create_deep_agent` can compile the researcher standalone with the nested reader
  `SubAgentMiddleware` and produce the same final-message text `task` delivers today.
**Files:**
- `harness/session.py` — new: `Session` (event queue, researcher task handles, turn loop, D2);
  new file per D2.
- `harness/tools/dispatch.py` — new: `build_dispatch_researcher_tool` + `build_submit_report_tool`
  (D1/D3); one module per tool is the registry convention.
- `harness/tools/__init__.py` — lead toolset gains both tools.
- `harness/agent.py` — `build_agent` takes `subagents=[]`; new `build_researcher_graph(...)`
  from `_researcher_spec`'s pieces; lead-tier wrapping of the new tool.
- `harness/__main__.py` — replace the `astream` loop with `Session.run(...)`; PlainRenderer
  output only in this phase.
- `harness/prompts/orchestrator.md` — dispatch/return/submit_report sections replace `task` text.
- `tests/test_session.py` — new; `tests/test_agent.py`, `tests/test_tools_registry.py` — modify.
**Diff budget:** ~500-750 lines across 8 files (≈250 of it `__main__.py` deletions)

**Reuse:**
- Extend `_researcher_spec` pieces in `harness/agent.py` — do NOT write a second researcher
  prompt or toolset; `_task_dispatch_guard` wraps the new tool.
- Pattern to mirror: `harness/tools/ask_user.py` (schema + factory shape, typed refusal strings);
  `tests/conftest.py` `patch_run` + `ScriptedChatModel` for the end-to-end test.

**Contracts:**
- Tool `dispatch_researcher(label: str, objective: str, output_format: str, boundaries: str)
  -> str`; returns `"researcher/{n} ({label}) started"`, or `"refused: ..."` (cap, D1; closed,
  D3). Name constant `DISPATCH_RESEARCHER_TOOL_NAME`.
- Tool `submit_report(answer: str) -> str`; name constant `SUBMIT_REPORT_TOOL_NAME`.
- `Session` events: `ResearcherReturn(id: str, label: str, findings: str, elapsed_s: float)`,
  `UserMessage(text: str)`; `Session.events: asyncio.Queue`.
- Return message text: first line `[researcher/{n} — {label}] returned:`, then findings verbatim;
  final line `Roster: done {ids} · running {ids}` (Phase 2 fills the roster; Phase 1 emits it).
- `async Session.run() -> RunOutcome | None` (None = failed run, no report).

**Out of scope:**
- Keyboard input, TUI changes, slash commands, wall clock/round cap relocation (Phase 2 —
  Phase 1 may leave the run unclocked).
- Touching `subagent.md`/`reader.md` beyond nothing; the researcher→reader `task` path.

**Tests (write first, confirm red):**
- [x] A lead turn calling `dispatch_researcher` twice ends the turn immediately with two
  "started" results and two running tasks.
- [x] A researcher completing produces exactly one `ResearcherReturn` and the next lead input
  is a `HumanMessage` matching the return-message contract; a second return while the first
  turn runs waits for the next turn.
- [x] `submit_report(answer)` ends research and `write_report` receives `RunOutcome.answer ==
  answer`; the session's `run()` returns the outcome.
- [x] Cap and closed refusals return the contract strings and dispatch nothing.
- [x] The compiled researcher graph carries the nested reader and `fetch_pages` only on the reader.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Build `dispatch.py` tools and `build_researcher_graph`; wire the lead toolset.
3. Build `Session.run` (D2 loop, D3 termination) and swap it into `__main__.py`.
4. Rewrite `orchestrator.md`'s dispatch/output sections for the new tools.
5. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] `uv run python -m harness "test question"` on a TTY with real models: transcript shows
  two "started" lines, then per-researcher return narration, then a report path.
- [x] `grep -n 'subagent_type' harness/__main__.py` → no matches.

### Phase 2: Budgets, roster data and run exits inside the session
**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** Wall clock, round cap, synthesis margin, Ctrl-C and the report gate all live in
`Session` with R5/R6 semantics, and the researcher roster is live data.
**Requirements:** R2, R5, R6, R8
**Files:**
- `harness/session.py` — clock armed at first successful dispatch, disarmed at `submit_report`;
  `_note_model_turns`/`_margin_reached` moved here; exit gating.
- `harness/activity.py` — `ResearcherState` + `ActivitySink.researchers()`, mirroring readers.
- `harness/agent.py` — lead-tier activity hook on `dispatch_researcher` start/finish.
- `harness/config.py`, `harness.toml` — `agent.max_researchers`.
- `harness/__main__.py` — exit codes from `Session.run()` result.
- `tests/test_session.py`, `tests/test_activity.py` (or existing sink tests) — modify.
**Diff budget:** ~250-400 lines across 7 files

**Reuse:**
- Move, don't rewrite: `_note_model_turns`, `_margin_reached`, `_SYNTHESIZE_NOW` handling and
  the report gate from `harness/__main__.py`.
- Pattern to mirror: `ActivitySink.readers()` / `_ToolActivityMiddleware` for researcher records.

**Contracts:**
- `ActivitySink.researchers() -> list[ResearcherState]` with `id, label, started_at,
  finished_at | None, status: Literal["running", "done", "failed"]`.
- Config key `[agent] max_researchers: int` (default 4).
- Synthesis-margin injection text names `submit_report` explicitly.

**Out of scope:**
- Any TUI rendering of the roster (Phase 5); user input (Phase 3).
- Changing `CutShortReason` values or report wording.

**Tests (write first, confirm red):**
- [x] Clock arms on the first successful dispatch (not on a refusal) and stops at
  `submit_report`; time passing after the report never cuts short.
- [x] Answer-less wall-clock expiry and Ctrl-C → `run()` returns None, no report file, exit 1;
  ~~wall-clock expiry after `submit_report` → report kept~~ (see Reconciliations, 2026-08-27).
- [x] Synthesis margin injects the submit_report instruction once; round cap still cuts short.
- [x] Roster records transition running → done/failed and the `Roster:` line lists them.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Relocate budget logic and exit gating into `Session`; add the config key.
3. Add researcher activity records and feed the roster line from them.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `test_main_welcome.py` and all prior `__main__` tests still pass or are moved, not deleted.

### Phase 3: Composer — queued user messages and post-report chat
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** The user can type at any time; text is queued and drained into the next lead turn,
and after the report the session keeps answering over the same sources until quit.
**Requirements:** R1, R3, R5
**Files:**
- `harness/session.py` — `UserMessage` events, post-report loop, quit handling.
- `harness/__main__.py` — session-long key thread (generalized from `_read_answer`), composer
  `LineBuffer`, Enter/quit keys.
- `harness/display.py` — composer line + minimal transcript of user/agent turns in
  `RichRenderer` (full redesign is Phase 5).
- `harness/prompts/orchestrator.md` — mid-run user messages, redirect rules, post-report mode.
- `tests/test_session.py`, `tests/test_display.py`, `tests/test_main_welcome.py` — modify.
**Diff budget:** ~300-450 lines across 6 files

**Reuse:**
- Extend `_read_answer`'s thread→queue bridge in `harness/__main__.py` — do NOT add a second
  key reader; `LineBuffer` from `harness/input.py` is the editor.
- Pattern to mirror: the welcome screen's key loop (`_run_welcome`) for key handling shape.

**Contracts:**
- Key thread posts `KeyEvent`s to one ~~`asyncio.Queue`~~ stdlib `queue.Queue` (see
  Reconciliations, 2026-08-27) for the whole session (welcome, run, interrupts, post-report
  all read from it).
- Quit: Ctrl-C/Ctrl-D before a report → failed run (Phase 2 gate); after → exit 0.

**Out of scope:**
- Slash commands (Phase 6) — a leading `/` is an ordinary message in this phase.
- Roster view, task dock, styling (Phase 5).

**Tests (write first, confirm red):**
- [x] Text entered while a turn runs is delivered in the next turn's `HumanMessage`, after any
  returns drained in the same batch, in arrival order; nothing is dropped across two turns.
- [x] Text entered while idle (roster empty, no report) starts a turn immediately.
- [x] After `submit_report`, a message starts a turn and `dispatch_researcher` refuses; quit then
  exits 0 with the report intact.
- [x] Composer renders the in-progress line inside the Live renderable.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Generalize the key thread; add composer state to the renderer.
3. Add user-message events and the post-report loop to `Session`; update the lead prompt.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Live: type "skip the routing angle" mid-fan-out; the lead's next turn acknowledges it.

### Phase 4: ask_user with choices
**Risk:** none
**Test-first:** required
**Goal:** The lead may ask a clarifying question at any point with up to four choices, answered
from the composer, and the run resumes.
**Requirements:** R4
**Files:**
- `harness/tools/ask_user.py` — `choices: list[str] | None` (max 4) on the schema.
- `harness/session.py` — interrupt detection inside a turn, resume via `Command(resume=...)`.
- `harness/display.py` — question + numbered choices rendered above the composer.
- `harness/prompts/orchestrator.md` — lift the pre-research-only restriction; when to offer choices.
- `tests/test_agent.py`, `tests/test_session.py`, `tests/test_display.py` — modify.
**Diff budget:** ~150-250 lines across 6 files

**Reuse:**
- Move `_answer_questions`' interrupt handling from `harness/__main__.py` into `Session`;
  `_INTERRUPT_ON` unchanged.
- Pattern to mirror: the existing overlay tests in `tests/test_display.py` (774-918).

**Contracts:**
- `AskUserInput(question: str, choices: list[str] | None)`; a digit 1-N answer resolves to the
  choice text, anything else is free text.

**Out of scope:**
- Multiple pending interrupts (the lead is one node — at most one).
- Researcher/reader tiers asking anything.

**Tests (write first, confirm red):**
- [x] Schema rejects >4 choices; a question with choices renders them numbered.
- [x] An interrupt mid-run pauses the turn; "2" resumes with the second choice's text; free text
  resumes verbatim; researchers keep running meanwhile.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Extend the schema, move interrupt handling into `Session`, render choices.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Live: the lead asks a clarifying question before dispatching; answering by digit proceeds.

### Phase 5: Chat TUI — transcript, task dock, researcher roster
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** `RichRenderer` shows the mock's layout: session bar with crumb and source counter,
scrolling transcript (user quotes, agent bylines, one-line tool calls), persistent todo dock,
researcher roster (id, label, status, elapsed), composer.
**Requirements:** R8
**Files:**
- `harness/display.py` — restructure `_build_renderable`; transcript model; roster panel from
  `ActivitySink.researchers()`; remove the reader strip from the lead view.
- `harness/session.py` — emit transcript events (user turn, agent text, tool call start/result).
- `tests/test_display.py` — modify.
**Diff budget:** ~400-600 lines across 3 files

**Reuse:**
- Extend `RichRenderer` — do NOT add a second renderer class; keep `_build_checklist`,
  the source counter and alert window as the dock/session-bar pieces.
- Pattern to mirror: `_build_reader_strip` for the roster rows; existing `StringIO` console tests.

**Contracts:**
- Renderer event API used by `Session`: `user_turn(text)`, `agent_text(text, model)`,
  `tool_call(name, arg_summary, result_summary | None)`, `report_written(path)`.

**Out of scope:**
- Reader-tier rows; the `/` command menu (Phase 6); colour/token tuning beyond the mock.
- Any change to what the lead is told.

**Tests (write first, confirm red):**
- [x] Renderable contains the transcript in order, the dock with todo count "N of M done", the
  roster with elapsed for a running researcher and a done marker for a finished one.
- [x] Transcript is bounded (oldest turns scroll out of the Live region) without losing the dock
  or composer.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Restructure the renderable; wire session events.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Live run visually matches the mock's regions (screenshot in the PR).

### Phase 6: Slash commands — /sources, /model, /new
**Risk:** flagged (!#4)
**Test-first:** required
**Goal:** The three binding commands work mid-run and post-report; `/model` carries the
conversation; `/new` tears the run down and returns to the welcome screen.
**Requirements:** R7
**Files:**
- `harness/session.py` — command parsing, `/sources`, `/model` (D4), `/new` (D6).
- `harness/agent.py` — `build_agent` reusable with a supplied message seed.
- `harness/display.py` — `/` menu listing the commands; local (non-model) reply turns.
- `harness/__main__.py` — welcome ↔ session loop for `/new`.
- `tests/test_session.py`, `tests/test_main_welcome.py` — modify.
**Diff budget:** ~300-450 lines across 5 files

**Reuse:**
- `SourceRegistry.all()` for `/sources`; `build_chat_model` + `_register_no_shell_profile` for
  `/model`; `RoleConfig.choices` and `_handle_model` for the picker — do NOT add a second
  choices list.
- Pattern to mirror: `_handle_model` in `harness/__main__.py` for the picker UX.

**Contracts:**
- Commands are handled in the session loop, never sent to the model; unknown `/x` prints a local
  reply listing commands.
- `/model` takes effect at the next turn boundary; `/new` cancels tasks, awaits them, then
  returns control to `_run_welcome`.

**Out of scope:**
- `/budget`, `/report` (preferences — only if trivially adjacent, else a backlog note).
- Persisting a switched model to `harness.toml`.

**Tests (write first, confirm red):**
- [x] `/sources` lists every registry entry with read_mode and never reaches the model.
- [x] `/model head <choice>` rebuilds the agent; the new thread's message list equals the old;
  the no-shell profile is registered for the new model; a queued user message is preserved.
- [x] `/new` with two running researchers cancels both, disarms the clock, and the next run has
  a different `run_id`; the browser session object is the same.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement command parsing and the three commands; wire the welcome loop.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Live: `/model head <other>` mid-run, then a follow-up question that references an earlier
  answer is answered consistently.

### Phase 7: Prompts, docs and supersession
**Risk:** none
**Test-first:** N/A — documentation and prompt polish only
**Goal:** Docs reflect the session model and local-laptop deployment; the superseded TUI plan
is marked; prompts read as one coherent contract.
**Requirements:** none
**Files:**
- `CLAUDE.md`, `docs/INDEX.md`, `docs/guides/setup.md` — deployment = local laptop (homelab
  future); status paragraph; shared-resources rows for `harness/session.py` and `dispatch.py`.
- `docs/architecture.md` — session loop, dispatch, failure modes (cancellation, reseed).
- `docs/decisions.md` — D1-D6 headlines with dates.
- `docs/plans/PLAN-tui-redesign.md` — Status → superseded by this plan.
- `docs/backlog.md` — PlainRenderer removal; `/budget` `/report` if not built.
- `harness/prompts/orchestrator.md` — final pass for consistency.
**Diff budget:** ~120-200 lines across 7 files

**Reuse:**
- Follow the entry shape already in `docs/decisions.md` and `docs/backlog.md`.

**Out of scope:**
- Code changes of any kind other than prompt wording.

**Manual verification:**
- [x] `grep -rn "homelab" CLAUDE.md docs/INDEX.md` shows only the "future" mention.
- [x] `docs/plans/PLAN-tui-redesign.md` line 3 reads a superseded status naming this plan.

**Steps:**
1. Update each listed doc; mark the TUI plan superseded.
2. Run the quality gate (see Verification).

**Acceptance criteria:**
- [x] `docs/INDEX.md` Shared Resources lists `harness/session.py` and `harness/tools/dispatch.py`.

## Verification
- [ ] Live end-to-end on a TTY: question → dispatches → per-return narration → mid-run steer →
  `ask_user` with choices → report path → post-report question → `/sources` → `/model` →
  `/new` → quit. Then a fresh run quit before any report: no file in reports/, exit 1.
- [ ] `uv run pytest` (coverage floor 90% on `harness/`)
- [ ] `uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy .`

## Notes
- Non-TTY invocation after Phase 3 should print "requires an interactive terminal" and exit 2;
  `PlainRenderer` stays in the tree until the backlog item removes it.
- `ScriptedChatModel` replies for the lead must now include `dispatch_researcher` and
  `submit_report` tool calls; check `tests/test_agent.py` fixtures that script `task` calls.

## Risks
#1. **Standalone researcher compile may not match `task`'s behaviour** — D1 compiles the
    researcher with `create_deep_agent` instead of letting `SubAgentMiddleware` build it. The
    middleware stack (summarizer wrapper, filesystem backend rooted at the run workspace, reader
    nesting, tool-call patch) must be reproduced; the architecture doc warns a nested tier
    receives none of it automatically. Confirm in Phase 1 by asserting the compiled graph's
    middleware names and that `fetch_pages` exists only on the reader.
#2. **Cancellation and shared resources** — `/new` and Ctrl-C cancel researcher tasks mid-fetch
    while they share the one `BrowserSession`. Confirm the browser survives a cancelled crawl
    (relaunch-at-most-once rule) and that cancelled tasks are awaited before the run is torn down.
#3. **Key thread vs Rich Live** — a daemon `read_keys()` thread runs for the whole session on
    both termios and msvcrt; today it runs only during interrupts. Watch for redraw tearing,
    echo leakage, and Windows `msvcrt` blocking on exit. The composer must be drawn inside the
    renderable, never printed.
#4. **Reseeding a thread across models** — `aupdate_state` with the full message list must
    preserve tool-call/`ToolMessage` pairing or the new model errors on its first turn; a
    provider may reject another provider's message shapes. Confirm with a scripted two-model test
    and one live switch.
#5. **Lead behaviour on the new prompt** — the head model may narrate every return without
    dispatching follow-ups, or call `submit_report` too early. Phase 1's prompt is the lever;
    budget a live tuning pass and record the wording that worked in `## Discoveries`.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->
- 2026-08-27 — Phase 2 test bullet "wall-clock expiry after `submit_report` → report kept" is
  unreachable by construction: `submit_report` disarms the hard clock (Phase 2 Contracts), so
  the clock can never fire after an answer exists. Replaced by
  `tests/test_agent.py::test_main_keeps_the_report_when_time_passes_after_submit_report`
  (time passing after submit never cuts short) and the margin-zero test asserting exit 1 / no
  report when the clock fires before any answer. R5/R6 semantics unchanged. Approved by the
  developer on Phase 3 entry.
- 2026-08-27 — Phase 3 contract "one `asyncio.Queue`" → one stdlib `queue.Queue` fed by the
  daemon key thread. `_run_welcome` is synchronous and blocks the event loop while it reads, so
  `call_soon_threadsafe` into an `asyncio.Queue` would never be processed during the welcome
  screen; async consumers read via `run_in_executor`, with a `None` sentinel on close so no
  executor thread is left blocked at exit (Risk #3). One thread, one queue — the contract's
  intent — is preserved. Approved by the developer at Phase 3's 3F gate.

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->
- 2026-08-25 — Phase 1: diff budget overrun — ~2,600 insertions / 14 files against a ~500-750 /
  8-file band. ~500 lines are the approved wholesale move of budgets + report gate from
  `__main__.py` into `session.py`; the rest is the new `Session` surface plus rescripting ~15
  tests that drove the lead through `task`. → deferred (recorded for Phase 2-6 budget calibration).
- 2026-08-25 — Phase 1: Risk #5 live tuning pass not yet run — scripted tests cannot exercise the
  head model's behaviour on the new `dispatch_researcher`/`submit_report` prompt. → deferred to
  the developer's first live `uv run python -m harness "<question>"`; record the wording that
  produced dispatch → per-return narration → `submit_report` here.
- 2026-08-27 — Phase 2: `cut_short == "wall_clock"` WITH an answer is now unreachable in
  production (the clock is disarmed the moment `submit_report` is accepted), but `report.py`
  still renders that combination and `test_report.py` covers it directly. → deferred: drop the
  dead rendering branch when `CutShortReason` is next touched (Phase 2 forbade changing it).
- 2026-08-27 — Phase 2: `Session._finish_roster_row` (not in the plan) wraps the sink write that
  sits between a researcher's completion and its `events.put_nowait`; a renderer crash there
  becomes `_fatal` instead of stranding the return and hanging the loop. → acted (kept; it is
  the same fail-as-a-display-bug rule `on_activity_change` already applies).
- 2026-08-27 — Phase 2: the roster hook lives in `Session` (dispatch/finish/cancel call the sink
  directly), not as a lead-tier middleware in `agent.py` as the plan's file list suggested —
  the session owns the researcher task lifecycle and `_ToolActivityMiddleware`'s docstring
  keeps the lead tier uninstrumented. → acted.
- 2026-08-27 — Phase 2 3F Minors (deferred, no question asked per developer instruction):
  (a) `Session._finish_roster_row`'s docstring claims `_fatal` is raised by the loop for both
  call sites, but the `_cancel_running` site runs in `run()`'s `finally` after the loop exited,
  so a `DisplayError` there is silently dropped while `_finish` may still write the report →
  narrow the docstring or check `_fatal` before the report gate; (b)
  `tests/test_config.py::test_shipped_harness_toml_declares_the_researcher_cap` cannot fail if the
  key is deleted from `harness.toml` (default is also 4) → assert on the parsed TOML text; (c)
  the Phase 2 test bullet "wall-clock expiry after `submit_report` → report kept" is
  unreachable by construction once the clock is disarmed at submit — the two `test_agent.py`
  tests were re-premised (`test_main_keeps_the_report_when_time_passes_after_submit_report`;
  the margin-zero test now asserts exit 1 / no report). This is Contracts-vs-Tests drift that
  needs a `## Reconciliations` entry re-approved by the developer → PENDING developer
  re-approval on return (not written unilaterally).
- 2026-08-27 — Phase 3: the `## Notes` "non-TTY → exit 2" behaviour is deferred to Phase 7 —
  every headless `main()` test runs non-TTY, so flipping it now would take the whole
  `test_agent.py` main() suite with it. Phase 3 adds `Session(interactive=...)` instead:
  `False` keeps the nudge-then-fail idle backstop and returns right after the report.
  → deferred (Phase 7 decides whether to keep the flag or make TTY mandatory).
- 2026-08-27 — Phase 3: the report is written when `submit_report` is accepted and a
  `ReportWritten(path)` timeline event replaces the immediate `RunFinished`, which now fires at
  session end — `RunFinished` tears down the Live screen, so it cannot precede post-report
  chat. → acted.
- 2026-08-30 — Phase 3 review deferrals (developer-approved): (a) raw mode is now held for the
  whole session, so `\x03` is a key, not SIGINT — after the composer's first `interrupt`
  triggers a quit, a hung provider call in `_chat_turn` or `_finish`'s verification leaves no
  keyboard escape (a second Ctrl-C is never read); revisit alongside Phase 6 `/new` teardown.
  (b) `__main__.py`'s "do not reorder" welcome-teardown invariant and its test now pin only the
  injected-generator path — production restores raw mode later via `KeyReader.close()`,
  harmlessly. (c) `RichRenderer._timeline` is unbounded and grows per chat turn, pushing the
  composer toward the crop edge — Phase 5's transcript bounding owns the fix. (d) `_type` key
  helper is duplicated in tests/test_ask_user.py and tests/test_main_welcome.py — move to
  conftest on the next touch. Phase 3 diff landed ~3x the phase budget (1,329 insertions / 11
  code files vs ~300-450 / 6) — recorded for later budget calibration, like Phase 1.
- 2026-08-27 — Phase 3 3F review dispositions (4 Majors, 4 Minors): Major 1 (post-report chat
  outside `run()`'s exception handling) → acted — `_chat_turn` discloses a failed chat turn as
  an `Alert`, keeps the chat open, leaves `cut_short` None so a report on disk stays a clean
  exit; Major 2 (round cap counts post-report turns) → acted — `_note_model_turns` gains the
  same `answer is None` guard the margin check has; Major 3 (keyboard→queue bridge untested) →
  acted — Enter/empty-line/answer-routing tests in `tests/test_ask_user.py` plus
  `Session.receive_user_message`'s contract test; Major 4 (diff budget 2.3× over) → deferred
  to budget calibration like Phase 1. Minor a was already reconciled (queue.Queue entry,
  approved at the 3F gate); Minor b → acted — `AnswerDraft` deleted and the overlay draws no
  answer row (the composer is the one input line; Phase 4 reworks the overlay); Minor c →
  acted — Ctrl+D quits the welcome screen like Ctrl+C; Minor d → acted — `_run_task` is
  cleared when the turn loop ends, so a quit landing after `run()` returned can no longer
  cancel `main()`'s `finally`; the second simplification → acted — the function-local
  `UserMessage` import in `Composer._send` replaced by `Session.receive_user_message`.

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->

### 2026-08-25 — Phase 1: Session tracer — dispatch tool, return injection, submit_report
- Done: `harness/session.py` (`Session`, `ResearcherReturn`/`UserMessage`, D2 turn loop, D3
  termination) and `harness/tools/dispatch.py` (`dispatch_researcher`, `submit_report`); lead
  runs with `subagents=[]`; `build_researcher_graph` compiles the researcher standalone
  (`_researcher_spec` deleted, its pieces became `_researcher_middleware`/`_researcher_prompt`/
  `_backend`); `__main__.py` 1216 → ~570 lines; orchestrator.md rewritten for the new tools.
  838 tests, 96% coverage, gates clean.
- Learned: budgets (wall clock, round cap, synthesis margin) AND the report gate were MOVED into
  `Session` in this phase (orchestrator scoping decision) — Phase 2 does not relocate them, it
  changes their semantics (arm on first successful dispatch, disarm at `submit_report`,
  post-report never cuts short) and adds `agent.max_researchers` (today `_MAX_RESEARCHERS = 4`
  in session.py) plus roster data. Report is written iff `submit_report` was called and
  `cut_short != "error"`. 3F fixes: `submit_report` refuses while researchers run except during
  a forced synthesis pass (`_forced_synthesis`); `_cancel_running` records a
  `researcher_cancelled` incident per dropped researcher; pass-through exceptions
  (search-abort/DisplayError) set `_fatal` without a fabricated `RESEARCHER FAILED`. Headless
  idle lead gets one `_SUBMIT_NOW` nudge then fails (Phase 3 replaces idle with wait-for-user).
  Gathered `dispatch_researcher` ToolMessages come back in completion order, not call order —
  never assert positionally. `tests/conftest.py` now has `_dispatch_call`/`_submit_call`,
  `_LeadModel` (auto-submits on the synthesis phrase), `patch_run_by_role`, `patch_models_by_role`.
- Drift: none.
- Watch-next: the live TTY acceptance criterion (two "started" lines → per-return narration →
  report path) and Risk #5's prompt tuning are unrun — needs real models; see `## Discoveries`.

### 2026-08-27 — Phase 2: Budgets, roster data and run exits inside the session
- Done: `[agent] max_researchers` (default 4, `gt=0`) in config + harness.toml, read by
  `Session.dispatch`; `ResearcherState` + `ActivitySink.start_researcher/finish_researcher/
  researchers()` (list, start order), fed by `Session` on dispatch/finish/cancel — the
  `Roster:` line is built from the sink; `submit_report` disarms the hard clock
  (`_clock.reschedule(None)`), is refused once an answer exists, and the synthesis-margin check
  is gated on `answer is None` (3F Major fix); `Session.clock_armed` property; session-level
  Ctrl-C test (cancels the `run()` task — a `KeyboardInterrupt` raised inside a langgraph node
  kills pytest itself). 848 tests, 96% coverage, gates clean.
- Learned: `cut_short == "wall_clock"` with an answer is unreachable now (report.py branch is
  dead); `_finish_roster_row` guards the sink write between a researcher's completion and its
  `events.put_nowait`. `_cancel_running` is confirmed safe for Risk #2: cancels, closes roster
  rows, one `researcher_cancelled` incident each, `gather(return_exceptions=True)`, and
  `BrowserSession.arun_many` catches `Exception` not `BaseException`, so a mid-crawl
  `CancelledError` does not consume the one browser relaunch.
- Drift: pending — see the 2026-08-27 3F Minors entry in `## Discoveries` (c): the plan's Phase
  2 test bullet contradicts the disarm-at-submit contract; a `## Reconciliations` entry awaits
  developer re-approval.
- Watch-next: Phase 3 replaces the headless idle backstop (`_SUBMIT_NOW` nudge then fail) with
  wait-for-user; the post-report loop must keep `dispatch`/`submit` refusing. Resolve the
  pending Reconciliation first.

### 2026-08-30 — Phase 3: Composer — queued user messages and post-report chat
- Done: session-long `KeyReader` (one daemon key thread → stdlib `queue.Queue`, welcome/run/
  interrupt/post-report all read from it; `eof` KeyKind added) + `Composer` in `__main__.py`
  (Enter → `Session.receive_user_message`, pending `composer.answer()` future for `ask_user`,
  interrupt/eof → `request_quit`); `Session(interactive=...)` — interactive idle waits for the
  user (headless keeps the nudge-then-fail backstop), post-report `_chat_loop`/`_chat_turn`
  (provider errors disclosed as alerts, KeyboardInterrupt/cancel = clean quit, report survives);
  report written at submit (`ReportWritten` timeline event), `RunFinished` moved to session end;
  `ComposerDraft`/`UserTurn`/`AgentText` display events, composer drawn inside the renderable;
  `AnswerDraft` deleted; orchestrator.md gained `# Messages from the user` / `# After the
  report`. Three 3F passes (flagged !#3): 4 Majors + 9 Minors found, all fixed except four
  developer-approved deferrals (see Discoveries 2026-08-30). 870 tests, 96.8% coverage, gates
  clean.
- Learned: raw mode now spans the whole session, so Ctrl-C arrives as a KEY, not SIGINT — quit
  routes through `Composer` → `request_quit()` (cancels the run task pre-report; flips the chat
  loop post-report), and `_run_task` is cleared in `run()`'s `finally` so a late key cancels
  nothing. A `KeyboardInterrupt` raised inside a langgraph node task escapes `asyncio.run`
  regardless of awaiters (kills pytest too) — deliver test interrupts via `session._stream_pass`,
  never a scripted model. `_abort`/`reader.close()` must run BEFORE `renderer.close()` (restore
  while the alternate screen still owns the terminal); `KeyReader.close()` is idempotent.
- Drift: two Reconciliations this phase — the Phase 2 unreachable test bullet (approved on
  entry) and the key-thread contract `asyncio.Queue` → stdlib `queue.Queue` (welcome blocks the
  loop; `run_in_executor` + `None` sentinel).
- Watch-next: the live TTY acceptance criterion (type "skip the routing angle" mid-fan-out)
  is still unrun — needs real models, along with Phase 1's live pass. Phase 4 moves interrupt
  handling into `Session` and should fold `composer.answer()` digits; watch Discoveries (a) —
  no keyboard escape while a post-report turn hangs — when touching that seam.

### 2026-08-30 — Phase 4: ask_user with choices
- Done: `AskUserInput.choices: list[str] | None` (`max_length=4`) + docstring lifting the
  pre-research-only restriction; the interrupt round-trip moved from `__main__._answer_questions`
  into `Session._collect_answers` (URL approval, `_NO_ANSWER_GIVEN`, question fallbacks all
  moved with it), with `answer_source: Callable[[], Awaitable[str]]` replacing
  `answer_interrupt` — `__main__` supplies only where the text comes from (composer.answer on a
  TTY, the stdin `_read_answer` bridge headless); digit 1-N resolves to the choice text via
  `_resolve_answer`, anything else is verbatim; overlay renders numbered choices + an
  `answer with 1-N or type freely` hint; orchestrator.md `# Clarification` rewritten. Review
  fixes: choices clamped to four in `_collect_answers` (the interrupt path never executes the
  tool, so the schema cap is only a model-facing hint), the stage header returns to
  `researching` after a mid-research answer with researchers still running, `answer_source`'s
  unreachable `| None` branch dropped. 879 tests, gates clean.
- Learned: `HumanInTheLoopMiddleware._process_decision` SKIPS tool execution for `respond`
  decisions — `args_schema` validation never runs on the interrupt path, so any R-contract on
  ask_user's arguments must be enforced in `_collect_answers`. `interrupts[0]` assumes the
  lead is the only node with `interrupt_on`; that breaks if a second tier ever gets it.
- Drift: none.
- Watch-next: Phase 5 (chat TUI) restructures `_build_renderable` — the transcript bound
  (Discoveries 2026-08-30 (c)) is owed there, and the `clarifying`/`researching` stage
  handoff just added should be re-checked against the new task dock.

### 2026-09-01 — Phase 5: Chat TUI — transcript, task dock, researcher roster
- Done: chat-TUI frame in display.py (session bar with crumb + `sources: N`, bounded transcript
  via `deque(maxlen=_TRANSCRIPT_TAIL)`, `Tasks — N of M done` dock, researcher roster from
  `ActivitySink.researchers()`, composer, footer); Session emits `RunStarted`/`LeadToolCall`/
  `AgentText`/`ResearchersUpdated` transcript events; lead tool calls rewrite in place on
  result (keyed by call_id); PlainRenderer one-line-per-event path kept. NOTE: implemented by a
  prior session that died mid-phase (no handoff); this session adopted the uncommitted diff
  after 157 targeted tests passed — red-first evidence unavailable, compensated by the full
  gate (883 tests) and a clean flagged (!#3) judgment review. Phase 3 deferral (c) (unbounded
  `_timeline`) is paid.
- Learned: reviewer confirmed Risk #3 discipline — every new event handler mutates state and
  calls `_live.refresh()`, no direct prints, key-thread seam untouched; a `DisplayError` in
  `dispatch()`'s `_emit_researchers` rides `_PASS_THROUGH_TASK_FAILURES` to a failed run by
  design.
- Drift: none.
- Watch-next: two live checks owed at final verification alongside the screenshot-vs-mock
  acceptance criterion: (a) the `clarifying`/`researching` stage handoff against the new task
  dock (only indirectly evidenced in tests), (b) Phase 6 begins slash commands — the composer's
  leading-`/` text stops being an ordinary message there.

### 2026-09-03 — Phase 6: Slash commands — /sources, /model, /new
- Done: slash parsing in `Session.receive_user_message` (a `/` line never becomes a
  `UserMessage`); `CommandReply` display event (dim, no byline, Plain + Rich); `/sources`
  from `SourceRegistry.all()`; `/model <role> <choice>` as a `ModelSwitch` event applied at
  the turn boundary in `_next_batch` — head switches rebuild the agent, mint a new
  `thread_id`, and reseed messages AND todos via `aget_state`/`aupdate_state`
  (`as_node="TodoListMiddleware.after_model"` for todos); researcher/reader switches
  recompile the researcher graph; `/new` sets `restart_requested`, reuses
  `request_quit`/`_cancel_running`, and `__main__.main()` now loops welcome↔session with the
  `BrowserSession`, key reader, config and preflights held outside the loop; new public
  `BrowserSession.rebind_run(run_log, run_id)` re-points incidents and the downloads dir at
  the current run (replaces a private poke); `run_id` gained a random suffix against
  same-second `/new` collisions. 897 tests + gates clean. 3F (flagged !#4): Major fixed —
  the reseed test now carries tool_call/ToolMessage pairs across the switch.
- Learned: an `aupdate_state` targeting a fresh thread with unspecified `as_node` resolves
  to `__start__`, whose writers cover only the input schema (`messages`) — other channels
  are silently dropped, not errored; seeding `todos` requires targeting the owning node
  (`TodoListMiddleware.after_model`). A first report that this was impossible turned out to
  be a test race (the switch was declared done at `thread_id` change, before `_seed_todos`
  ran) — wait for the switch's `CommandReply` in tests. `_seed_todos` keeps a
  verify-and-disclose safety net (`model_switch_todos_not_carried` incident) for future
  deepagents changes.
- Drift: none.
- Watch-next: Phase 7 is docs/prompts only. Live checks stacked for final verification:
  the `/model` cross-provider switch (risk #4's only untestable half), the stage-handoff
  and screenshot-vs-mock items from Phase 5, and the phase acceptance one-liners on live
  runs. Phase 7 must also reconcile the docs' downloads-dir wording with `rebind_run`.

### 2026-09-03 — Phase 7: Prompts, docs and supersession
- Done: CLAUDE.md/INDEX.md deployment = local laptop (homelab future); INDEX.md status
  paragraph rewritten for the chat session, Shared Resources rows for `session.py`,
  `dispatch.py`, `rebind_run`, and the researcher-wiring row re-pointed from the deleted
  `_researcher_spec` to `build_researcher_graph`; architecture.md gained `## Session Loop`
  (D1-D4, D6, `interactive=False` seam) and a current Directory Structure; decisions.md
  D1-D6 headlines + the non-TTY-guard deferral; backlog.md entries for PlainRenderer
  removal, `/budget`/`/report`, the non-TTY startup guard (half exists: no-question
  non-TTY already exits 2 via argparse) and orchestrator.md's hardcoded researcher cap;
  setup.md live-check paragraph describes the chat flow; PLAN-tui-redesign.md superseded.
  orchestrator.md unchanged (reviewer confirmed every behavioural claim against the code).
  Adopted a dead session's uncommitted diff (adopt-and-verify), then fixed all 2 Majors +
  4 Minors from the 3F quality scan in place. 897 tests, gates clean.
- Learned: `python -m harness "<question>"` still works and skips the welcome screen; on a
  piped stdin it runs headless (`Session(interactive=False)`) — "no non-TTY mode" is a
  support statement, not a code fact, and the docs now say so consistently.
- Drift: none.
- Watch-next: Final verification is entirely live checks — see the Phase 6 Watch-next list
  plus the plan's `## Verification` walkthrough; nothing left for scripted tests.
