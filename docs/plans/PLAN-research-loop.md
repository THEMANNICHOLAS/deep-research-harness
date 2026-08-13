# PLAN: Research Loop — Single-Agent Orchestrator, Clarification, and Tier Contracts

**Status:** In Progress
**Created:** 2026-08-09
**Amended:** 2026-08-09 — deepagents locked at 0.7.x; source capture (R8), fetch cap (R9),
visible plan (R10); sequential verification; TODO-placeholder validation
**Type:** Single plan

## Intent

**True goal:** Give the developer a research loop that actually runs — one question in, one
cited markdown report out — built on `deepagents` with a single lead agent, so that the
orchestration design (delegation contracts, verification, context handling) is settled and
measured before a researcher/reader pyramid is wired on top of it. The developer is the sole
user and operator, driving runs by hand over SSH on a Linux homelab; the eventual audience
for reports is non-technical, which is why the report must never overstate its evidence.

**Binding outcomes:**

- **R1** — One command takes a research question and produces a timestamped markdown report
  on disk, printing its path. Claims in the report carry resolved, clickable sources.
  - A question that yields no usable sources still produces a report, stating that.
- **R2** — Before researching, the agent may ask the developer clarifying questions and wait
  for answers, so a misread question is caught before anything is spent. Asking is confined
  to that pre-research window.
- **R3** — The report never overstates its evidence: each claim is checked against only its
  own cited source, and unsupported, uncited, or unresolvable claims are marked as such
  rather than presented as sound.
  - A run long enough to trigger history compression still produces a fully attributed
    report — which source supported which finding survives compression.
- **R4** — The report discloses what it cannot settle or cover: sources that disagree are
  surfaced with both positions and their `[Sn]` IDs and never adjudicated; failed fetches,
  dead branches, and a run cut short by its ceiling are stated.
- **R5** — The researcher and reader tier contracts are frozen versioned artifacts — what a
  tier receives (objective, output format, tools to use, task boundaries) and what it must
  return (findings, source IDs, conflict flags) — and neither tier may ask the developer
  anything. A later round adds those tiers without renegotiating the seam.
- **R6** — Model roles declared in config resolve to working chat clients on the OpenCode
  endpoint. A missing key or unreachable endpoint fails with a message naming the role and
  provider, before any research starts. A transiently failing call (rate limit, timeout,
  5xx) is retried with bounded backoff against the same model, never switched to another.
  - An unfilled `TODO` placeholder in a role's model or its provider's `base_url` fails the
    same way, naming the offending value.
- **R7** — A run cannot spiral: a configured cap on research rounds plus a 30-minute
  wall-clock stop, either of which still yields a report from whatever was gathered. A run's
  token cost is recorded so a later pyramid can be priced against a real baseline.
  - ~~The wall clock keeps running while awaiting a clarification answer, so an unattended run
    self-terminates rather than hanging indefinitely.~~ (see `## Reconciliations` 2026-08-10 —
    Phase 5: the clock starts at the first research tool call, so a pre-research clarification
    waits indefinitely by design.)
- **R8** — Every consulted source is captured at fetch time: extracted content for a usable
  page, an explicit failure record (404, blocked, empty) for an unusable one. Claim
  verification reads only this captured content — never a refetch — and a claim citing a
  failed source is marked unverifiable, never supported.
- **R9** — A single fetch call cannot pull more than a configured number of sources
  (default 4); the bound is enforced by the tool's own input validation, not by prompt
  guidance.
- **R10** — Mid-run, the developer can see the agent's current research plan and which step
  is in progress, echoed at the terminal as it updates; the plan state survives history
  compression. (This is the surface a later UI would read.)

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**

- Keep the dependency footprint small; avoid provider SDKs that are never called.
- Prefer a flat module layout over a deep package tree, consistent with `harness/` today.
- Ruff-clean and mypy-clean under the existing configuration.
- Report readability — headings, a source list, gaps stated plainly — matters, but tuning it
  is iterative and not an acceptance gate.
- Prompt wording for the lead agent is expected to be iterated after real runs; the tier
  contracts are what must be right the first time.

**Non-goals:**

- No researcher or reader tiers *running* this round — their contracts are frozen as
  artifacts, nothing delegates to them yet.
- No truth adjudication: the harness never decides which of two conflicting sources is
  correct.
- No source reputability or quality scoring.
- No separate citation agent — citation resolution is mechanical (substrate D4).
- No digest / `read_source` second tier, no PDF extraction, no search parameterization.
- No cross-invocation follow-ups and no durable checkpointing.
- No shell or sandbox filesystem backend, and no approval-gate interrupts on any tool other
  than the clarification tool.
- No web UI, no model or provider switchover, no token-budget scheduler, no database, no
  cross-run page caching, no deployment automation.

**Constraints & assumptions:**

- `deepagents` is confirmed as the framework, pinned exact on the 0.7.x line (0.7.5,
  released 2026-08-06, at planning time). v0.7.0 was a breaking release: `TodoListMiddleware`
  / `write_todos` are no longer enabled by default (this plan opts in — D9), message history
  moved to `DeltaChannel`, and filesystem-tool output formats changed. Patch cadence is fast
  (five patches Jul 24–Aug 6 2026), so the installed package stays authoritative over docs.
- `deepagents` hard-depends on `langchain-anthropic` and `langchain-google-genai`, neither of
  which this project calls, and ships no `langchain-openai`, which must be added for the
  OpenAI-compatible OpenCode endpoint.
- A checkpointer is mandatory for human-in-the-loop interrupts. `InMemorySaver` is
  sufficient because the clarification exchange happens inside one live process, which keeps
  the no-database invariant intact.
- The default backend is in-memory `StateBackend`; workspace/report writes must be
  explicitly disk-backed, confined by `permissions`. Current 0.7.x docs list no plain disk
  backend named `FilesystemBackend` (!#8) — D6 carries the contingency.
- ~~Kimi K3 is~~ The head model is served by the existing OpenCode provider, so it is a
  `[roles]` model value and needs no new provider entry. (Head moved to `deepseek-v4-flash`
  on cost grounds at the Phase 3 gate, 2026-08-10 — see `docs/decisions.md`.)
- Nested subagents work natively in 0.7.x (the upstream issue asking to *restrict* nesting
  closed in Feb 2026); `CompiledSubAgent(runnable=...)` remains available as an option, not
  a workaround, for the later pyramid.
- Delegation costs 3-10x the tokens of a single agent and nesting compounds per level, which
  is why R7's ceiling is load-bearing rather than decorative.
- `deepagents` ships no token/cost middleware and LangSmith is excluded; R7's token figure is
  summed from `usage_metadata` on the run's final state (mechanism confirmed in Phase 3).
- Implementation is sequenced after `docs/plans/PLAN-harness-substrate.md` Phase 5 lands.
  That plan's D1 (async tools driven with `ainvoke`), D3 (`[roles]` is the only place a model
  ID is named), D4 (`SourceRegistry.add()` is the only ID minter, IDs are per-run) and D8
  (one registry per run, passed to `build_tools` once) are inherited frozen.
- `browser.backend = "playwright"`; Lightpanda is out (see `docs/decisions.md`).
- Python >=3.11 managed with uv; the venv resolves to 3.14 while mypy targets 3.12.
- Development on Windows; production is the Linux homelab operated over SSH.
- Tests are fixture-based and offline; anything network- or model-dependent additionally
  gets one documented manual live-check command.
- No hardcoded endpoints, model IDs, or keys.

**Open questions:**

- The real values for `harness.toml`'s three `TODO` placeholders —
  `[providers.opencode].base_url`, `[roles.head].model` (Kimi K3), `[roles.subagent].model` —
  needed before any live check in Phase 1 onward.

## Background

External library facts confirmed against deepagents 0.7.5 during planning (installed-package
verification still required — Phase 1 step 3). Referenced by the `## Design Decisions` below
rather than restated in them.

- **deepagents 0.7.x** — `create_deep_agent(model=, tools=, system_prompt=, middleware=,
  subagents=, skills=, memory=, permissions=, backend=, interrupt_on=, response_format=,
  checkpointer=, store=, ...)`. A subagent is a dict of `name`/`description`/`system_prompt`
  (required) plus optional `tools`/`model`/`middleware`/`response_format`/`permissions`;
  `CompiledSubAgent(name, description, runnable)` wraps a compiled LangGraph graph. The lead
  automatically gets a `task(subagent_name, task)` tool, and a `general-purpose` subagent is
  injected unless disabled via the harness profile. **Subagent context is isolated: the
  parent receives only the final result.** Coroutine-only `@tool` tools and `ainvoke` are
  supported (LangGraph async execution). **Backends:** `StateBackend` (default, in-memory,
  lost at process exit), `StoreBackend`, `CompositeBackend`, plus `LocalShellBackend` and
  ~~sandbox backends which carry a shell `execute` tool~~ sandbox backends (see
  `## Reconciliations` 2026-08-09 — Phase 3: EVERY backend gets an `execute` tool bound; only
  sandbox backends make it functional). **`TodoListMiddleware` is opt-in since
  0.7.0.** `SummarizationMiddleware` compresses history on a configurable `trigger`
  (`("tokens", N)`, `("messages", N)`, `("fraction", f)`) with a `keep` policy for what
  survives verbatim (default `("messages", 20)`).
- **Human-in-the-loop** — `interrupt_on` is keyed by **tool name** and gates *proposed* tool
  calls, with `allowed_decisions` drawn from `approve`/`edit`/`reject`/`respond`. The
  `respond` decision is documented as being for "ask_user"-style tools: the human's message
  becomes the tool result. **A checkpointer is REQUIRED**; resume with
  `Command(resume={"decisions": [...]})` under the same `thread_id`. A tool calling
  `interrupt()` itself is possible but explicitly outside the supported middleware pattern.
- **Since 0.5.0** — async/background subagents (`AsyncSubAgent` plus task-management tools)
  exist upstream; relevant to the later pyramid only, unused this round.

## Codebase Map

- **Entry points:** none — `harness/__main__.py` does not exist and is created by Phase 3.
- **Module boundaries (existing):** `harness/config.py` (TOML+env load, pydantic models,
  `ConfigError`), `harness/sources.py` (`SourceRegistry`, `normalize_url`),
  `harness/prompts.py` (`render`, `required_variables`, `PromptError`), `harness/tools/`
  (`build_tools`, `fetch.py`, `search.py`).
- **Reuse targets:**
  - `build_tools(config: HarnessConfig, registry: SourceRegistry) -> list[BaseTool]` —
    `harness/tools/__init__.py:11`; composes `build_fetch_tool(config, registry)`
    (`harness/tools/fetch.py:230`) and `build_search_tool(config)`
    (`harness/tools/search.py:113`). A new tool is a new module plus one line here.
  - `render(name: str, **variables: object) -> str` — `harness/prompts.py:33`;
    `required_variables(name)` at `:28`. Prompt files are `harness/prompts/<name>.md`.
  - `load_config(path: Path | None = None) -> HarnessConfig` — `harness/config.py:98`; wraps
    every failure into `ConfigError` via `_describe(exc)` at `:117`.
  - `SourceRegistry.resolve(text)` / `.unresolved_ids(text)` / `.link(id)` —
    `harness/sources.py`; currently have no consumer.
- **Existing prompt files:** `harness/prompts/orchestrator.md` (`$current_date`,
  `$research_question`, `$max_sources`) and `harness/prompts/subagent.md` (`$current_date`,
  `$task`). Both describe a hand-rolled single-agent flow with a JSON tool-call convention and
  a fixed two-tool set; neither mentions `ask_user` or interrupts.
- **Conventions:** every pydantic model sets `model_config = ConfigDict(extra="forbid")`; one
  `<Domain>Error(Exception)` per module with messages naming the offending value; `async def`
  only for I/O-bound tool bodies; library exceptions are never allowed to reach the model —
  they are classified into a typed outcome and returned as tool content/artifact.
- **Tests:** `tests/` — 64 tests across `test_config.py`, `test_fetch.py`, `test_prompts.py`,
  `test_search.py`, `test_sources.py`, `test_tools_registry.py`, plus `tests/conftest.py`
  providing a `make_config` fixture. `asyncio_mode = "auto"`, so async tests need no marker.
  External clients are faked by `monkeypatch.setattr` over the class the module imports
  (see `tests/test_fetch.py:22-69` and `tests/test_search.py:10-17`).
- **Existing config:** `harness.toml` has `[providers.opencode]` (`base_url = "TODO"`),
  `[providers.cerebras]`, `[roles.head]` (`model = "TODO"`), `[roles.subagent]`
  (`model = "TODO"`), `[browser]`, `[fetch]`, `[search]`. `pyproject.toml` pins
  `crawl4ai==0.9.2` and carries `pydantic>=2.9`, `langchain-core>=0.3`, `httpx>=0.27`; no
  `deepagents` and no `langchain-openai`. Fetch concurrency is bounded per tool call by
  `MemoryAdaptiveDispatcher(max_session_permit=config.fetch.max_concurrency)` at
  `harness/tools/fetch.py:177` — there is no run-wide bound today.
