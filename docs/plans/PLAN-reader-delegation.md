# PLAN: Reader Subagent Delegation

**Status:** In Progress
**Created:** 2026-08-14
**Type:** Single plan

## Intent

**True goal:** The lead agent's context fills with raw fetched pages, capping how deep a
research run can go. Wire the frozen reader tier (@harness/prompts/reader.md) into live
delegation: the lead spawns a reader subagent per link; the reader fetches and analyzes the
page with its own tool calls, then terminates and returns a digest. This relieves lead
context, widens effective coverage per round, improves source reading fidelity, and routes
heavy token volume to the `[roles.subagent]` model.

**Binding outcomes:**
- **R1** — Every fetched page reaches the lead as a reader digest; the lead never receives
  raw page content in the normal path.
  - Delegation shape is binding: the lead hands the reader a URL (never page content); the
    reader does its own fetching and analysis via tool calls, then terminates and returns
    its report to the lead.
- **R2** — When digestion fails after bounded retry, the lead gets the raw page wrapped in
  an explicit XML-like marker identifying it as undigested, and the run discloses it.
- **R3** — Claim verification keeps judging claims against original source text; reader
  digests never enter verification.
- **R4** — `[Sn]` citation IDs survive digestion: digested findings cite sources that the
  report and verifier can still resolve.
- **R5** — Digestion is observable per run: the report discloses which pages were digested
  and which fell back raw.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- Deeper reports on the same round-cap/wall-clock budget.
- Digest latency kept reasonable (sequential is acceptable; no hard number).

**Non-goals:**
- Researcher tier stays unwired; its frozen contract (@harness/prompts/subagent.md) is untouched.
- No concurrent subagents; delegation is sequential this round.
- No recursive delegation (readers spawn nothing).
- Verification search tool (agentic verifier that greps source text) — deferred to docs/backlog.md.
- No config cost cap on reader calls (single-user homelab, fine at this scale).

**Constraints & assumptions:**
- Reader model comes from `[roles.subagent]` in harness.toml — never hardcoded.
- Adding the reader must not modify the agent loop (standing invariant: tools are additive;
  only loop-stopping tools like `ask_user` touch `interrupt_on`).
- Best-effort + disclose invariant governs all degraded paths.
- Cost tolerance: one extra subagent model call per fetched page is acceptable; no budget knob.

**Open questions:**
- Does the frozen reader contract (@harness/prompts/reader.md) actually fit per-URL
  digestion as bound in R1 — complete enough to define the digest's expected shape?
  *(Answered in exploration: yes for R1/R4 — URL-in/digest-out, [Sn] untouched, tools
  match; silent on R2/R5, which are lead-side concerns and live outside the contract.)*
- How do the fetch tool and source registry hand off today — where do `[Sn]` IDs attach,
  and what does verify.py consume (registry text vs re-fetch)?
  *(Answered: `SourceRegistry.add` mints IDs inside the fetch tool; verify.py reads
  `<workspace>/<run_id>/sources/Sn.md` capture files and never refetches — see Codebase Map.)*

## Codebase Map
All facts below are subagent-confirmed (planning exploration, 2026-08-14).

- Entry points: `harness/__main__.py` — CLI run driver; `harness/agent.py::build_agent`
  (`create_deep_agent` at agent.py:90) — the ONLY module that imports deepagents.
- Fetch path: `harness/tools/fetch.py` — `build_fetch_tool(config, registry) -> BaseTool`
  (fetch.py:315); `_fetch` drives `crawl4ai.AsyncWebCrawler.arun_many`; `classify()` maps
  results to `FetchOutcome`; `_render()` caps model-facing text at
  `fetch.per_page_char_cap` (12000, harness.toml); `_write_source_file()` writes the
  UNTRUNCATED capture to `<workspace>/<run_id>/sources/Sn.md`; `FETCH_FAILED_PREFIX` /
  `is_failed_capture` mark failed captures.
- Tool registry: `harness/tools/__init__.py::build_tools(config, registry)` returns
  `[fetch_pages, search_web, ask_user]`. Convention: every tool is `async def`, wrapped
  `@tool(..., response_format="content_and_artifact")`, errors become typed outcomes in
  the content string — never exceptions into the model.
- Sources: `harness/sources.py::SourceRegistry` — `add(url, title=None) -> str` (mints
  `S{n}`, first-write-wins by normalized URL), `get`, `all`, `link`, `resolve`,
  `unresolved_ids`; `Source(id, url, title)` holds NO page text.
- Verification: `harness/verify.py::verify_paragraphs` reads `sources/Sn.md` files
  directly, never refetches (module docstring) — R3 holds as long as reader fetches write
  the same files.
- Report: `harness/report.py` — `_is_usable` (report.py:137) reads the same capture files;
  report.py:405 prints the subagent model line ("configured but not yet wired").
- Models: `harness/models.py::build_chat_model(config, role)` / `preflight(config, role)`;
  retry lives INSIDE `ChatOpenAI(max_retries=...)` — "callers must not wrap the returned
  client in another retry layer".
- Prompts: `harness/prompts.py::render(name, **vars)` / `required_variables(name)`;
  `reader.md` requires exactly `$current_date` and `$max_urls_per_call`
  (tests/test_prompts.py:114-118); reader.md tools list = `fetch_pages` + filesystem, no
  `search_web`, no `ask_user`; "[Sn] assigned by the fetch tool itself; never invent,
  renumber, or resolve them".
- Agent assembly: `harness/agent.py:68-74` — `register_harness_profile(profile_key,
  HarnessProfile(excluded_tools=frozenset({"execute"}),
  general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))`; `_middleware()`
  (agent.py:121-126) returns TodoList + deepagents summarization middleware; `interrupt_on`
  registers `ask_user`.
- deepagents (installed 0.7.5, package-confirmed): `SubAgent` TypedDict
  (middleware/subagents.py:36-164) — required `name`/`description`/`system_prompt`;
  `tools` takes INSTANCES; `model` takes a `BaseChatModel`. `task`/`atask`
  (subagents.py:542-596) call `subagent.invoke` with NO try/except — subagent exceptions
  (incl. `GraphRecursionError`) propagate and would crash the lead run. Task tool args:
  `description: str`, `subagent_type: str`; stateless per call (fresh messages; filesystem
  state carries over). A subagent ending with no final text returns an EMPTY ToolMessage.
  Declared subagents get NO task tool (non-recursive) and CANNOT interrupt (no checkpointer
  forwarded). Per-subagent middleware is built fresh per spec (graph.py:656-743): its own
  FilesystemMiddleware + deepagents summarization middleware for the subagent's model, and
  the HarnessProfile resolved for THAT subagent's model key — `excluded_tools` only applies
  to the reader if a profile is registered under the reader model's key.
  `GeneralPurposeSubagentProfile(enabled=False)` affects only the default subagent, not
  declared ones. No per-subagent iteration knob (recursion_limit fixed at 9,999).