- **Gaps this plan closes (verified):** `FetchPagesInput.urls` is unbounded today
  (`harness/tools/fetch.py:227`) and per-page rendering caps at `fetch.per_page_char_cap`
  (default 12000) with no aggregate cap; `SourceRegistry` stores URL and title, never page
  content; no token-usage accounting exists anywhere in `harness/`.
- **Commands:** `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`.
- **Docs to keep current:** `docs/architecture.md` (its `## Failure Modes` is still a
  placeholder), `docs/decisions.md`, `docs/backlog.md`, `docs/guides/setup.md`,
  `docs/INDEX.md`.

## Non-Goals

Inherits every `## Intent` non-goal — not re-listed.

- No replacement of the substrate's tools beyond Phase 2's two amendments (URL cap, source
  capture): `fetch_pages` and `search_web` are otherwise consumed as they stand, including
  their known misclassifications (a 404 HTML body reads as `fetched`).
- No `general-purpose` subagent left enabled just because deepagents injects one by default.
- No use of `deepagents`' `memory=` long-term memory files or any `StoreBackend`.
- No LangSmith tracing setup, despite `langsmith` arriving as a transitive dependency.

## Design Decisions

### D1: One lead agent this round; tier contracts frozen but unwired
- **Chosen:** A single `deepagents` lead agent holding `search_web`, `fetch_pages` and
  `ask_user`. The researcher and reader tier contracts are written as prompt artifacts and
  frozen, but nothing delegates to them.
- **Rejected:** Wiring the full researcher/reader pyramid now — it stacks a 3-10x token
  multiplier that compounds per level and an unproven model client into one round with
  nothing measured (nesting itself works natively in 0.7.x, so cost, not mechanism, is the
  reason to wait). Also rejected: a role-split
  topology (`searcher` → `reader` → `synthesizer`), which decomposes by problem type and
  produces the documented telephone-game failure where a reader handed a URL does not know
  why it mattered.
- **Consequences:** The token cost recorded in Phase 3 becomes the baseline the pyramid is
  priced against. Tier 3's value is context protection, not role separation, so its contract
  must pass down the facet being supported — not a bare URL. `ask_user` must be excluded from
  both tier contracts, or a tier-3 reader will interrupt the developer mid-fan-out.

### D2: The agent writes working notes; Python assembles the report
- **Chosen:** The agent externalizes working notes to a disk-backed workspace through its
  filesystem tools. After the run returns, Python performs the claim check, calls
  `registry.resolve()`, appends the disclosure sections, and writes one timestamped file to
  the reports directory.
- **Rejected:** The agent writing the final report itself — the model would choose the
  filename, Python would have to discover it, and the claim check would then either rewrite
  the file or send the model back for a revision that can introduce fresh unsupported claims.
  Also rejected: Python assembling everything with no workspace notes — simplest data flow,
  but a run cut short by the ceiling would yield nothing, quietly voiding R7.
- **Consequences:** R4's disclosure content is harness state (fetch outcomes, unresolved IDs,
  cut-short flag) that Python holds reliably and the model does not. Report assembly is pure
  string work and therefore fully testable offline. The reports path and filename format are
  deterministic, which R1's "prints its path" depends on.

### D3: Verification is a code-orchestrated pass over isolated claims, and never adjudicates
- **Chosen:** After the draft exists, Python walks its claims and makes one model call per
  claim carrying only that claim and its own cited source. Conflicting sources are surfaced
  with both positions and their IDs. The harness never decides which source is right.
- **Rejected:** A verifier agent or a verification tier — measured false-conclusion adoption
  runs 34.5% mid-research to 85% immediately before synthesis, and every tested defense
  (pre-research prompting, post-research refinement, both) leaves 15-62% residual. Crucially,
  verifier models reliably catch misleading claims in *focused* contexts and miss the same
  claims when verification is embedded in a long-horizon workflow. A peer agent inherits that
  workflow failure; an isolated per-claim call reproduces the context where verification
  works. Also rejected: source reputability scoring — high-authority misleading sources are
  adopted at 54% versus 8% for low-authority ones, so scoring authority amplifies the failure.
- **Consequences:** Verification is deterministic Python orchestration, not model-driven
  delegation, so its cost is predictable and its behavior testable with a faked model. A
  report states disagreement rather than resolving it, which is the only honest option for a
  non-technical reader. This also catches the substrate's known 404-as-`fetched`
  misclassification, since an error page cannot support the claim attached to it.

### D4: Claim checks run sequentially on `[roles.head]`
- **Chosen:** Verification is a sequential loop — one model call per claim, one at a time, on
  the head role, the same strong model as the lead.
- **Rejected:** A concurrent fan-out with its own `[agent]` concurrency cap — cuts minutes off
  the post-run wait, but reintroduces a config surface, concurrent-429 retry handling, and a
  registry race, for a wait the developer accepts (~1-5 minutes at ~30 claims against a
  10-15 minute research loop). Reusing `[roles.subagent]` (the cheap worker) — cheaper, but a
  weak verifier produces both false "unsupported" flags that make reports noisy and false
  "supported" ones that defeat the purpose. A new `[roles.checker]` entry — additive and
  independently swappable, but an unused config surface until someone wants a third model.