- langchain middleware (installed, package-confirmed): `AgentMiddleware.wrap_tool_call`
  (agents/middleware/types.py:662-666) — raised tool exceptions genuinely pass through the
  hook; `ToolErrorMiddleware` (middleware/tool_error.py) and `ToolRetryMiddleware`
  (middleware/tool_retry.py) already implement error→ToolMessage and bounded-retry
  wrapping. Middleware tools (incl. the task tool) share ONE ToolNode with regular tools
  (factory.py:1005, 1056-1068), so a middleware in the LEAD's `middleware=` list wraps the
  task tool. First-defined = outermost. Only a lead-side tool wrapper can catch
  `GraphRecursionError` (raised by the subagent's pregel loop, outside any inner hook).
- Tests: pytest (`uv run pytest`), `asyncio_mode="auto"`, offline. `tests/conftest.py`:
  `ScriptedChatModel(ChatOpenAI)` (records `_bound_tool_names`, `_received_messages`) via
  `patch_model`/`patch_run`/`scripted_model` fixtures; `install_crawler` fixture fakes
  `AsyncWebCrawler` (tests/test_fetch.py); `install_search_transport` fakes SearXNG.
  Relevant files: test_agent.py, test_fetch.py, test_verify.py, test_report.py,
  test_sources.py, test_prompts.py, test_tools_registry.py.
- Commands: `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` /
  `uv run mypy .`

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- No new `[roles.reader]` config role — the reader uses `[roles.subagent]` (see D3).
- No edits to `harness/prompts/reader.md` this round — delegation instructions live in the
  orchestrator prompt and the per-call task description (see D1 consequences).
- No custom `read_source` wrapper tool around a hand-rolled agent loop — ruled out in D1.
- No digest-inside-fetch single-call shape — ruled out in D1 (violates R1's bound shape).
- No strict one-URL-per-delegation rule — ruled out in D5.

## Design Decisions

### D1: Delegation mechanism
- **Chosen:** deepagents-native — declare the reader as a `SubAgent` spec passed via
  `create_deep_agent(subagents=[...])`; the lead delegates through the framework's `task`
  tool (`subagent_type="reader"`). Reader spec: `system_prompt=render("reader",
  current_date=..., max_urls_per_call=...)`, `model=build_chat_model(config, "subagent")`,
  `tools=[<the run's fetch_pages instance>]` (deepagents adds filesystem tools itself).
- **Rejected:** custom `read_source(url)` tool wrapping a self-compiled reader agent —
  most new code, drives a second agent loop inside a tool coroutine (no precedent), and
  breaks the "only agent.py imports deepagents" boundary. Single-call digest inside the
  fetch tool — cheapest (~1x extra call vs the 3–10x delegation multiplier,
  docs/architecture.md) but drops the bound spawned-subagent shape in R1; developer
  explicitly kept the agentic shape with the cost on the table.
- **Consequences:** the lead's toolset must physically exclude `fetch_pages` (R1 is
  structural, not prompt-begged). Passing the SAME fetch tool instance to the reader is
  what satisfies R3/R4 (shared `SourceRegistry` + same `sources/` dir). Task calls are
  stateless; all digest context must travel in the task `description`. reader.md stays
  frozen — the runtime call shape supplies objective/output framing.

### D2: Failure path (R2)
- **Chosen:** lead-side bounded retry + error-catch middleware in `_middleware()` using
  langchain's existing `ToolRetryMiddleware`/`ToolErrorMiddleware` shapes (see Codebase
  Map for the package facts), converting reader crashes into `status="error"` ToolMessages;
  plus a narrow lead-visible fallback tool that fetches raw content, wraps it in an
  explicit `<undigested>` marker, writes the normal capture file, and records the fallback
  (R5). Retry count stays at 1 — retrying `task` re-runs the whole reader including
  ChatOpenAI's own internal retries (see models.py guidance), so the budget multiplies.
- **Rejected:** skip+disclose (no fallback tool) — smallest diff but a failed digest loses
  the page entirely; developer chose coverage. Middleware-fetches-raw — fully structural
  but the middleware must parse URLs out of task descriptions and drive crawl4ai from the
  middleware layer; heaviest and most fragile.
- **Consequences:** the fallback boundary is prompt-steered, not structural — the lead
  COULD call the fallback tool in the normal path; R5's per-use disclosure is the
  detection mechanism. An empty digest (empty ToolMessage) must be treated by the lead
  prompt as a failure eligible for fallback.

### D3: Reader model role
- **Chosen:** reuse the existing `[roles.subagent]` role (`gpt-5.6-luna`).
- **Rejected:** new `[roles.reader]` — config surface with no present variation point
  (YAGNI); revisit when the researcher tier is wired and the two want different models.
- **Consequences:** wiring the researcher tier later may force the role split; report.py's
  "configured but not yet wired" line (report.py:405) becomes stale and is updated here.

### D4: R5 recording mechanism
- **Chosen:** a per-source read-mode field on `SourceRegistry`'s `Source` model (e.g.
  digested / fallback / unread), written by the fetch path and the fallback tool, read by
  report rendering.
- **Rejected:** deriving digested-vs-fallback from disk markers in the capture files —
  couples the report to string-parsing of file bodies; the registry is already the
  per-source metadata home report.py walks.
- **Consequences:** `sources.py` and `report.py` both change; the field is the frozen seam
  between Phase 2 (recording) and Phase 3 (rendering).

### D5: URLs per delegation
- **Chosen:** the lead may pass one or MORE URLs per task call, bounded by
  `fetch.max_urls_per_call` (reader.md is already parameterized with `$max_urls_per_call`).
- **Rejected:** strict one-URL-per-task — multiplies the 3–10x delegation overhead per page
  with no fidelity gain.
- **Consequences:** orchestrator prompt guidance must state the batching bound; digest
  output covers multiple `[Sn]` IDs per call.

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Lead only sees digests; URL-in/digest-out delegation | Phase 1 (structural toolset split), Phase 3 (prompt half) |
| R2 | Bounded retry then marked raw fallback, disclosed | Phase 2 |
| R3 | Verification stays on original source text | Phase 1 (shared fetch instance), Phase 4 (regression assertion) |
| R4 | `[Sn]` IDs survive digestion | Phase 1 (shared registry), Phase 4 (end-to-end resolution) |
| R5 | Report discloses digested vs fallback per page | Phase 2 (recording), Phase 3 (rendering) |

## Progress
- [x] Phase 1: Wire the reader subagent (tracer bullet)
- [ ] Phase 2: Failure path — retry, catch, fallback tool
- [ ] Phase 3: Disclosure and prompt wiring
- [ ] Phase 4: End-to-end regression + docs
- [ ] Final verification

## Phases

### Phase 1: Wire the reader subagent (tracer bullet)
**Risk:** flagged (!#1)
**Test-first:** required
**Goal:** The lead agent is built with a declared `reader` subagent and can no longer fetch
directly — delegation is structurally live end-to-end on scripted models.
**Requirements:** R1, R3, R4
**Assumes:**
- deepagents 0.7.5 behaves as package-confirmed in the Codebase Map (SubAgent spec fields,
  tools-as-instances, declared subagents unaffected by the general-purpose disable).
**Files:**
- `harness/agent.py` — build the reader `SubAgent` spec per D1; pass `subagents=[...]`;
  register a `HarnessProfile` under the READER model's profile key (execute exclusion +
  general-purpose disable), mirroring agent.py:68-74.
- `harness/tools/__init__.py` — split tool building: lead set excludes `fetch_pages`;
  the fetch instance is built once and routed to the reader spec.
- `tests/test_agent.py`, `tests/test_tools_registry.py` — new/updated cases.
**Diff budget:** ~120-190 lines across 4-5 files

**Reuse:**
- Extend `build_tools` in `harness/tools/__init__.py` — do NOT create a parallel registry.
- `render("reader", ...)` via `harness/prompts.py`; `build_chat_model(config, "subagent")`
  via `harness/models.py` — both exist, currently uncalled for this role.
- Pattern to mirror: `harness/agent.py:68-74` profile registration; test shape in
  `tests/test_agent.py` (ScriptedChatModel bound-tool assertions).

**Contracts:**
- Subagent name `"reader"` — the `subagent_type` value the lead prompt (Phase 3) uses.
- Lead toolset contains `task`, `search_web`, `ask_user` + deepagents-injected tools and
  NOT `fetch_pages`; the reader's tools include the run's `fetch_pages` INSTANCE.
- Reader spec model comes from `build_chat_model(config, "subagent")`; prompt from
  `render("reader", current_date=..., max_urls_per_call=...)`.

**Out of scope:**
- Failure/fallback handling (Phase 2), prompt guidance for WHEN to delegate (Phase 3),
  report changes (Phase 3), any edits to reader.md or subagent.md, any verify.py changes.