- **Consequences:** No verification concurrency setting exists anywhere. R6's retry only ever
  faces serial 429s, so bounded backoff stays simple. Verification runs after the agent loop
  and outside the R7 wall clock — claim count bounds it naturally (!#5). If real reports grow
  enough claims to hurt, bounded concurrency is the recorded fallback (backlog entry first),
  never unbounded fan-out.

### D5: Clarification via `interrupt_on` + `respond`, in-memory checkpointer, pre-research only
- **Chosen:** An `ask_user` tool registered as `interrupt_on={"ask_user": ...}` with
  `allowed_decisions` limited to `respond`, over an `InMemorySaver` checkpointer and a
  per-run `thread_id`. The lead prompt confines asking to a pre-research window.
- **Rejected:** A tool calling `interrupt()` directly — documented as outside the supported
  middleware pattern. Durable SQLite checkpointing — it would enable cross-invocation
  follow-ups but is a database in the literal sense, and it makes D4's per-run `[Sn]` scope
  inconsistent with a resumable thread. Asking at any point during a run — more powerful, but
  it creates interrupt contention once the pyramid exists and lets a run stall repeatedly.
- **Consequences:** `__main__` is a resume loop, not a straight line: invoke, and while the
  result carries an interrupt, print the question, read the answer, resume. ~~The wall clock
  keeps running during that wait, which is what makes an unattended run self-terminate
  instead of blocking forever.~~ (see `## Reconciliations` 2026-08-10 — Phase 5: the clock
  does not start until research does, so the pre-research wait is unbounded.) No
  approval-gate interrupts are enabled on any other tool.

### D6: Disk-backed backend, confined by `permissions`; custom backend if none ships
- **Chosen:** A disk-backed backend with `permissions` restricting writes to a workspace
  directory and a reports directory, both config-declared. Phase 3's smoke check verifies
  against the installed package whether 0.7.x ships a shell-free disk backend; if not
  (current docs list none named `FilesystemBackend` — !#8), a minimal custom backend
  implementing the backend protocol with plain disk writes is written in `harness/agent.py`.
- **Rejected:** The default `StateBackend` — in-memory, so notes evaporate at process exit and
  a cut-short run has nothing to assemble from. `LocalShellBackend` and the sandbox backends —
  ~~they carry a shell `execute` tool, which violates the project's no-shell-in-the-registry
  invariant outright~~ they make the always-bound `execute` tool actually functional, which
  violates the project's no-shell invariant outright (see `## Reconciliations` 2026-08-09 —
  Phase 3). `CompositeBackend` routing scratch to state and reports to disk — the
  cleanest separation, but a third configuration to get right for no present benefit.
- **Consequences:** This is the first code satisfying the workspace invariant. Paths are
  config values, never literals. Test runs must be pointed at a temporary directory. If the
  custom backend is needed, it is deliberately minimal — read/write/list confined to the two
  configured roots, nothing else.

### D7: Context handling is configured middleware, not new code
- **Chosen:** Within-run history, subagent isolation, compression, and file externalization
  are all deepagents middleware that get *configured and tested*, not reimplemented. The
  `SummarizationMiddleware` `keep` policy must preserve source attribution.
- **Rejected:** A bespoke context layer or a custom digest/`read_source` tool — it would hand-
  build what `FilesystemMiddleware` and subagent context isolation already provide, which is
  the same reasoning that removed the digest tier from this round's scope.
- **Consequences:** Compression before synthesis is exactly the 85% false-conclusion regime
  (see D3). If a summary drops which `[Sn]` supported which finding, the lead synthesizes from
  unattributed assertions and the claim check has nothing left to check against — and it would
  look fine. R3's compression case is therefore a real test, not a formality.

### D8: The existing prompts are rewritten in place
- **Chosen:** `orchestrator.md` is rewritten as the deepagents lead prompt; `subagent.md` is
  rewritten as the researcher tier contract; a new `reader.md` carries tier 3.
- **Rejected:** New `lead.md`/`researcher.md`/`reader.md` files with the old two deleted —
  clearer names, but it deletes the sibling plan's Phase 5 deliverable. Leaving the old two in
  place beside new files — zero collision, but it strands two stale prompts that a future
  session will mistake for current ones, which is corrosive in a project that treats docs as
  context.
- **Consequences:** Both existing files must be rewritten rather than extended: they encode a
  hand-rolled JSON tool-call convention that fights deepagents' native tool calling. The
  prompt loader and filenames are reused unchanged. Phase 3 must confirm how deepagents
  composes `system_prompt` with its own injected middleware prompts before assuming rendered
  output arrives intact. The rewritten lead prompt carries a reflection rule — after each
  search or fetch result, assess relevance and coverage before the next action — chosen as
  prompt-level instruction over a no-op `think_tool` (one more tool module for a behavior the
  planning middleware already paces); upgrade to the tool only if real runs show the model
  rushing past results.

### D9: `TodoListMiddleware` is opted in; the todo state is the run's progress surface
- **Chosen:** `build_agent` enables `TodoListMiddleware` explicitly (opt-in since 0.7.0). The
  lead prompt instructs the agent to write its research plan as todos before searching and
  keep them current; `__main__` echoes todo updates to the terminal as the run progresses.
- **Rejected:** No planning middleware — the lead loses the externalized plan that survives
  compression (Anthropic's lead researcher persists its plan for exactly this reason), and a
  cut-short run cannot say which planned facets were never covered. A workspace plan file
  maintained by prompt alone — unstructured, invisible to a future UI, and duplicates what
  the middleware already provides.
- **Consequences:** R10's observable surface is the todo state streamed from the run — a
  later UI reads the same thing the terminal echo does. The cut-short report (Phase 5) can
  name planned-but-unvisited steps. D7's compression `keep` policy must preserve todo state
  alongside source attribution.

### D10: The fetch tool captures source content to per-source workspace files
- **Chosen:** `fetch_pages` writes each source's full extracted text (untruncated, unlike the
  capped model-visible render) to ~~`<workspace_dir>/sources/S<n>.md`~~
  `<workspace_dir>/sources/<run_id>/S<n>.md` (see `## Reconciliations` 2026-08-12 — Phase 6)
  with a URL and title header, at fetch time. A non-`fetched` outcome writes a stub whose first line names the
  failure. Verification (Phase 6) reads only these files.
- **Rejected:** Extending `SourceRegistry` with content fields — content dies with the
  process (nothing left for a cut-short run), and it grows a substrate seam the sibling plan
  froze. Refetching at verification time — nondeterministic (the page may have changed or
  died since the run), doubles fetch load, and re-runs the rate-limit gauntlet after the
  research is already done.
- **Consequences:** The claim check is offline and deterministic once the run ends. A claim
  citing a stubbed source is `unverifiable` by construction — the 404-as-`fetched`
  misclassification is caught here even though `classify()` stays untouched. Re-fetching the
  same URL overwrites the same file (the registry dedups IDs by normalized URL, and titles
  are first-write-wins).

### D11: `fetch_pages` is bounded per call by config
- **Chosen:** `FetchPagesInput` rejects more than `fetch.max_urls_per_call` URLs (default 4),
  bound into the schema at tool-build time the same way `search`'s `default_max_results`
  already is.
- **Rejected:** Prompt-level guidance alone ("use at most N sources") — advisory and
  unenforced; one eager call fetching 20 URLs returns ~60k tokens in a single turn and blows
  the context before the summarizer's trigger ever fires. An aggregate character cap across
  the batch — a second knob measuring the same risk; URL count times the existing per-page
  cap already bounds the worst case (~12k tokens at defaults).
- **Consequences:** The agent fetches in batches of at most 4 and simply makes another call
  when it wants more. Tool-level validation makes the bound testable offline and independent
  of prompt quality.

## Requirements Coverage

| ID | Requirement | MoSCoW | Covered by |
|----|-------------|--------|------------|
| R1 | One command → timestamped cited report on disk, path printed | MUST | Phase 3 (report written, path printed), Phase 6 (citations resolved) |
| R2 | Pre-research clarifying questions, answered and resumed | MUST | Phase 4 |
| R3 | No overstated evidence; per-claim check against its own source | MUST | Phase 6 (check + marking), Phase 3 (compression config) |
| R4 | Discloses conflicts, failed fetches, dead branches, cut-short runs | MUST | Phase 6 (conflicts + gaps), Phase 5 (cut-short) |
| R5 | Researcher and reader tier contracts frozen as artifacts | MUST | Phase 7 |
| R6 | Roles resolve to chat clients; fail-fast incl. `TODO` placeholders and an unreachable endpoint; bounded retry, no switchover | MUST | Phase 1 (config fail-fast + `preflight`), Phase 3 (`preflight` call site in `__main__`) |
| R7 | Round cap + 30-minute wall clock; token cost recorded | MUST | Phase 5 (ceiling), Phase 3 (token baseline) |
| R8 | Source content captured at fetch time; verification reads capture only | MUST | Phase 2 (capture), Phase 6 (consumption) |
| R9 | Fetch call bounded to configured source count (default 4) | MUST | Phase 2 |
| R10 | Research plan visible mid-run, echoed at terminal, survives compression | MUST | Phase 3 |

## Progress

- [x] Phase 1: Model client and agent config
- [x] Phase 2: Fetch amendments — URL cap and source capture
- [x] Phase 3: Tracer bullet — question in, report on disk
- [x] Phase 4: Pre-research clarification
- [x] Phase 5: Run ceiling and cut-short reporting
- [x] Phase 6: Claim verification and disclosure
- [x] Phase 7: Researcher and reader tier contracts
- [ ] Final verification

## Phases

### Phase 1: Model client and agent config

**Risk:** flagged (!#5, !#7)
**Test-first:** required
**Goal:** Resolve a config role to a working chat client on the OpenCode endpoint with bounded
retry already applied, failing loud and specific before any research starts.
**Requirements:** R6
**Assumes:**
- `uv sync` works and the substrate's 64 tests are green.
- The developer supplies real values for `harness.toml`'s three `TODO` placeholders.
**Diff budget:** ~170-240 lines across 7 files.

**Files:**
- `pyproject.toml` — modify: add `deepagents` (pinned exact on the 0.7.x line — 0.7.5 at
  planning time) and `langchain-openai`.
- `harness/config.py` — modify: add an `AgentSettings` model and its field on `HarnessConfig`.
- `harness.toml` — modify: add the `[agent]` section; fill the three `TODO` placeholders.
- `harness/models.py` — new: role → chat client resolution, retry, and the `preflight`
  reachability check. Reason: R6 is a distinct concern that every later phase depends on and
  that is independently live-checkable.
- `tests/test_models.py` — new.
- `tests/test_config.py` — modify: cover the new `[agent]` section.
- `docs/guides/setup.md` — modify: add the manual live-check command for a single model call.

**Reuse:**
- Extend `HarnessConfig` in `harness/config.py` — do NOT create a second config loader or a
  separate settings module.
- Read every limit from `AgentSettings` — do NOT introduce literals for retry counts,
  concurrency, or timeouts.
- Pattern to mirror: `harness/config.py` — `ConfigDict(extra="forbid")`, one `<Domain>Error`
  per module, messages naming the offending value; and `tests/conftest.py`'s `make_config`
  fixture for building config in-memory without a TOML file.

**Contracts:**
- `harness/models.py`:
  - `class ModelError(Exception)`
  - `build_chat_model(config: HarnessConfig, role: str) -> BaseChatModel` — raises `ModelError`
    naming both the role and its provider when the role is undeclared, the provider is
    undeclared, the API key is absent, or the role's model or its provider's `base_url` is
    the literal string `TODO` (R6's fail-fast covers unfilled placeholders). The returned
    client has retry **already applied**; callers must not wrap it again.
  - `async def preflight(config: HarnessConfig, role: str) -> None` — R6's reachability half
    (added during execution; see `## Discoveries` 2026-08-09). Builds via `build_chat_model`,
    makes one completion capped at a single token, returns `None` on success, and raises
    `ModelError` naming the role, provider, `base_url` and model — distinguishing unreachable
    endpoint from rejected credentials from unknown model — so no `openai`/`httpx` exception
    ever escapes. Unconditional, no config knob. Phase 3's `__main__` is its call site.
- `harness.toml` section name `[agent]`, frozen for later phases, carrying at minimum: the
  round cap, the wall-clock budget in seconds (default 1800), the workspace directory, and
  the reports directory.

**Out of scope:**
- No agent construction, no `deepagents` call, no tools, no prompts — Phase 3.
- No model or provider switchover, and no fallback role (Intent non-goal).
- Do not change `ProviderConfig`'s shape; the deferred `SecretStr` question in the substrate
  plan's `## Discoveries` stays deferred.
- Do not touch `harness/tools/`, `harness/sources.py`, or `harness/prompts.py`.

**Tests (write first, confirm red):**
- [x] A valid config resolves each declared role to a client carrying that role's model ID and
      its provider's base URL.
- [x] Every failure mode raises `ModelError` with a message naming the offending role and
      provider: unknown role, role pointing at an undeclared provider, absent API key, and a
      literal `TODO` left in the role's model or the provider's `base_url`.
- [x] The `[agent]` section loads, and each omitted value falls back to its documented default
      including the 1800-second wall clock.
- [x] A transient failure is retried up to the configured bound and then surfaces; a
      non-transient failure is not retried.
- [x] `preflight` succeeds against a reachable endpoint and raises `ModelError` — never a
      library exception — for an unreachable endpoint, rejected credentials, and an unknown
      model, each distinguishable from the others; it inherits the client's bounded retry, and
      its request is capped to a single completion token.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the two dependencies and `uv sync`; record the resolved `deepagents` version.
3. Inspect the **installed** `deepagents` package and record which middleware
   `create_deep_agent` enables by default and what its backend defaults to — confirming the
   0.7.x expectations in `## Background` (`TodoListMiddleware` opt-in, `StateBackend`
   default) and surfacing any drift (!#2). Append the finding to `docs/decisions.md`.
4. Implement `AgentSettings` and `build_chat_model` per **Contracts**.
5. Run the tests; confirm they PASS (green).
6. Add the live-check command to `docs/guides/setup.md`.

**Acceptance criteria:**
- [x] Manual live check: one call through `build_chat_model(config, "head")` reaches Kimi K3
      on OpenCode and returns text; ~~then with the key unset, the same call raises
      `ModelError` naming the role and provider rather than a library traceback.~~ (See
      `## Reconciliations` 2026-08-09.) **Run 2026-08-09: PASSED** — `preflight` returns clean
      for both `head` and `subagent`, and the head role answered in text.
- [x] `docs/decisions.md` records the pinned `deepagents` version and the observed default
      middleware set and backend, as read from the installed package.
- [x] `harness.toml` contains no `TODO` values.

### Phase 2: Fetch amendments — URL cap and source capture

**Risk:** none
**Test-first:** required
**Goal:** Bound a single `fetch_pages` call to a configured number of URLs, and capture every
consulted source — full extracted text or an explicit failure stub — as a per-source
workspace file that verification later reads offline.
**Requirements:** R8 (capture), R9 (cap)
**Assumes:**
- Phase 1's `[agent]` section exists (it declares the workspace directory).
**Diff budget:** ~90-140 lines across 5 files.

**Files:**
- `harness/config.py` — modify: `max_urls_per_call` on `FetchSettings` (default 4).
- `harness.toml` — modify: add the setting to `[fetch]`.
- `harness/tools/fetch.py` — modify: enforce the cap in `FetchPagesInput`; write the source
  files.
- `tests/test_fetch.py` — modify; `tests/test_config.py` — modify.

**Reuse:**
- Pattern to mirror: `harness/tools/search.py:122-125` — a config value bound into the input
  schema at tool-build time.
- Source file names derive from the ID `SourceRegistry.add()` returns — do NOT mint or parse
  IDs any other way (substrate D4).
- The existing `classify()` outcomes drive stub content — do NOT widen the classifier itself
  (D10).

**Contracts:**
- Source file path, ~~frozen: `<workspace_dir>/sources/S<n>.md`~~ amended to
  `<workspace_dir>/sources/<run_id>/S<n>.md` (see `## Reconciliations` 2026-08-12 —
  Phase 6) — a URL and title header, then
  the full extracted text (untruncated; the model-visible render stays capped). A
  non-`fetched` outcome writes a stub whose first line names the outcome (e.g.
  `FETCH FAILED: blocked`). Phase 6 treats any stub as unusable.
- `FetchPagesInput` rejects a `urls` list longer than `fetch.max_urls_per_call`.

**Out of scope:**
- No change to `classify()` — 404-as-`fetched` stays; the claim check is the mitigation.
- No changes to `search.py`, no run-wide concurrency bound, no dedup or caching.
- Nothing reads these files yet — Phase 6 is the consumer.

**Tests (write first, confirm red):**
- [x] A fetch of more URLs than the cap is rejected by input validation; at the cap it
      proceeds.
- [x] Each successfully fetched page writes `sources/S<n>.md` with its URL and title header
      and full extracted text, under the ID the registry assigned that URL.
- [x] A failed or blocked fetch writes a stub naming its outcome instead of content.
- [x] `max_urls_per_call` defaults to 4 and loads from `[fetch]` when overridden.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the setting, the schema bound, and the source-file writes.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] The substrate's pre-existing tests still pass unmodified, except any that constructed
      an over-cap fetch. (None did — no pre-existing test fetched more than 4 URLs, so nothing
      needed changing; `conftest.py` and `test_config.py` were extended additively.)

### Phase 3: Tracer bullet — question in, report on disk

**Risk:** flagged (!#1, !#2, !#3, !#8)
**Test-first:** required
**Goal:** One command drives a single deepagents lead agent over the existing tools and writes
a timestamped markdown report to disk, printing its path — proving the framework seams before
anything is built on them.
**Requirements:** R1, R7 (token baseline), R3 (compression config), R10
**Assumes:**
- Phase 1's `build_chat_model` and `[agent]` config exist; Phase 2's fetch amendments are in.
- SearXNG is reachable and the Playwright backend works, per the substrate's live checks.
**Diff budget:** ~300-420 lines across 8 files.

**Files:**
- `harness/agent.py` — new: deepagents construction — model, tools, prompt, middleware,
  disk-backed backend, permissions. Reason: the framework wiring is one coherent concern and
  the only place `deepagents` is imported.
- `harness/report.py` — new: report assembly and the timestamped write. Reason: pure string
  work, kept separate so R3/R4 assembly tests need no model.
- `harness/__main__.py` — new: argv, `await preflight(config, "head")` before anything is
  spent, run, print path. Reason: R1 needs an entry point and none exists, and `preflight` is
  R6's "before any research starts" guard (see `## Discoveries` 2026-08-09).
- `harness/prompts/orchestrator.md` — modify: rewritten as the deepagents lead prompt (D8).
- `tests/test_agent.py` — new.
- `tests/test_report.py` — new.
- `docs/guides/setup.md` — modify: add the end-to-end live-check command.
- `docs/decisions.md` — modify: record whether deepagents accepted the substrate's async tools
  and how it composed our `system_prompt` with its own.

**Reuse:**
- Call `build_tools(config, registry)` from `harness/tools/__init__.py` once per run and pass
  the result to the agent — do NOT construct tools individually and do NOT create a second
  registry (substrate D8).
- Call `render("orchestrator", ...)` from `harness/prompts.py` — do NOT inline the prompt text
  or add a second template mechanism.
- Call `build_chat_model(config, "head")` from Phase 1 — do NOT construct a chat model here.
- Pattern to mirror: `harness/tools/search.py` for module shape and for classifying failures
  into typed values instead of raising.

**Contracts:**
- `harness/agent.py`:
  - `build_agent(config: HarnessConfig, registry: SourceRegistry) -> Runnable` — the compiled
    lead agent, driven with `ainvoke` (substrate D1).
- `harness/report.py`:
  - `class RunOutcome(BaseModel)` — the seam between a finished run and report assembly.
    Phase 5 extends it with cut-short state and Phase 6 with verification results; later
    phases add fields rather than reshaping it.
  - `write_report(outcome: RunOutcome, config: HarnessConfig) -> Path`
  - Report filename format, frozen: `<reports_dir>/YYYY-MM-DD-HHMMSS-<slug>.md`.
- `python -m harness "<question>"` prints the report path as the final line of stdout, frozen
  because R1 depends on it.

**Out of scope:**
- No `ask_user`, no interrupts, no checkpointer — Phase 4.
- No round cap and no wall clock — Phase 5.
- No claim checking, no conflict surfacing, no disclosure sections — Phase 6.
- No subagents, and the injected `general-purpose` subagent is disabled rather than used.
- Do not modify `harness/prompts/subagent.md` — Phase 7 owns it.
- Do not change the substrate's tools to suit the agent; if a tool proves incompatible,
  surface it rather than editing `fetch.py` or `search.py`.

**Tests (write first, confirm red):**
- [x] With a faked chat model, a run produces a report file at the frozen filename format and
      the path is the last line of stdout.
- [x] A run whose tools return no usable sources still produces a report stating that, and
      exits non-error.
- [x] The agent is constructed with the config's model, the tools from `build_tools`, a
      disk-backed backend whose writes are confined to the configured workspace ~~and reports
      directories~~ (see `## Reconciliations` 2026-08-09 — Phase 3), `TodoListMiddleware`
      enabled (D9), and no `general-purpose` subagent.
- [x] Compression is configured such that source attribution survives it: a history long
      enough to trigger the summarizer still yields findings whose `[Sn]` associations are
      intact, with the todo plan state intact too.
- [x] Todo updates surface at the terminal as the run progresses (R10).
- [x] Token usage from the run is recorded on `RunOutcome`, summed from `usage_metadata` on
      the final state.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Smoke-check the three riskiest seams against the installed package before building on
   them: that `create_deep_agent` accepts the substrate's async `@tool`-built tools and can
   be driven with `ainvoke` (!#1), how it composes a supplied `system_prompt` with its own
   middleware prompts (!#3), and which shell-free disk backend it offers, if any (!#8, D6).
   Record all three in `docs/decisions.md`.
3. Rewrite `harness/prompts/orchestrator.md` as the lead prompt, dropping the JSON tool-call
   convention in favour of native tool calling, and carrying the plan-upkeep and reflection
   rules (D8, D9).
4. Implement `build_agent`, then `write_report`, then `__main__`.
5. Run the tests; confirm they PASS (green).
6. Add the live-check command to `docs/guides/setup.md` and record the observed token cost of
   one real run as the pyramid's baseline.

**Acceptance criteria:**
- [x] Manual live check: `python -m harness "<a real question>"` produces a report on disk
      whose content answers the question and cites URLs, and prints its path. **Run
      2026-08-10: PASSED** — 19 sources consulted, `[Sn]` citations resolved to links, the
      one failed fetch quarantined as not-usable, path printed last.
- [x] `docs/decisions.md` records the async-tool outcome and the prompt-composition behavior
      as observed, not as assumed. If async tools were rejected, the entry says so and names
      the workaround taken.
- [x] The observed token cost of one real run is written down as the delegation baseline
      (`docs/decisions.md`, 2026-08-10: 773,032 in / 22,883 out of which 16,539 reasoning /
      795,915 total, on `deepseek-v4-flash`).
- [x] `uv run ruff check .` and `uv run mypy .` clean for the new files.

### Phase 4: Pre-research clarification

**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** Before researching, the agent can ask the developer questions, block for typed
answers, and resume with those answers as tool results.
**Requirements:** R2
**Assumes:**
- Phase 3's agent runs end to end and `__main__` exists.
**Diff budget:** ~150-210 lines across 7 files.

**Files:**
- `harness/tools/ask_user.py` — new: the clarification tool. Reason: mirrors the existing
  one-module-per-tool layout, so it registers through `build_tools` like the others.
- `harness/tools/__init__.py` — modify: add one line to `build_tools`.
- `harness/agent.py` — modify: `interrupt_on`, the `InMemorySaver` checkpointer, and a per-run
  `thread_id`.
- `harness/__main__.py` — modify: the interrupt/resume loop.
- `harness/prompts/orchestrator.md` — modify: confine asking to the pre-research window ~~and
  bound how many questions may be asked~~ (see `## Reconciliations` 2026-08-10 — Phase 4).
- `tests/test_ask_user.py` — new; `tests/test_tools_registry.py` — modify.
- `tests/test_agent.py` — modify: added during execution, see `## Discoveries` 2026-08-10 —
  the checkpointer makes a `thread_id` mandatory on every existing `build_agent` invocation.

**Reuse:**
- Extend `build_tools` in `harness/tools/__init__.py` — do NOT create a second tool list.
- Pattern to mirror: `harness/tools/search.py`'s `build_search_tool` factory shape and its
  pydantic `args_schema`.
- Extend `harness/agent.py` and `harness/__main__.py` from Phase 3 — do NOT add a second
  entry point or a parallel agent builder.

**Contracts:**
- `harness/tools/ask_user.py`:
  - `build_ask_user_tool(config: HarnessConfig) -> BaseTool` — produces a tool named
    `ask_user`, frozen because `interrupt_on` is keyed by tool name.
- The resume protocol, frozen: the run is invoked under a stable per-run `thread_id`, and an
  interrupt is answered with `Command(resume={"decisions": [{"type": "respond", ...}]})`.
- `interrupt_on` is configured for `ask_user` only, with `allowed_decisions` limited to
  `respond`.

**Out of scope:**
- No approval, edit, or reject gates on `fetch_pages`, `search_web`, or any filesystem tool.
- No mid-research or pre-synthesis asking — pre-research only (D5).
- No durable checkpointer and no cross-invocation resume (Intent non-goal).
- No special handling for an unattended run; it blocks until the Phase 5 wall clock fires.
- Do not give `ask_user` to any subagent definition — Phase 7 freezes that exclusion.

**Tests (write first, confirm red):**
- [x] A faked model that calls `ask_user` causes the run to interrupt rather than complete, and
      the question text reaches stdout.
- [x] Supplying an answer resumes the run under the same `thread_id` and the answer arrives as
      the tool's result.
- [x] A run whose model never calls `ask_user` completes without interruption, unchanged from
      Phase 3.
- [x] `build_tools` returns `ask_user` alongside `fetch_pages` and `search_web`, with unique
      names and a non-empty description and schema each.
- [x] Interrupts are configured for `ask_user` only — a proposed `fetch_pages` call does not
      interrupt.
- [x] Added at the 3F gate, beyond the plan's five: a second clarification round asks and
      resumes again (pinning the loop, not a one-shot `if`); two `ask_user` calls in one
      interrupt get one correctly-paired answer each and the tool body never also runs; and an
      empty answer is disclosed rather than sent as silence. All three were mutation-tested.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `build_ask_user_tool` and register it in `build_tools`.
3. Add the checkpointer, `thread_id`, and `interrupt_on` to `build_agent`.
4. Implement the resume loop in `__main__`: invoke, and while an interrupt is pending, print
   the question, read an answer, resume.
5. Update `orchestrator.md` with the pre-research asking rules.
6. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] Manual live check: a deliberately ambiguous question causes at least one clarifying
      question at the terminal; answering it produces a report reflecting the answer.
      (Must be run BY the developer — it blocks on typed input at a real terminal, which no
      tool-driven run can supply.)
- [x] `uv run ruff check .` and `uv run mypy .` clean for the changed files.

### Phase 5: Run ceiling and cut-short reporting

**Risk:** flagged (!#6)
**Test-first:** required
**Goal:** Bound a run by rounds and by a 30-minute wall clock, and still produce a report from
the workspace notes when either fires.
**Requirements:** R7, R4 (cut-short disclosure)
**Assumes:**
- Phase 3's workspace notes and Phase 2's source files are being written to disk during a run.
- Phase 4's resume loop exists, since the clock spans it.
**Diff budget:** ~140-200 lines across 6 files.

**Files:**
- ~~`harness/agent.py` — modify: apply the configured round cap.~~ (see
  `## Reconciliations` 2026-08-10 — Phase 5: the cap is a `RunnableConfig` key, so it lands
  in `harness/__main__.py`; `agent.py` is untouched this phase.)
- `harness/__main__.py` — modify: enforce the wall clock across the whole run including
  clarification waits, and carry the round cap on the run config.
- `harness/report.py` — modify: assemble a report from workspace notes when a run is cut
  short, and record that it was.
- `tests/test_report.py` — modify; `tests/test_agent.py` — modify.
- `docs/guides/setup.md` — modify: document the two ceiling settings and their defaults.

**Reuse:**
- Read both bounds from `AgentSettings` (Phase 1) — do NOT introduce literals, and do NOT add
  a new config section.
- Extend `RunOutcome` in `harness/report.py` with cut-short state — do NOT introduce a second
  result type.
- Pattern to mirror: `harness/tools/fetch.py`'s classification of a partial outcome into a
  typed value rather than an exception.

**Contracts:**
- `RunOutcome` carries whether the run was cut short and by which bound; `write_report`
  renders that as an explicit disclosure section in the report.

**Out of scope:**
- No token-budget ceiling and no rate/token accounting beyond the Phase 3 usage figure.
- ~~No pausing of the clock during a clarification wait — that is the decision that lets an
  unattended run terminate (R7's recorded case).~~ (see `## Reconciliations` 2026-08-10 —
  Phase 5: reversed by the developer at this gate. The clock does not start until the first
  research tool call; a mid-run ask is bounded only by the time remaining on it.)
- No separate idle timeout on a clarification wait, and no new `[agent]` setting of any kind.
- No retry or resumption of a cut-short run.
- No changes to the tools' own per-call concurrency or timeouts.

**Tests (write first, confirm red):**
- [x] Reaching the round cap ends the run and produces a report disclosing that bound.
- [x] Exceeding the wall clock ends the run and produces a report disclosing that bound.
- [x] A cut-short report contains the findings present in the workspace notes at the time,
      rather than being empty, and names the planned todos not yet done (D9).
- [x] ~~The clock is not paused while an interrupt is pending, so an unanswered clarification
      terminates the run at the bound.~~ Amended (see `## Reconciliations` 2026-08-10 —
      Phase 5), now two tests: a pre-research clarification does NOT start the clock, so an
      unanswered one never terminates the run; and once research has started the clock is not
      paused by an interrupt, so an unanswered mid-run clarification terminates at the bound.
- [x] A run finishing inside both bounds produces a report with no cut-short disclosure.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. ~~Apply the round cap in `build_agent`.~~ Apply the round cap as `recursion_limit` on the
   run config in `__main__` (see `## Reconciliations` 2026-08-10 — Phase 5).
3. Enforce the wall clock around the whole `__main__` run, spanning the resume loop, and
   ensure the partial-report path runs on expiry rather than propagating a timeout (!#6).
4. Extend `RunOutcome` and `write_report` for cut-short assembly from notes.
5. Run the tests; confirm they PASS (green).
6. Document both settings in `docs/guides/setup.md`.

**Acceptance criteria:**
- [ ] Manual live check: with the wall clock temporarily set to a few seconds, a real run is
      cut short and still writes a report naming the bound it hit.
- [x] `uv run ruff check .` and `uv run mypy .` clean for the changed files.

### Phase 6: Claim verification and disclosure

**Risk:** flagged (!#5)
**Test-first:** required
**Goal:** Check each drafted claim against only its own captured source content, mark what
cannot be supported, surface disagreeing sources without adjudicating, and disclose every
remaining gap.
**Requirements:** R3, R4, R1 (citations resolved), R8 (consumption)
**Assumes:**
- Phase 3's `RunOutcome` and `write_report` exist.
- Phase 2's source files exist for every registered source.
**Diff budget:** ~220-300 lines across 4 files.

**Files:**
- `harness/verify.py` — new: claim extraction, the sequential per-claim loop, and conflict
  collection. Reason: the only new module that calls a model outside the agent, kept out of
  `report.py` so report assembly stays model-free and offline-testable.
- `harness/report.py` — modify: render claim markings, the conflicts section, and the gap
  disclosures; call `registry.resolve()`.
- `tests/test_verify.py` — new; `tests/test_report.py` — modify.

**Reuse:**
- Call `SourceRegistry.resolve()` and `.unresolved_ids()` from `harness/sources.py` — do NOT
  reimplement marker matching or link rendering, and do NOT mint IDs here (substrate D4).
- Call `build_chat_model(config, "head")` from Phase 1 — do NOT construct a model or add
  retry (the returned client already retries).
- Read source content from Phase 2's ~~frozen `sources/S<n>.md`~~ amended
  `sources/<run_id>/S<n>.md` path (see `## Reconciliations` 2026-08-12 — Phase 6) — do NOT
  refetch (D10).
- Pattern to mirror: `harness/tools/fetch.py`'s independent per-item outcomes, where one
  item's failure never fails the batch.

**Contracts:**
- `harness/verify.py`:
  - `class ClaimCheck(BaseModel)` — one per claim, carrying its verdict and the source ID it
    was checked against.
  - The verdict vocabulary, frozen because the report renders it and R3 is judged on it:
    `supported`, `unsupported`, `uncited`, `unresolved`, `unverifiable` (its source is a
    failure stub — R8).
- `RunOutcome` carries the claim checks and the collected conflicts; `write_report` renders
  each non-`supported` claim with a visible marker and emits a conflicts section listing both
  positions with their `[Sn]` IDs.

**Out of scope:**
- No adjudication of which conflicting source is correct, and no confidence scores derived
  from agreement counts (D3).
- No source reputability or authority scoring.
- No revision loop sending the draft back to the lead for rewriting.
- No verification of the tier prompts or of anything a subagent returns — no subagents exist.
- No refetching at verification time — captured files only (D10).
- Do not widen the substrate's fetch classifier to catch 404-as-`fetched`; the claim check is
  the mitigation.

**Tests (write first, confirm red):**
- [x] Each verdict in the frozen vocabulary is reachable and renders a visible marker in the
      report: a claim its source supports, one its source does not, one with no citation, one
      citing an unregistered ID, and one citing a source whose file is a failure stub.
- [x] Each claim is checked against only its own cited source's captured file — a check never
      receives another source's text and never triggers a fetch.
- [x] Claims are checked one at a time — verification issues no concurrent model calls (D4).
- [x] One failing check does not fail the pass; the remaining claims are still checked and
      the failure is disclosed.
- [x] Disagreeing sources produce a conflicts section naming both positions and both IDs, and
      no verdict about which is right.
- [x] Every `[Sn]` marker surviving into the report resolves to a clickable link, and any that
      cannot is reported by `unresolved_ids` and disclosed.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement claim extraction and the sequential per-claim loop over the captured source
   files in `harness/verify.py`.
3. Implement conflict collection from the loop's observations.
4. Extend `write_report` to render markers, the conflicts section, resolved links, and the gap
   disclosures.
5. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Manual live check: a real run produces a report in which at least one claim carries a
      non-`supported` marker or the report states that all claims were supported, and every
      citation in it is a working link.
- [x] `uv run ruff check .` and `uv run mypy .` clean for the new files.

### Phase 7: Researcher and reader tier contracts

**Risk:** flagged (!#4)
**Test-first:** required
**Goal:** Freeze what a researcher and a reader tier receive and must return, as versioned
prompt artifacts, so a later round wires the pyramid without renegotiating the seam.
**Requirements:** R5
**Assumes:**
- Phases 3-6 have run, so the lead's real behavior is known.
**Diff budget:** ~130-190 lines across 5 files.

**Files:**
- `harness/prompts/subagent.md` — modify: rewritten as the researcher tier contract (D8).
- `harness/prompts/reader.md` — new: the reader tier contract. Reason: R5 requires tier 3 to
  exist as a versioned artifact, and no existing file covers it.
- `tests/test_prompts.py` — modify: cover both contracts' declared variables.
- `docs/architecture.md` — modify: document the tier topology and fill the `## Failure Modes`
  placeholder from what Phases 2-5 observed.
- `docs/INDEX.md` — modify: add the new modules to Shared Resources.

**Reuse:**
- Render both through `render()` in `harness/prompts.py` — do NOT add a second template
  mechanism or a per-tier loader.
- Pattern to mirror: `harness/prompts/orchestrator.md` as rewritten in Phase 3 — same
  `$variable` convention, same native-tool-calling assumption.

**Contracts:**
- Each tier contract declares the four fields a task must carry — objective, output format,
  tools to use, task boundaries — and the fields a tier must return: findings, the `[Sn]` IDs
  they rest on, and any conflict flag. Frozen: the next round builds subagent definitions
  against exactly these names.
- The reader contract receives the facet it is supporting, never a bare URL.
- Neither contract includes `ask_user`; both state that a tier may not ask the developer
  anything.

**Out of scope:**
- No `create_deep_agent(subagents=...)` wiring, no `task` tool, no `CompiledSubAgent` — nothing
  delegates this round (Intent non-goal).
- No effort-scaling logic in code; the delegation counts live in the lead prompt as guidance.
- No tuning of any prompt for output quality — these are judged as artifacts.
- Do not modify `harness/agent.py`; adding tiers is the next round's work.

**Tests (write first, confirm red):**
- [x] Both contracts render with all declared variables supplied and leave no `$` placeholder.
- [x] A missing variable raises `PromptError` naming both the prompt and the variable.
- [x] `required_variables` reports exactly the placeholders each file declares.
- [x] Neither contract references `ask_user`.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Rewrite `subagent.md` as the researcher contract and write `reader.md`.
3. Run the tests; confirm they PASS (green).
4. Update `docs/architecture.md` — tier topology plus the observed failure modes — and
   `docs/INDEX.md`.

**Acceptance criteria:**
- [x] `docs/architecture.md` has no `To be documented` placeholder left in `## Failure Modes`,
      and its content names failure modes actually observed in Phases 3-6.
- [x] `docs/INDEX.md` Shared Resources lists `harness/models.py`, `harness/agent.py`,
      `harness/verify.py`, and `harness/report.py`.

## Verification

- [x] `uv run pytest` — all tests pass from the repo root. (232 passed, 2026-08-12.)
- [x] `uv run ruff check .` — clean.
- [x] `uv run ruff format --check .` — clean.
- [x] `uv run mypy .` — clean.
- [ ] Manual end-to-end: `python -m harness "<an ambiguous question>"` asks at least one
      clarifying question, answers it, researches, and writes a report whose citations all
      resolve, whose unsupported claims are marked, and whose disclosure section is present.
- [ ] Every source consulted in the manual end-to-end run has a
      ~~`sources/S<n>.md`~~ `sources/<run_id>/S<n>.md` file in the workspace — content or
      failure stub.
- [ ] `harness.toml` and `.env.example` between them name every setting the code reads, and no
      endpoint, model ID, or key appears as a literal in `harness/`.
- [ ] The recorded single-agent token baseline from Phase 3 is written down where the next
      round can find it.

## Notes

- The three `harness.toml` `TODO` values block every live check in this plan. Phase 1 cannot
  finish without them.
- The substrate's known misclassifications stay unfixed here by design: a 404 HTML body reads
  as `fetched`, and PDFs do not reliably land in `non_html` (see `docs/backlog.md`). The
  Phase 6 claim check over Phase 2's captured source files is the mitigation for the first;
  the second is simply out of scope.
- `deepagents` brings `langchain-anthropic`, `langchain-google-genai`, and `langsmith` as
  unused transitive dependencies. That was accepted knowingly; do not attempt to strip them.
- The next round's work, in the order this plan sets it up for: wire the researcher tier
  against Phase 7's contract, measure against Phase 3's token baseline, then decide whether
  the reader tier earns its multiplier.
- Anthropic's published effort-scaling rules — one agent for simple fact-finding at 3-10 tool
  calls, 2-4 subagents for comparisons at 10-15 calls each, 10+ for complex research — belong
  in the lead prompt when delegation is wired, not in code.

## Risks

#1. **The substrate's async-only tools are unexercised against deepagents.** Current docs
    confirm coroutine-only tools and `ainvoke` are supported (LangGraph async execution),
    but no run has proven OUR `@tool`-built async tools through `create_deep_agent`. Phase 3
    step 2 settles it against the installed package before anything depends on it. If they
    are rejected anyway, the fallback is supplying a sync `func` alongside the `coroutine`
    via `StructuredTool.from_function` — the substrate deliberately kept tool bodies thin
    enough to permit a sync wrapper. Record the outcome in `docs/decisions.md`; do not edit
    `fetch.py` or `search.py` to work around it without surfacing it first.

#2. **deepagents 0.7.x moves fast, and 0.7.0 was already a breaking minor release.** Five
    patches shipped Jul 24–Aug 6 2026, and `TodoListMiddleware` went from default to opt-in
    without a major-version bump. The pin is exact (0.7.5 at planning time); any upgrade is
    a deliberate act, verified against the installed package and recorded in
    `docs/decisions.md`. Phase 1 step 3 records the installed default middleware set and
    backend so drift stays visible; treat any mismatch with the **Contracts** here as a
    version problem to surface rather than a contract to quietly rewrite.

#3. **The existing prompts encode a convention that fights the new mechanism.**
    `orchestrator.md` and `subagent.md` assume a hand-rolled JSON tool-call convention and a
    fixed two-tool set, because they were written for a loop that no longer exists. deepagents
    calls tools natively and injects its own middleware prompts around a supplied
    `system_prompt`. Phase 3 must observe how composition actually happens before assuming
    `render()` output arrives intact, and Phase 4 must not reintroduce a JSON convention for
    `ask_user`. The failure mode is subtle: a surviving JSON instruction produces a model that
    describes tool calls in prose instead of making them, which looks like a prompt-quality
    problem rather than a mechanism conflict.

#4. **Tier contracts freeze names against a fast-moving subagent API.** Nested subagents work
    natively in 0.7.x, so the mechanism risk is gone, but the subagent dict shape the
    contracts assume (`name`/`description`/`system_prompt`/optional `tools`/`model`/...) is
    only confirmed as of 0.7.5. The contracts stay mechanism-neutral — Phase 7 freezes field
    names and obligations, not wiring — and the next round re-verifies the shape against its
    installed version before building subagent definitions on it.

#5. **Sequential verification adds minutes, not seconds, after the loop ends.** One head-model
    call per claim at ~2-10s each puts a ~30-claim report at roughly 1-5 minutes — accepted
    knowingly (D4) and outside the wall clock, so a slow verification pass cannot void R7.
    Phase 1's retry therefore only faces serial 429s: bounded backoff with jitter, not a
    fixed sleep. If real reports grow enough claims to hurt, log the observation to
    `docs/backlog.md` and take bounded concurrency as the recorded fallback — never
    unbounded fan-out.

#6. **Interrupting an in-flight graph leaves recovery dependent on notes already written.** The
    wall clock has to stop a run mid-step, and a timeout around the top-level `ainvoke`
    abandons the graph wherever it was. Whatever report the run produces then comes from
    workspace notes on disk — which is precisely why D6 rejected the in-memory backend, and
    Phase 2's source files bolster it: even sparse notes leave captured sources to disclose.
    If Phase 5 finds that notes are written too rarely for a cut-short report to be useful,
    the fix belongs in the lead prompt (instructing it to write findings as it goes), not in
    a new persistence layer.

#7. **`harness.toml` ships `TODO` in three places and the substrate does not validate them.**
    `[providers.opencode].base_url`, `[roles.head].model`, and `[roles.subagent].model` are
    literal `TODO` strings that pass validation today. Every live check from Phase 1 onward
    needs real values. Decided: Phase 1's `build_chat_model` rejects a literal `TODO` in the
    role's model or its provider's `base_url` with a `ModelError` naming it (R6) — leaving
    the frozen `ProviderConfig`/`RoleConfig` shapes untouched.

#8. ~~**No shell-free disk backend may ship in 0.7.x.**~~ **RETIRED 2026-08-09 by Phase 3's
    smoke check — `deepagents.backends.filesystem.FilesystemBackend` ships and is shell-free;
    D6's contingency is not needed.** Original text: Current docs list `StateBackend`,
    `StoreBackend`, `CompositeBackend`, `LocalShellBackend`, and sandbox backends — ~~nothing
    named `FilesystemBackend`~~, and `LocalShellBackend` is forbidden here because it carries a
    shell `execute` tool. If Phase 3's smoke check finds no shell-free disk backend in the
    installed package, D6's contingency applies: a minimal custom backend — read/write/list
    confined to the two configured roots — implemented in `harness/agent.py`, with the
    finding recorded in `docs/decisions.md`.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

2026-08-09 — Phase 1: the acceptance criterion's unset-key live check is unreachable —
`ProviderConfig`'s after-validator resolves `api_key` from the environment and raises when the
variable is unset, which `load_config` wraps as `ConfigError`, so `build_chat_model` is never
reached on that path → the struck half is replaced by: the unset-environment-variable case
surfaces as `ConfigError` naming the variable at load time (already covered by
`tests/test_config.py`), and `build_chat_model`'s defensive empty-`api_key` branch is covered
by an offline test built with `ProviderConfig.model_construct`. The `## Phases` **Contracts**
block is unchanged — all five `ModelError` failure modes are still implemented, including the
absent key. `ProviderConfig`'s shape stays frozen (Phase 1 **Out of scope**). Residue,
accepted knowingly: on the one path a real operator can actually hit, the `ConfigError` names
the provider and the environment variable but **not** the role, so R6's "naming the role and
provider" is literally satisfied only on `build_chat_model`'s defensive branch. Naming the role
there would mean teaching `ProviderConfig` about roles, which the frozen shape forbids.

2026-08-09 — Phase 3: two assumptions about deepagents' tool surface proved false against the
installed 0.7.5, both settled empirically before any code was written.

(a) **The `execute` tool is bound unconditionally, on every backend.** The plan assumed only
`LocalShellBackend` and the sandbox backends carry it (`## Background`, D6, !#8 — all struck).
`FilesystemMiddleware`'s tool table (`filesystem.py:1713`) always registers `execute`, so it
appears in the compiled graph's `ToolNode.tools_by_name` even under a plain `FilesystemBackend`.
It cannot be removed from that registry by any supported means. → **Amendment:** the invariant
is satisfied by two independent defenses instead of absence. (1) `execute` is put out of the
model's reach with `excluded_tools=frozenset({"execute"})` on a registered `HarnessProfile`, so
it never enters the schema passed to `bind_tools` and the model can never emit a call to it.
(2) `FilesystemBackend` is not a `SandboxBackendProtocol`, so even a call that somehow arrived
returns an in-band `ToolMessage(status="error", ...)` — there is no shell to reach. Phase 3
adds a test pinning both: `execute` absent from the model-visible tool schema, and the backend
not sandbox-capable. `harness/tools/` — the project's own registry, which is what
`CLAUDE.md`'s invariant names — remains shell-free and untouched.

(b) **Disabling the injected `general-purpose` subagent has no kwarg**; `subagents=[]` and
`subagents=None` are identical (`graph.py:751`). The only supported route is registering a
`HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))` under
the key deepagents derives for the model — empirically `"openai:kimi-k3"`, from
`get_model_provider()` (`ChatOpenAI` hardcodes `ls_provider="openai"` regardless of `base_url`)
plus `get_model_identifier()`. With it registered, `SubAgentMiddleware` drops out of the stack
entirely and the `task` tool disappears. → **Amendment:** `build_agent` performs this
registration, deriving the key from config rather than hardcoding it, and carries both this
and (a)'s `excluded_tools` in one `HarnessProfile`. Accepted residue, recorded knowingly: the
profile registry is **process-global and keyed by provider:model-name only**, so the
registration is not scoped to our `base_url` and would match any `ChatOpenAI(model="kimi-k3")`
in the same process. Harmless for a single-agent CLI; revisit if the pyramid ever builds two
agents wanting different profiles for the same model ID.

(c) **The agent's disk root is the workspace only, not workspace + reports.** The Phase 3 test
line said writes are confined to "the configured workspace and reports directories" (struck).
Per D2 the agent never writes the report — Python does, after the run — so the reports
directory is not reachable from the backend at all: `FilesystemBackend(root_dir=workspace_dir)`.
→ **Amendment:** confinement is to the workspace directory alone, which is strictly narrower
than the struck text, with `permissions` declared as the second layer.

2026-08-10 — Phase 4: the plan's Files line asked `orchestrator.md` to "bound how many
questions may be asked" (struck). The developer chose **no bound at all** when the placement
was put to them (prose constant vs. a new `[agent]` setting vs. none) → **Amendment:** asking
is confined to the pre-research window (D5) and no count limit is stated anywhere — not in the
prompt, not in config, not in code. R2 names no number, so no requirement breaks and there is
nothing to escalate. Accepted residue, recorded knowingly: nothing stops the lead asking a long
series of questions, each blocking the run on human input. The backstop is Phase 5's wall clock,
which by D5 keeps running through a clarification wait — so an unattended or over-asking run
self-terminates rather than hanging. Revisit only if a real run actually over-asks.

2026-08-10 — Phase 5: the round cap cannot be applied in `build_agent` (struck in **Files** and
step 2). Verified against the installed packages before any code was written: `recursion_limit`
is absent from `create_deep_agent`'s signature entirely, and is a `RunnableConfig` key consumed
by `Pregel.astream` at invocation time. The only way to bind it inside `build_agent` is
`graph.with_config(...)`, which is the recorded Phase 4 Dead End (the `RunnableBinding` it
returns breaks `tests/test_agent.py`'s `graph.nodes[...]` helpers). → **Amendment:** the cap is
set as `"recursion_limit"` on the `run_config` dict `__main__` already builds for `thread_id`,
alongside the wall clock it also owns; `harness/agent.py` is not modified this phase, and the
round-cap test lands in `tests/test_agent.py` driving the graph under that config rather than
inspecting `build_agent`'s return. No requirement is affected — R7 names a "configured cap on
research rounds", not where it is applied. `recursion_limit` counts LangGraph **supersteps**,
not model-plus-tool rounds; the developer chose at this gate to preserve the config field's
advertised meaning by mapping it as `max_rounds * 2 + 1` (one round = model call + tool
execution, plus the final tool-free answer turn), so the default 20 rounds becomes 41
supersteps. `AgentSettings.max_rounds` keeps its name and default.

2026-08-10 — Phase 5: R7's sub-bullet, D5's Consequences and this phase's **Out of scope** all
said the wall clock keeps running through a clarification wait, so an unattended run always
self-terminates (all three struck). Put to the developer at this gate, they reversed it: an
initial clarifying question may take as long as it takes. → **Amendment:** the wall clock starts
at the **first `search_web` or `fetch_pages` tool call** — the observable form of R2's
"pre-research window" — and from then on runs continuously, including through any later
clarification wait. Consequences, accepted knowingly: (1) a run launched and then abandoned
before the first question is answered waits indefinitely rather than self-terminating, which is
the R7 case being given up — acceptable because the sole operator drives runs by hand over SSH
and Ctrl-C works; (2) the Phase 4 reconciliation's recorded backstop for an unbounded number of
clarifying questions no longer applies to pre-research asks, which is the whole window asking is
supposed to happen in — so nothing bounds an over-asking lead except the operator; (3) a
contract-violating mid-run ask is bounded only by the time left on the clock, deliberately, with
no second timeout and no new setting. R7's other half — the ceiling still yields a report from
whatever was gathered — is unaffected.

2026-08-12 — Phase 6: D10's frozen source path `<workspace_dir>/sources/S<n>.md` collides
across runs → amended to `<workspace_dir>/sources/<run_id>/S<n>.md`. `[Sn]` IDs are minted
per-run (substrate D4), but `agent.workspace_dir` is one shared directory reused by every run,
so a run that fetches fewer sources than its predecessor leaves the predecessor's `S<n>.md`
readable under an ID it never fetched. Phase 5 flagged this as its Watch-next precisely
because Phase 6 verification reads those files: a claim would be checked against a *different
run's page* and returned `supported`, which is the exact class of silent overstatement R3
exists to prevent. Phase 5 already solved the same collision for workspace notes with an
mtime filter (`_MTIME_TOLERANCE_SECONDS` in `harness/report.py`); the developer chose
structural separation over a second mtime filter at this gate, so collisions become
impossible rather than detected after the fact, and each run's captured pages survive for
debugging instead of being overwritten.

Mechanism (developer-selected at this gate over two alternatives): `SourceRegistry` gains a
`run_id` attribute defaulting to a construction-time `%Y-%m-%d-%H%M%S` stamp, and
`_sources_dir` takes `(config, registry)`. The registry is already the per-run object and
already reaches all three consumers — `fetch.py` receives it from `build_tools`, `report.py`
carries it on `RunOutcome`, and `verify.py` will hold it — so no new parameter is threaded
through `build_tools`/`build_agent` and substrate D8's signature is untouched. The default is
a fresh stamp rather than a shared fallback so an omitted `run_id` still cannot collide;
`__main__` passes the run's own `started_at` stamp. Note the sources dir and the report
filename do NOT carry the same stamp — `run_id` is the run's START, while `write_report`
stamps the filename at WRITE time — so on a long run they differ by the run's duration.
Aligning them would mean changing Phase 3's frozen filename source, which is not worth fresh
drift; pair a report with its sources dir by ordering, not by string equality. Rejected: a
required `run_id` (most
explicit, but ~40 mechanical test-site edits blow the phase's diff budget) and threading
`run_id` through `build_tools` (disturbs substrate D8's frozen signature and sends run
identity down two paths instead of one).

Consequences: Phase 2's **Contracts** path and its already-passed acceptance criterion, D10's
**Chosen** path, Phase 6's **Reuse** line, and the plan's `## Verification` bullet are struck
and amended in place. No requirement text changes — R8's obligation is *that* content is
captured and read from capture, not where it sits — so this does not escalate to
REQUIREMENTS. Old flat `sources/S<n>.md` files from Phases 2-5 runs are orphaned, not
migrated; they are disposable run artifacts.

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

2026-08-12 — Phase 5: the `max_rounds * 2 + 1` mapping chosen at the Phase 5 gate rests on a
premise that measured FALSE. Its stated reasoning — one round = a model call plus a tool
execution = 2 supersteps, plus the final answer turn — accounts only for the marginal cost.
Measured against the installed deepagents by binary-searching `recursion_limit` against runs
doing exactly 1, 2 and 3 tool rounds: 1 round needs a limit of 9, 2 needs 13, 3 needs 15. The
marginal cost is indeed ~2 supersteps per round, but the compiled graph carries a fixed ~7-9
superstep middleware overhead on top of it, so `max_rounds=20` → limit 41 buys roughly 16
rounds, not 20. → **kept as-is, knowingly**: the error is conservative (the cap bites earlier
than advertised, never later), R7 asks for a bound rather than an exact round count, and
fitting the arithmetic to `2N + 9` would hard-code one deepagents version's node layout
against a dependency whose patch cadence !#2 already flags as fast. Recorded instead in three
places a reader will hit: the `run_config` comment, the `[agent]` bullet in
docs/guides/setup.md, and `tests/test_agent.py::test_max_rounds_scales_the_recursion_limit`,
which pins the measured boundary (cut short at `max_rounds=3`, completes at 4) so a
deepagents upgrade that changes the overhead turns a test red rather than silently
re-scaling the ceiling.

2026-08-12 — Phase 5: `recursion_limit` is applied per INVOCATION, not per run — langgraph
sets `stop = resumed_step + recursion_limit + 1` (`langgraph/pregel/_loop.py`), so every
clarification resume grants a fresh allowance (3F judgment review, Minor). With the number of
clarifying questions deliberately unbounded (Phase 4 Reconciliation) and pre-research asks now
unclocked (Phase 5 Reconciliation), R7's "a run cannot spiral" holds per pass rather than per
run. → **deferred**: the wall clock is the run-level bound once research starts, and every
extra allowance costs a human answering a question at the terminal, so there is no unattended
spiral. Revisit if a real run ever burns rounds across many resumes.

2026-08-12 — Phase 7: the phase's four named tests cover placeholders, rendering and the
`ask_user` exclusion, but nothing guards the contract FIELD names that R5 calls frozen and
that the next round builds subagent definitions against — a later edit could drop "conflicts"
from either contract with every test still green. → **acted now**: added
`tests/test_prompts.py::test_tier_contracts_name_their_frozen_fields`, parametrized over both
contracts and all seven fields. Anchored to the bolded bullet (`**Tools**`), not the bare word,
because the 3F review showed the first version could not fail — "tools" also matches the
`# Tools` heading, "boundaries" matches `# Standing boundaries`, and so on, so a renamed field
still passed. Generalise: a presence assertion over prose needs an anchor the prose does not
supply incidentally.

2026-08-10 — Phase 5: `_read_answer`'s `asyncio.to_thread(input, prompt)` makes the wall clock
unable to actually end a run. Probed before any code was written: with `asyncio.wait_for` around
`asyncio.to_thread` on a blocking call, the timeout fires on schedule but `asyncio.run()` then
blocks at interpreter shutdown joining the non-daemon executor worker — the probe returned at
30s, not the 1s timeout. With a real `input()` that wait is unbounded, so the run would print its
cut-short report and then hang at an already-dead `> ` prompt until someone typed. The same
probe on a daemon thread feeding an `asyncio.Future` returned at 1.0s. This does not contradict
Phase 4's recorded decision, whose stated reason was "so Phase 5's clock can still fire" — the
daemon thread serves that intent and is the only shape where it holds. → **acted now**:
`_read_answer` moves to a daemon thread; `harness/__main__.py` was already in this phase's Files
list. Still required under the amended clock (see `## Reconciliations` 2026-08-10 — Phase 5),
because the clock is armed during any mid-run ask.

2026-08-10 — Phase 4: Phase 4's Files list omits `tests/test_agent.py`, but adding D5's
`InMemorySaver` checkpointer to `build_agent` makes a `thread_id` mandatory on EVERY
invocation — a checkpointer-compiled graph raises `ValueError: Checkpointer requires one or
more of the following 'configurable' keys: thread_id, checkpoint_ns, checkpoint_id` (verified
against the installed langgraph before any code was written). Every existing `build_agent`
test invoked without a config, so all of them break. The alternative that would have spared
those tests — returning `graph.with_config({"configurable": {"thread_id": ...}})` from
`build_agent` — was rejected: the resulting `RunnableBinding` breaks `test_agent.py`'s
`graph.nodes["tools"]` helpers, trading a mechanical edit for a structural one, and it hides
the thread from the caller that has to resume under it. → **acted now**: the existing
invocations take an explicit `thread_id` config, which is also the shape production uses, and
the Files list above gained the file. No assertion was weakened to accommodate it.

2026-08-10 — Phase 4: `build_agent` now compiles with `InMemorySaver()`, which retains a
checkpoint per superstep for the whole run. Phase 3's baseline run carried 773k input tokens of
message state, so the retained checkpoints are not free, and D5's Consequences say nothing about
the memory growth (3F judgment review, Minor) → **deferred to Phase 5**, which owns the run
ceiling and is where a bound or a note belongs. Nothing observed yet: the Phase 3 baseline run
predates the checkpointer, so there is no measurement — take one during Phase 5's live check
before deciding whether it needs anything beyond a documented consequence.

2026-08-09 — Phase 1: removing `[providers.cerebras]` from `harness.toml` left stale references
in `.env.example`, `docs/guides/setup.md`, `CLAUDE.md` and `docs/INDEX.md`, all describing a
provider the runtime config no longer declares → **acted now**: all four pruned in Phase 1.
`tests/test_config.py` keeps its own two-provider TOML fixtures and was deliberately NOT
pruned — they are the only remaining coverage of the multi-provider path.

2026-08-09 — Phase 1: R6's "unreachable endpoint fails … before any research starts" was
covered by nothing — `build_chat_model` validates config shape only, so a well-formed but
wrong `base_url` or model ID surfaced mid-run as a raw `openai.APIConnectionError`, the library
traceback R6 exists to prevent (3F judgment review, Major) → **acted now**: added
`async def preflight(config, role)` to `harness/models.py` (see Phase 1 **Contracts**), called
from `__main__` in Phase 3 (see Phase 3 **Files**), with the `## Requirements Coverage` R6 row
amended to name both phases.

2026-08-09 — Phase 1: `AgentSettings` places `request_timeout_seconds` and `max_retries` under
`[agent]`, making them global rather than per-provider, so a second provider needing a
different timeout would require a reshape (3F judgment review, noted not filed) → **deferred**:
correct while exactly one provider is declared; revisit if the pyramid reintroduces a second.

2026-08-09 — Phase 1: no test pins the *jitter* property of R6's bounded backoff — swapping the
SDK's backoff for a hand-rolled fixed sleep would keep every retry test green, since the tests
assert attempt counts and terminal error types (3F judgment review, noted not filed) →
**deferred**: the guarantee rests on the pinned `openai` SDK plus a code comment, which is an
acceptable home for it; revisit only if retry is ever hand-rolled.

2026-08-09 — Phase 3: a mid-run model connection failure propagates as a raw library traceback
and yields NO report, even though the run had already written its todo plan and captured three
sources to disk. Observed live: the first end-to-end check died on a transient DNS failure
(`getaddrinfo failed` → `openai.APIConnectionError`) after the client's bounded retry was
exhausted, and `__main__` let it escape. R6's fail-fast covers *startup* via `preflight`, and
Phase 3's **Out of scope** assigns the ceiling and the cut-short report to Phase 5 — so this is
not a Phase 3 contradiction, but it is the same failure shape !#6 describes and the same
disclosure obligation R7/R4 carry. → **deferred to Phase 5**, whose step 3 already owns "ensure
the partial-report path runs on expiry rather than propagating a timeout": that path must be
entered on ANY mid-run termination, not only on wall-clock expiry, and `__main__` must convert
a terminal model failure into a plain stderr message plus a cut-short report rather than a
traceback. Evidence that a useful partial report was possible: the todo plan and three captured
`sources/S<n>.md` files were already on disk when it died.

2026-08-12 — Phase 6: the per-claim check needs a model prompt, and the phase's **Files** list
names only `harness/verify.py`, `harness/report.py` and the two test files — no prompt file.
Not a contradiction (the plan simply never says where the prompt lives), so this is a
Discovery, not Drift. → **acted on now**: a new `harness/prompts/verify.md` rendered through
the existing `render()` loader, matching `orchestrator.md` and `subagent.md`. Chosen over a
module-level template string in `verify.py` because it would otherwise be the only model
prompt in the codebase outside `harness/prompts/`, and Phase 7 is about to freeze two more
prompts as artifacts — the odd one out would read as an oversight to a cold session. The
phase's Files list is therefore one file longer than written.

2026-08-12 — Phase 6: the plan says verification "walks its claims" but never defines a claim.
→ **decided at the 3C gate**: a claim is a SENTENCE, and one `ClaimCheck` is produced per
(sentence × cited `[Sn]` marker), so a sentence citing two sources is checked twice,
independently, once against each. Rejected: a whole paragraph/list item as the claim unit (no
sentence splitter needed, but one bad clause marks five good sentences unsupported, which
reads to a non-technical reader as "none of this is trustworthy" — the opposite of the
calibration R3 wants), and a text span running back from each marker (most faithful to what a
citation asserts, but spans go ragged in real prose and uncited text belongs to no span, so
the `uncited` verdict would need a second extraction pass regardless). Consequence: conflict
detection needs no extra model call and no extra vocabulary — a conflict is one sentence whose
several cited sources return disagreeing verdicts, rendered with both positions and both IDs
and no adjudication (D3).

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. -->

### 2026-08-09 — Phase 1: Model client and agent config
- Done: `harness/models.py` with `ModelError`, `build_chat_model` (five fail-fast branches,
  retry pre-applied via the OpenAI SDK's `max_retries`) and `async preflight` (R6's
  reachability guard, one 1-token call, classified into `ModelError`, never leaks a library
  exception). `AgentSettings` added to `harness/config.py` and `[agent]` to `harness.toml`.
  `deepagents==0.7.5` and `langchain-openai>=0.3` pinned. 118 tests green; ruff/format/mypy
  clean. Real config values are in: OpenCode serves both roles (`kimi-k3` head,
  `gpt-5.6-luna` subagent) at `https://opencode.ai/zen/go/v1`; `[providers.cerebras]` removed.
- Learned: (1) `TodoListMiddleware` DOES exist — in `langchain` 1.3.14, not deepagents'
  namespace — so D9 is implementable; an earlier claim that it was missing was wrong and
  `docs/decisions.md` now carries the corrected entry. (2) `SubAgentMiddleware` is in the
  DEFAULT stack (general-purpose subagent auto-added), so Phase 3 must disable it explicitly
  rather than assume its absence. (3) The default summarizer is deepagents'
  `_DeepAgentsSummarizationMiddleware` wrapper, not langchain's plain `SummarizationMiddleware`
  — D7's `keep` policy must be written against the wrapper. (4) `ProviderConfig` resolves the
  API key of EVERY declared provider, used or not, so declaring a provider means setting its
  key. (5) `base_url` is the API base — the client appends `/chat/completions` itself.
- Drift: one Reconciliation, 2026-08-09 — Phase 1's unset-key acceptance criterion was
  unreachable (`ConfigError` fires at load before `build_chat_model`); struck and amended. See
  `## Reconciliations`. Also three `## Discoveries` entries, two acted on (cerebras pruning,
  the `preflight` addition) and two deferred (global vs per-provider timeout; unpinned jitter).
- Watch-next: the Phase 1 live check is still UNRUN — `base_url` (`https://opencode.ai/zen/go/v1`)
  was inferred from the OpenAI-compatible convention, not read off OpenCode's docs, and the
  model IDs are unverified against the endpoint. Run the two live checks in
  `docs/guides/setup.md` ("Manual live check") before Phase 3 builds on them; `preflight` is
  the fastest way to find out, and a wrong path is a one-value fix in `harness.toml`.

### 2026-08-09 — Phase 2: Fetch amendments — URL cap and source capture
- Done: `fetch.max_urls_per_call` (default 4) bounds one `fetch_pages` call through pydantic
  `max_length` on the input schema, which moved inside `build_fetch_tool` to close over config
  the way `build_search_tool` already does — so the bound is pre-network, not prompt guidance
  (R9/D11). Every consulted source is now captured to `<workspace_dir>/sources/S<n>.md` at
  fetch time: full untruncated text for a `fetched` page, a stub for anything else (R8/D10).
  131 tests green; ruff/format/mypy clean.
- Learned: (1) The stub carries MORE than the frozen first line — `- Status:` and `- Error:`
  bullets are appended when the page has them, so Phase 6 must key on the first line
  (`FETCH FAILED: `) and not assume a fixed stub length. (2) `_sources_dir(config)` in
  `harness/tools/fetch.py` is the single home for the frozen `sources/` path — Phase 6 imports
  it rather than rebuilding the path. (3) A per-file capture failure warns on **stderr** and
  lets the batch continue; the file is simply absent, so Phase 6 must treat a MISSING source
  file exactly as it treats a stub — unverifiable — since that absence is the disclosure.
  (4) `make_config` now defaults the workspace to `tmp_path`, and `.gitignore` covers
  `workspace/` and `reports/` — real runs from Phase 3 write there.
- Drift: none.
- Live check (run at this gate, 2026-08-09): **PASSED.** `preflight` returns clean for both
  roles against the real endpoint, and the head role answers in text — so the inferred
  `base_url` `https://opencode.ai/zen/go/v1` and both model IDs (`kimi-k3`, `gpt-5.6-luna`) are
  confirmed correct, and R6 is verified live, not just under fakes. Two findings for Phase 3:
  `usage_metadata` IS populated on responses from this endpoint (`input_tokens`/`output_tokens`/
  `total_tokens`), which is the mechanism R7's token baseline depends on and the plan left to
  be confirmed in Phase 3 — it is confirmed. And `kimi-k3` is a **reasoning** model: "Say hi in
  five words or fewer" cost 119 output tokens of which 96 were reasoning, so the Phase 3 token
  baseline will be dominated by reasoning tokens and must be recorded with that split visible,
  or the pyramid gets priced against a misleading number.
- Watch-next: ~~the Phase 1 live check is STILL unrun and now blocks Phase 3~~ — done, passed.
  Operational gotcha for any live check from a git worktree: `.env` is gitignored, so it does
  NOT exist inside the worktree — point uv at the main checkout's copy
  (`uv run --env-file ../../../.env ...`) or the run fails with "No environment file found".
  Phase 3 is still the
  heaviest phase in the plan (flagged !#1/!#2/!#3/!#8, ~300-420 lines, 8 files) and the first
  to touch deepagents. Before starting it, note two Phase 1 findings that bear directly on it:
  `SubAgentMiddleware` is in deepagents' DEFAULT stack (the general-purpose subagent must be
  disabled explicitly, not assumed absent), and the default summarizer is deepagents' own
  `_DeepAgentsSummarizationMiddleware` wrapper rather than langchain's plain one, so D7's
  `keep` policy must be written against the wrapper.

### 2026-08-10 — Phase 3: Tracer bullet — question in, report on disk
- Done: `python -m harness "<question>"` runs end to end. `harness/agent.py` (`build_agent` —
  deepagents lead over `build_tools`, `FilesystemBackend` rooted at the workspace,
  `TodoListMiddleware`, no general-purpose subagent, `execute` excluded from the model's
  schema), `harness/report.py` (`RunOutcome` + `write_report`, frozen filename, usable-vs-failed
  source split), `harness/__main__.py` (argv → `preflight` → `astream` with todo echo → report,
  path printed last). `orchestrator.md` rewritten onto native tool calling. 153 tests green;
  ruff/format/mypy clean. Live check PASSED 2026-08-10.
- Learned: (1) Risks !#1, !#3 and !#8 are all retired empirically — async tools work unchanged,
  our system prompt arrives INTACT at the head of a single system message with deepagents'
  middleware prompts appended after it, and `FilesystemBackend` ships shell-free so D6's custom
  backend was never needed. (2) The summarizer MUST be `deepagents.middleware.summarization.
  SummarizationMiddleware`, not langchain's plain one — they share a `.name` so either replaces
  the default, but only the wrapper offloads evicted history to the backend instead of deleting
  it, and only the wrapper leaves the graph's message list intact so the R7 token sum stays
  honest. Shipping the wrong one passes every test. (3) `execute` is bound on EVERY backend and
  cannot be removed from the graph — only from the model's schema, via `excluded_tools` on a
  registered `HarnessProfile`. Same registration disables the general-purpose subagent; the
  registry is process-global and keyed `provider:model-name`. (4) The head role is now
  `deepseek-v4-flash`, which needs a region opt-in on the OpenCode dashboard or the endpoint
  403s. (5) Baseline: 773,032 in / 22,883 out (16,539 reasoning) / 795,915 total for one
  19-source run — input dominates ~34x, so price delegation against INPUT.
- Drift: one Reconciliation with three parts (2026-08-09 — Phase 3): the `execute` tool is
  unremovable from the graph; disabling the general-purpose subagent needs a process-global
  profile registration; and the agent's disk root is the workspace ONLY, not workspace+reports.
  Plus one `## Discoveries` entry deferred to Phase 5 (a mid-run model failure yields no report
  at all — see Watch-next).
- Watch-next: **Phase 5 must widen its partial-report path.** Observed live: the first
  end-to-end attempt died mid-run on a transient DNS failure and produced NO report and a raw
  library traceback, even though the todo plan and three captured sources were already on disk.
  Phase 5's step 3 currently scopes that path to wall-clock expiry only; it must cover ANY
  mid-run termination, and `__main__` must turn a terminal model failure into a plain stderr
  message plus a cut-short report. Also note for Phase 4: `__main__` currently drives the run
  with `astream(stream_mode=["updates","values"])`, so the interrupt/resume loop has to be built
  around that streaming shape rather than a single `ainvoke`.

### 2026-08-10 — Phase 4: Pre-research clarification
- Done: `harness/tools/ask_user.py` (`ASK_USER_TOOL_NAME`, `build_ask_user_tool` — one
  `question` per call, mirroring `build_search_tool`'s factory shape), registered as the third
  entry of `build_tools`. `build_agent` gained `checkpointer=InMemorySaver()` and
  `interrupt_on={"ask_user": InterruptOnConfig(allowed_decisions=["respond"])}`. `__main__` is
  now a resume loop: `_read_answer` (via `asyncio.to_thread`, so Phase 5's clock can still
  fire), `_answer_questions` (one decision per action request), and a `while` around the
  existing `astream`. `orchestrator.md`'s `# No clarification` became `# Clarification`.
  161 tests green; ruff/format/mypy clean. Live check NOT yet run — it needs typed input at a
  real terminal, so only the developer can run it.
- Learned: (1) An interrupt surfaces in BOTH streams — `updates` as
  `{"__interrupt__": (Interrupt(...),)}` whose value is a **tuple**, and `values` as the state
  dict plus that key. Phase 3's todo echo called `.get` on every update value, so it would have
  crashed with `AttributeError` on the first interrupt; the `isinstance(node_update, dict)`
  guard is a real fix, mutation-proven. (2) Interrupt detection must be scoped to the CURRENT
  pass — reading a carried-over `final_state` re-asks the same question forever. (3) `respond`
  genuinely skips tool execution: with two `ask_user` calls in one `AIMessage` the human's two
  answers arrive as the only two `ToolMessage`s, and the tool body's fallback string appears
  nowhere. (4) Our `mode="allow"` filesystem `permissions` generate no interrupt entries
  (`_build_interrupt_on_from_permissions` returns `{}` unless a rule is `mode="interrupt"`), so
  `ask_user` is the whole interrupt surface — verified behaviorally, not assumed.
  (5) A checkpointer makes `thread_id` mandatory on every invocation.
- Drift: one Reconciliation, 2026-08-10 — Phase 4 (no bound on the number of clarifying
  questions; the developer chose none when asked where the bound should live). Plus two
  `## Discoveries` entries: `tests/test_agent.py` outside the Files list (acted now) and
  `InMemorySaver` checkpoint growth (deferred to Phase 5).
- Live check (run at this gate, 2026-08-10): **PASSED.** `python -m harness "which model should
  I use?"` asked one clarifying question at the terminal, blocked for typed input, took the
  answer (`A` = an AI/LLM for coding/writing/reasoning), and resumed into a todo plan and an
  11-source report scoped to LLMs rather than ML or non-software models — so R2 is verified
  live, not just under fakes. No `AttributeError` from the interrupt tuple and no re-asked
  question, confirming the `isinstance` guard and the `pass_state` scoping hold on the real
  path. Two findings: the `PYTHONIOENCODING=utf-8` prefix was NOT needed on the developer's
  PowerShell terminal, and the run disclosed its own degraded coverage unprompted (one fetch
  hit a bot check, one leaderboard table had its model names stripped), which is R4's
  best-effort-and-disclose behavior showing up without Phase 5's cut-short path built yet.
- Watch-next: ~~run the live check before building Phase 5 on this loop~~ — done, passed.
  Two things Phase 5 inherits from this phase: the wall clock must span
  `_read_answer`'s wait (which is why it is `asyncio.to_thread`, not a bare `input()`), and the
  `while` loop plus `pass_state` scoping is the structure the ceiling has to interrupt — a
  timeout has to break out of a pass that may be blocked on human input, not just one blocked
  on the model. Also unchanged from Phase 3 and still owed: Phase 5 must widen the
  partial-report path to ANY mid-run termination.

### 2026-08-12 — Phase 5: Run ceiling and cut-short reporting
- Done: both ceilings wired, and every mid-run termination now yields a report. `__main__`
  carries `recursion_limit = max_rounds * 2 + 1` on the run config (NOT `build_agent` — see
  the Reconciliation), an `asyncio.timeout(None)` clock rescheduled to a real deadline at the
  first `search_web`/`fetch_pages` call, and `TimeoutError`/`GraphRecursionError`/`Exception`
  handling that writes a cut-short report and returns 0 for a bound, 1 for a failure with one
  plain `error:` line on stderr. `_read_answer` moved to a daemon thread; `_final_answer`
  picks the last `AIMessage` carrying prose. `report.py` gained `CutShortReason`, three
  additive `RunOutcome` fields plus `started_at`, `_cut_short_section`, `_notes_section`, and
  the public `format_todos` both modules now share. Phase 3's deferred "widen the
  partial-report path to ANY mid-run termination" is discharged. 184 tests green; all four
  gates clean. Live check NOT yet run — it needs a real terminal, so only the developer can.
- Learned: (1) The mapping premise was wrong and is now measured — see the 2026-08-12
  Discovery; marginal cost is ~2 supersteps per round, but the graph carries a fixed ~7-9
  overhead. (2) `final_state` was being assigned AFTER the `async for`, which every cut-short
  path exits by exception — so cut-short reports silently lost both the answer AND the token
  usage. Found by mutation-testing a fix, not by the review or the gates; it is now assigned
  inside the `values` branch. (3) `tests/test_search.py`'s client-faking helper patches the
  process-global `httpx.AsyncClient`, and `openai`'s constructor rejects any `http_client`
  that is not an instance of whatever that name is bound to at the time — including
  `langchain_openai`'s `_AsyncHttpxClientWrapper`. Any test combining a faked search with a
  scripted model MUST build the model first. (4) `asyncio.timeout(None)` + `reschedule()` is
  the shape that works when the deadline is only known mid-stream; `asyncio.wait_for` cannot
  express it.
- Drift: two Reconciliations, both 2026-08-10 — Phase 5 (round cap moved out of `build_agent`;
  R7's clock-spans-the-wait reversed by the developer). Plus two `## Discoveries` entries
  dated 2026-08-12 (the measured mapping, kept knowingly; `recursion_limit` resetting per
  invocation, deferred).
- Watch-next: **run the live check before Phase 6** — set `wall_clock_seconds` to a few
  seconds in `harness.toml`, run a real question, and confirm the report names the wall clock
  and carries the notes written before the cut. Take an `InMemorySaver` memory reading during
  that run: the Phase 4 Discovery deferred checkpoint growth to this phase and it is still
  unmeasured — the one item Phase 5 inherited and did not settle. For Phase 6: claim
  verification reads `sources/S<n>.md`, and those files are NOT mtime-filtered the way
  workspace notes now are, so a stale `S1.md` from a previous run can still be read whenever
  IDs collide — decide there whether the same filter belongs on them.

### 2026-08-12 — Phase 6: Claim verification and disclosure
- Done: `harness/verify.py` (`Verdict`, `ClaimCheck`, `Conflict`, `VerificationResult`,
  `VerifyError`, `extract_claims`, `verify_claims`) plus `harness/prompts/verify.md`, rendered
  through the existing loader. `report.py` gained `RunOutcome.verification`, `_annotate`
  (marker insertion then `registry.resolve()`, in that order), `_place_marker`,
  `_conflicts_section` and `_gaps_section`. `__main__` runs the pass after the agent loop and
  outside the wall clock, skipping it when the run died `cut_short="error"` and disclosing the
  skip. Phase 5's Watch-next is settled by a Drift, not by an mtime filter: captures now live
  at `sources/<run_id>/S<n>.md`. 211 tests green; all four gates clean. Live check NOT yet run
  — it needs a real endpoint, so only the developer can.
- Learned: (1) `extract_claims` joins a block's lines with a space, so a claim is NOT
  guaranteed to be a verbatim substring of the answer — a hard-wrapped sentence or a
  "Key findings:" lead-in above bullets both produce one that is not. `str.replace` then
  no-oped and the verdict was computed and silently thrown away. Found by the 3F review, not
  by any gate, because every test fed single-line answers. Now placed by a whitespace-tolerant
  regex, and anything still unplaceable is disclosed in `## Gaps and disclosures` rather than
  dropped. Generalise: when a helper's output is fed back into a `str.replace` against its own
  input, test the shapes where the two can diverge. (2) Marker insertion MUST precede
  `registry.resolve()` — resolving first rewrites `[S1]` into a link and no claim matches.
  (3) Citation resolution is unconditional, not gated on the verification pass: a Phase 3 test
  asserting a raw `[S1]` survives was outdated by R1 and was updated (with developer approval)
  to assert the resolved form. A sibling test passed only because its registry was empty.
  (4) An assertion over the WHOLE report body cannot say "no bare marker survives" — `##
  Sources` legitimately prints `- [S1] <link>`; scope such assertions to `## Answer`.
- Drift: one Reconciliation (2026-08-12 — Phase 6): D10's frozen `sources/S<n>.md` became
  `sources/<run_id>/S<n>.md`, with `run_id` on `SourceRegistry` and `_sources_dir(config,
  registry)`. Struck in four places. Plus two `## Discoveries` entries dated 2026-08-12 (the
  prompt file's home; the definition of a claim), both acted on now.
- Watch-next: **the live check is owed for BOTH Phase 5 and Phase 6** and neither is ticked —
  Phase 5's wall-clock check (plus the still-unmeasured `InMemorySaver` growth inherited from
  Phase 4) and Phase 6's cited-report check. For Phase 7: it is the last phase, needs no new
  runtime code, and its `docs/architecture.md` acceptance criterion wants failure modes
  actually observed in Phases 3-6 — this entry's "Learned" items and Phase 5's are the
  material. Also note `docs/INDEX.md`'s Shared Resources must list `harness/verify.py`, which
  now exists.

### 2026-08-12 — Phase 7: Researcher and reader tier contracts
- Done: `harness/prompts/subagent.md` rewritten as the researcher contract and
  `harness/prompts/reader.md` written as the reader contract — both mechanism-neutral prose
  through the existing `render()`, both declaring exactly `{current_date, max_urls_per_call}`,
  both naming the four task fields (`**Objective**`, `**Output format**`, `**Tools**`,
  `**Boundaries**`) and the three return fields (`**Findings**`, `**Source IDs**`,
  `**Conflicts**`), neither mentioning `ask_user`. `tests/test_prompts.py` gained a
  `TIER_CONTRACTS` list, a `_render_shipped` helper, and five parametrized tests (34 in that
  file). `docs/architecture.md` gained `## Agent Topology` and nine observed `## Failure
  Modes`, and its stale `## Overview`/`## Dependencies` were corrected; `docs/INDEX.md` gained
  five Shared Resources rows and a current Status line. No runtime code changed. 232 tests
  green; all four gates clean.
- Learned: (1) `$task` is gone from the researcher contract (developer-approved). A tier
  receives its task through the delegation call at run time, so substituting task text into
  the system prompt would freeze a second delivery path beside deepagents' native one — risk
  !#3's exact shape. This also shrinks !#4 exposure: the artifacts assume no subagent-dict
  field names at all. (2) A presence assertion over prose needs an anchor the prose does not
  supply incidentally — see the 2026-08-12 Phase 7 Discovery. (3) The contracts forbid
  searching (reader) and asking the developer (both) in prose only, but `build_tools` always
  returns `search_web` and `ask_user` and a deepagents subagent inherits the parent's tools
  unless given its own — so the next round must pass each tier a filtered tool list or it gets
  a searching reader that can interrupt the developer while every test stays green. Recorded
  in `docs/architecture.md` `## Agent Topology`.
- Drift: none. One `## Discoveries` entry dated 2026-08-12 — Phase 7 (the frozen-field test),
  acted on now.
- Watch-next: **three live checks are owed and the plan is NOT finished without them** — Phase
  5's wall-clock check plus the still-unmeasured `InMemorySaver` growth (inherited from Phase
  4, deferred twice), Phase 6's cited-report check, and the four manual items in `##
  Verification`, all developer-run. Plan `**Status:**` deliberately stays `In Progress` and
  `- [ ] Final verification` stays unticked until they pass. After that: `/pr-review` for the
  whole-feature review. Then the next round wires the researcher tier against this phase's
  contract and prices it against Phase 3's baseline (773,032 in / 22,883 out for a 19-source
  run — input dominates ~34x).