**Tests (write first, confirm red):**
- [x] Lead's bound tools include `task` and exclude `fetch_pages` (ScriptedChatModel
  `_bound_tool_names`).
- [x] The declared reader spec carries the subagent-role model, the rendered reader.md
  prompt, and the same fetch tool instance the run built (identity, not equality).
- [x] A profile is registered under the reader model's key excluding `execute`.
- [x] `build_tools` split: lead list and reader routing behave per Contracts.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the toolset split and reader spec wiring per Contracts.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] Existing suite still green (`uv run pytest`) — especially test_fetch.py (fetch tool
  itself unchanged) and test_agent.py's interrupt/profile cases.

### Phase 2: Failure path — retry, catch, fallback tool
**Risk:** flagged (!#2, !#3)
**Test-first:** required
**Goal:** A reader crash cannot kill the run: the lead receives a bounded-retried, then
error-classified ToolMessage, and can recover the page via the marked raw-fallback tool.
**Requirements:** R2, R5
**Assumes:**
- Phase 1 landed; langchain middleware facts hold as package-confirmed (wrap_tool_call
  receives raised exceptions; task tool shares the lead ToolNode).
**Files:**
- `harness/agent.py` — add retry/error middleware to `_middleware()` scoped to the `task`
  tool (retry count 1, per D2).
- `harness/tools/fallback.py` — `new`: the fallback fetch tool factory (`fetch_raw`),
  reusing fetch.py's fetch/classify/capture internals; new file because it is a distinct
  registered tool, matching the one-factory-per-tool layout of harness/tools/.
- `harness/tools/fetch.py` — expose/share whatever internal helpers the fallback tool
  needs instead of duplicating them.
- `harness/sources.py` — add the per-source read-mode field (D4).
- `harness/tools/__init__.py` — register `fetch_raw` in the lead's set.
- Tests: `tests/test_fallback.py` (new, mirrors test_fetch.py's fake-crawler harness),
  `tests/test_agent.py`, `tests/test_sources.py`.
**Diff budget:** ~200-300 lines across 7-8 files

**Reuse:**
- langchain's `ToolRetryMiddleware`/`ToolErrorMiddleware` (installed) — do NOT hand-roll
  the wrap_tool_call plumbing unless their config cannot scope to one tool.
- fetch.py internals (`_fetch`, `classify`, `_write_source_file`, registry add) — the
  fallback tool must NOT reimplement fetching or capture writing.
- Pattern to mirror: `harness/tools/search.py` factory shape; `tests/test_fetch.py`
  `install_crawler` fake-crawler harness.

**Contracts:**
- Tool name `fetch_raw`; content wrapped as `<undigested source="Sn" reason="...">` ...
  `</undigested>` — the marker string Phase 3's prompts and report reference.
- `Source` gains a read-mode field distinguishing at least digested / fallback / unread;
  written by fetch (digested when called by reader), by `fetch_raw` (fallback), read by
  Phase 3 rendering. Exact field name/type chosen at implementation and then frozen.
- Reader failures surface to the lead as a ToolMessage with `status="error"` after exactly
  1 retry — never a raised exception.

**Out of scope:**
- Report rendering of the recorded modes (Phase 3), orchestrator prompt changes (Phase 3),
  any retry added around `build_chat_model` clients (models.py forbids it), generalizing
  the middleware beyond the `task` tool.

**Tests (write first, confirm red):**
- [ ] A task-tool exception (including `GraphRecursionError`) becomes an error ToolMessage
  after one retry; the run continues.
- [ ] `fetch_raw` returns marker-wrapped content, still writes `sources/Sn.md`, still mints
  `[Sn]` via the shared registry, and records fallback mode.
- [ ] Read-mode field: default/unread, digested, fallback transitions each observable.
- [ ] Empty-digest edge: an empty task ToolMessage is distinguishable (documented shape)
  so the lead prompt can treat it as failure.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the middleware, fallback tool, and registry field per Contracts.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Full suite green; no retry layer added around model clients (inspection against
  models.py guidance).

### Phase 3: Disclosure and prompt wiring
**Risk:** none
**Test-first:** required
**Goal:** The report discloses digested-vs-fallback per source, and the orchestrator prompt
teaches the lead the full delegation protocol.
**Requirements:** R1, R5
**Assumes:**
- Phase 2's read-mode field and `<undigested>` marker exist as contracted.
**Files:**
- `harness/report.py` — render read-mode disclosure (which sources digested, which fell
  back, which failed entirely); update the stale "not yet wired" subagent line
  (report.py:405).
- `harness/prompts/orchestrator.md` — delegation protocol: delegate reading via
  `task(subagent_type="reader")` with up to `max_urls_per_call` URLs per call; never quote
  raw page text; call `fetch_raw` ONLY after digestion failed or returned empty; treat an
  empty digest as failure.
- `tests/test_report.py`, `tests/test_prompts.py`, `tests/test_agent.py` — rendering and
  prompt-content cases.
**Diff budget:** ~90-170 lines across 5 files

**Reuse:**
- `harness/report.py`'s existing disclosure-section rendering (best-effort + disclose
  sections) — extend, do not add a second disclosure mechanism.
- `harness/prompts.py` variable plumbing for any new orchestrator `$variables`
  (test_prompts.py pins required-variable sets).
- Pattern to mirror: report disclosure test shape in `tests/test_report.py`.

**Contracts:** none beyond consuming Phase 2's — nothing later depends on new seams here.

**Out of scope:**
- Editing reader.md/subagent.md; changing verdict/Sources line formats governed by D11
  (docs/decisions.md); any change to verify.py.

**Tests (write first, confirm red):**
- [ ] Report renders each read-mode bucket correctly, including the all-digested and
  mixed-mode cases.
- [ ] Orchestrator prompt contains the delegation protocol and renders with its
  required variables (pinned set updated).
- [ ] Stale "not yet wired" line is gone from the run-config rendering.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement rendering + prompt updates per Contracts.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Full suite green; report output for a scripted mixed-mode run reads correctly by
  inspection (paste one rendered report section into the phase notes).

### Phase 4: End-to-end regression + docs
**Risk:** flagged (!#4)
**Test-first:** required
**Goal:** One scripted end-to-end run proves the whole delegation loop — lead delegates,
reader fetches via fake crawler, digest returns, report discloses and citations resolve —
and the documentation reflects the wired tier.
**Requirements:** R3, R4
**Assumes:**
- Phases 1-3 landed as contracted.
**Files:**
- `tests/test_agent.py` or a new `tests/test_delegation_e2e.py` (new only if the scripted
  scenario outgrows test_agent.py's fixtures — implementor's call, note it) — scripted
  lead+reader run.
- `docs/architecture.md`, `docs/INDEX.md` — status: reader tier wired, researcher still
  frozen.
- `docs/decisions.md` — append D-entries for this plan's D1-D5 (1-3 lines each,
  referencing this plan).
- `docs/backlog.md` — add the deferred verification-search-tool idea (agentic verifier
  that greps source text) with 1-2 lines of context.
**Diff budget:** ~100-180 lines across 4-6 files

**Reuse:**
- `ScriptedChatModel` + `install_crawler` + `install_search_transport` fixtures
  (tests/conftest.py) — the e2e test composes existing fakes, no new fake infrastructure.
- Docs conventions: 1-3 sentences per entry, reference files as @path (per CLAUDE.md).

**Contracts:** none — terminal phase.

**Out of scope:**
- Live-network testing in the suite (offline invariant); wiring the researcher tier; any
  code change beyond what the e2e test exposes as broken (a break = Drift Reconciliation,
  not silent fixing).

**Tests (write first, confirm red):**
- [ ] End-to-end scripted run: lead issues `task(subagent_type="reader")`; reader's scripted
  model drives the shared fetch tool (fake crawler); digest returns to the lead; final
  report cites `[Sn]` IDs that resolve through the shared registry (R4) and discloses
  read modes.
- [ ] Verification regression: `verify_paragraphs` on that run consumes the capture FILES
  (fake-crawler content), not the digest text (R3) — assert the verification prompt's
  source payload contains capture text absent from the digest.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Fix only what the e2e run exposes (via Reconciliation if it contradicts a phase);
   update the four docs.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Full quality gate green (see ## Verification).
- [ ] A manual live smoke run is documented as an OPERATOR step (command + what to watch:
  token spend vs the ~796k head baseline, docs/decisions.md D10) — not executed by CI.

## Verification
- [ ] `uv run pytest` — full suite, including the Phase 4 e2e scripted run.
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] Manual (operator, post-merge): one live research run; confirm the report shows
  digested sources, `[Sn]` links resolve, and disclosure lists any fallbacks.

## Notes
- Task calls are stateless: each delegation must carry its full instruction in the
  `description`; filesystem/workspace state DOES carry over (deepagents copies parent
  state minus messages), which is how the shared `sources/` dir works.
- deepagents auto-injects its own summarization middleware per subagent — satisfying
  decisions.md D6 ([Sn]-preserving summarizer) for the reader without extra wiring.
- The reader cannot interrupt (no checkpointer forwarded) and has no ask_user anyway;
  leave the spec's `interrupt_on` unset so nothing inherits the lead's ask_user entry
  into a context where it cannot pause.
- LATER-PROBLEMS.md #7 (pooled verify reply contract untested live) is unrelated but will
  share the first live smoke run — don't attribute its failures to this plan.

## Risks
#1. **Reader model profile key may not receive the execute exclusion** — deepagents
    resolves a HarnessProfile PER SUBAGENT MODEL key (graph.py:657-661); today only the
    head model's key is registered (agent.py:68-74). If the reader's key is unregistered,
    the `execute` shell tool becomes visible to the reader, breaking the no-shell
    invariant. Phase 1 registers and TESTS the reader-key profile; if the profile-key
    format doesn't match the probe's `provider:model-name` note, stop and reconcile.
#2. **Delegation multiplier strains the "same budget" preference** — architecture.md
    prices delegation at 3–10x tokens per level on top of a ~796k head baseline (D10).
    Sequential reader calls also add wall-clock per page. The preference is negotiable by
    design; the live smoke run (Phase 4 acceptance) is where this is measured, and the
    round cap / wall clock stay unchanged this plan.
#3. **Retrying `task` re-runs an entire reader session** — one retry means up to two full
    reader runs per failure, each with ChatOpenAI's internal retries beneath it. Retry
    count is pinned at 1 (D2); if live behavior shows pathological retry stacking, drop to
    0 and rely on fallback alone — a Reconciliation, not a redesign.
#4. **The scripted e2e may not exercise deepagents' real subagent node graph faithfully** —
    ScriptedChatModel drives both tiers, but subagent state-copy and middleware assembly
    run for real. If the fake cannot reach the reader's model slot (bind_tools happens
    per-subagent inside deepagents), the e2e test may need to patch at a different seam;
    surface it rather than weakening the assertion to "task tool was called".

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

## Phase Handoff Log

### 2026-08-14 — Phase 1: Wire the reader subagent (tracer bullet)
- Done: reader wired as a declared SubAgent; `build_tools` returns `ToolSets(lead, reader)`;
  `_register_no_shell_profile` covers both model keys; `_reader_spec` is the test seam;
  332 tests + all quality gates green; flagged-risk review clean.
- Learned: deepagents has no public harness-profile read accessor — tests use the private
  `_get_harness_profile` (impl-plan-sanctioned fallback). conftest gained
  `patch_models_by_role` for role-distinct scripted models; `patch_model` untouched.
- Drift: none.
- Watch-next: Phase 2's middleware must scope retry/error to the `task` tool only;
  langchain's ToolRetryMiddleware/ToolErrorMiddleware config needs checking for
  per-tool scoping before hand-rolling anything.
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->
