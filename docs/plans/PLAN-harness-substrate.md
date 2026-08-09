# PLAN: Harness Substrate — Tools, Prompts, and Config

**Status:** In Progress
**Created:** 2026-08-08
**Type:** Single plan

## Intent

**True goal:** Build the tool, prompt, and config substrate that a future deep-research
orchestrator will call — search, batch fetch, citation IDs, tool registry, prompt
rendering, model routing — each provable on its own, before any agent loop exists.
The developer is the sole user and operator; the harness runs on a self-hosted
Linux homelab.

**Binding outcomes:**

- **R1** — Searching the self-hosted SearXNG returns normalized results (title, URL,
  snippet, engine). An unreachable or malformed response surfaces as a typed failure
  value, not a traceback.
- **R2** — One fetch invocation takes many URLs and retrieves them concurrently with
  independent per-URL outcomes; one URL's failure never fails the batch.
  - Outcomes are distinct named values: `fetched`, `blocked`, `timeout`, `non_html`,
    `error`. `blocked` is concluded from HTTP status alone (e.g. 403/429/503) — no
    challenge-page text sniffing, no vendor-specific detection.
  - A single combined per-page time budget (crawl4ai's `page_timeout`), config-driven,
    default 15s. No separate connect/TTFB budget.
  - An empty URL list returns an empty result, not an error.
- **R3** — Fetched pages return reading-grade markdown with site boilerplate — nav,
  footers, cookie banners, ads, scripts — stripped.
- **R4** — Every retrieved page gets a stable short ID that resolves *mechanically* to
  its URL and renders as a clickable markdown link. No model involvement in
  resolution; the model only emits the ID.
- **R5** — Every extension point — tools, model providers, browser backends — is
  defined by an explicit declared interface (Open/Closed). Adding a new implementation
  requires no change to existing implementations or their callers. The tool registry
  exposes each tool's callable schema.
  - The browser backend is config-selectable: Lightpanda over CDP is the default,
    crawl4ai-managed Playwright/Chromium is the fallback if the Lightpanda
    integration proves unworkable.
- **R6** — Orchestrator and subagent prompts are versioned files loaded and rendered
  by code, with declared required variables. A missing variable fails loud at render
  time. Prompts are judged as artifacts, not by model output quality.
- **R7** — Head-agent and subagent model roles resolve to a provider + model ID
  through validated config; adding or swapping a model or a provider touches no
  calling code. A missing required setting fails at startup with a clear message.
  Endpoints, keys, concurrency limits and timeouts share that same config surface.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**

- Keep the dependency footprint small; avoid pulling in provider SDKs that are never
  called.
- Boilerplate stripping tuned aggressively toward main content only. Tuning quality
  is iterative, not an acceptance gate.
- Ruff-clean under the existing config; mypy-clean once source files exist.
- Prefer a flat module layout over a deep package tree.

**Non-goals:**

- No agent loop, delegation, multi-round research, synthesis, or report writing.
- No model HTTP client and no fallback switchover between models.
- No user interface of any kind. A web UI is the intended later direction; the TUI
  idea is dropped.
- No verification, reputability, or source-quality agents.
- No robots.txt pre-checking.
- No database — reports will be files on disk.
- No shell tool in the registry.
- No sandbox, REPL, or code execution.
- No deployment automation for the homelab.

**Constraints & assumptions:**

- Python >=3.11, managed with uv. Ruff configured (line-length 100, select E/F/I/UP).
  pytest is not yet a dependency and must be added.
- Development on Windows; SearXNG and Lightpanda run as local Docker images;
  production is the Linux homelab operated over SSH.
- Tests are fixture-based and offline; each network-dependent tool additionally gets
  one documented manual live-check command.
- SearXNG is already deployed with its JSON API enabled. Lightpanda deployment is
  part of this project's build.
- Lightpanda is Beta: it implements a Page/Network/Runtime/DOM CDP subset, errors on
  commands outside it, and does not populate navigation timing. The
  crawl4ai-to-Lightpanda pairing is documented by neither project — this is the
  riskiest assumption in the plan.
- Best-effort + disclose: degraded coverage is surfaced, never silently thinned.
- No hardcoded endpoints, model IDs, or keys.

**Open questions:**

- none

## Context

The repository is greenfield — documentation scaffolding, `pyproject.toml` with zero
runtime dependencies, and no source files. This plan lays the first source down: the
tools a research orchestrator calls, the prompts it runs on, and the config that
routes it to models. The agent loop itself is deliberately excluded, so every piece
here is judged against its own contract rather than against end-to-end answer
quality.

## Background

The harness will eventually be an orchestrator–worker agent built on LangChain's
`deepagents` (a head model plans and synthesizes; a cheaper worker model does
parallel triage). That destination is settled but not built here. What it forces on
this plan is the **tool contract**: tools are LangChain-native so they drop straight
into `create_deep_agent(tools=[...])` later, and so MCP tools — which arrive already
shaped as `BaseTool` via `langchain-mcp-adapters` — live in the same list without
translation.

External library facts this plan is built on, each confirmed from official docs
during planning:

- **crawl4ai** — `BrowserConfig(browser_mode="custom", cdp_url=...)` attaches to an
  externally running browser over CDP instead of launching Playwright.
  `arun_many(urls, config=..., dispatcher=...)` crawls concurrently and reports
  per-URL success through `CrawlResult.success` / `.error_message` / `.status_code`
  rather than by raising. `MemoryAdaptiveDispatcher` is the default dispatcher
  (`max_session_permit` default 10). Boilerplate stripping is
  `CrawlerRunConfig(excluded_tags=..., markdown_generator=DefaultMarkdownGenerator(
  content_filter=PruningContentFilter(...)))`, which populates
  `result.markdown.fit_markdown` alongside `raw_markdown`. `page_timeout` is a single
  combined navigation+JS budget in milliseconds (default 60000); there is no separate
  connect/TTFB knob. Non-HTML responses are **not** content-type dispatched — a PDF
  URL yields empty markdown rather than an error. Current release 0.9.2.
- **Lightpanda** — official Docker image `lightpanda/browser`, CDP server on port
  9222, documented for Playwright via `connectOverCDP`. It implements a
  Page/Network/Runtime/DOM subset and errors on commands outside it. Its pairing with
  crawl4ai specifically is documented by neither project.
- **langchain-core** — `from langchain_core.tools import tool, StructuredTool`.
  `@tool` binds an `async def` as the tool's `coroutine` (via
  `inspect.iscoroutinefunction`); sync-invoking an async-only tool raises
  `NotImplementedError`, so the agent must always be driven with `ainvoke`.
  `response_format="content_and_artifact"` makes the tool return a `(content,
  artifact)` two-tuple where content becomes the `ToolMessage` the model reads and
  the artifact rides along **without being sent to the model**. `args_schema` accepts
  an explicit pydantic v2 model; name and description default to the function name
  and docstring and are overridable in the decorator.
- **deepagents** (destination, not a dependency here) — per-subagent `model` override
  is first-class; the compiled agent exposes `ainvoke` and `astream_events`. Its
  `"provider:model"` string form only covers providers LangChain knows, which
  OpenCode and Cerebras are not — they are OpenAI-compatible endpoints requiring an
  explicit `base_url`, which is why config carries a base URL per provider.

## Codebase Map

- **Entry points:** none — no source files exist. `harness/` is created by Phase 1.
- **Module boundaries (as this plan establishes them):** `harness/config.py` (config
  loading + validation), `harness/sources.py` (citation IDs), `harness/prompts.py`
  (prompt rendering), `harness/tools/` (one module per tool plus the tool list).
- **Reuse targets:** none exist. Every phase after Phase 1 reuses contracts pinned by
  an earlier phase in this plan.
- **Tests:** none exist. pytest is not yet a dependency; Phase 1 adds `pytest` and
  `pytest-asyncio` as dev deps and creates `tests/`.
- **Commands (as they work today):**
  - Lint: `uv run ruff check .`
  - Format check: `uv run ruff format --check .`
  - Typecheck: `uv run mypy .` — currently exits non-zero with "no .py[i] files";
    becomes usable once Phase 1 lands.
  - Test: none — `uv run pytest` works from Phase 1 onward.
- **Existing config surface:** `pyproject.toml` sets `requires-python = ">=3.11"`,
  ruff `line-length = 100`, `select = ["E", "F", "I", "UP"]`, `[tool.uv] package =
  false` (so the package is run in place, not installed). `.env.example` currently
  lists `OPENCODE_API_KEY`, `CEREBRAS_API_KEY`, `SEARXNG_URL`, `LIGHTPANDA_CDP_URL`.
- **Docs to keep current:** `docs/INDEX.md`, `docs/architecture.md`,
  `docs/guides/setup.md`, `docs/decisions.md`, `docs/backlog.md`.

## Non-Goals

- No agent loop, delegation, multi-round research, synthesis, or report writing —
  nothing in this plan calls a model.
- No model HTTP client: config resolves a role to a provider and model ID; actually
  constructing a chat model and calling it belongs to the loop plan.
- No `deepagents`, `langchain`, or `langgraph` dependency. Only `langchain-core`,
  for the tool decorator.
- No user interface. The TUI idea is dropped; a web UI is the later direction.
- No robots.txt pre-checking, no bot-challenge fingerprinting beyond HTTP status.
- No `read_source`-style second-tier retrieval tool or virtual filesystem — the
  fetch tool caps per-page content instead (see D6).
- No database, shell tool, sandbox, REPL, or homelab deployment automation.

## Design Decisions

### D1: Tool contract — LangChain-native vs framework-neutral
- **Chosen:** Tools are LangChain-native — `@tool` from `langchain_core.tools`, async,
  pydantic v2 schemas, `response_format="content_and_artifact"`.
- **Rejected:** A framework-neutral `ToolSpec` (name + description + pydantic input
  model + plain async callable) with a one-line `StructuredTool.from_function`
  adapter added later. It hedges an open framework decision — but the decision is not
  open: `deepagents` is the confirmed destination and MCP tools (a YouTube-transcript
  server is anticipated) already arrive as `BaseTool`. Under a known destination the
  hedge is pure premium.
- **Consequences:** `langchain-core` is a runtime dependency from Phase 1. Tools are
  exercised in tests via `.ainvoke({...})`, not by calling the underlying function.
  Because `@tool` binds `async def` to `coroutine` only, the harness must always be
  driven with `ainvoke` — sync invocation raises `NotImplementedError`.

### D2: Only `langchain-core`, not `langchain` or `deepagents`
- **Chosen:** Depend on `langchain-core` alone; import `tool` from
  `langchain_core.tools`.
- **Rejected:** Installing `deepagents` now — it hard-depends on
  `langchain-anthropic` and `langchain-google-genai`, two provider SDKs this project
  never calls, and ships no `langchain-openai`, which is what OpenCode/Cerebras
  actually need. Nothing in this plan uses what those packages provide.
- **Consequences:** The newer unified docs show `from langchain.tools import tool`;
  this plan uses the `langchain_core.tools` path where the symbols physically live.
  The loop plan will add `deepagents` and may switch the import path then.

### D3: Config surface — TOML providers/roles table, secrets in env
- **Chosen:** A checked-in `harness.toml` declares providers (`base_url`,
  `api_key_env`), model roles (`head`, `subagent` → provider + model), the browser
  backend, and limits. Secrets never appear in the file — it names the environment
  variable that holds each key. Parsed with stdlib `tomllib`, validated with pydantic.
- **Rejected:** Env-only with a `{PROVIDER}_BASE_URL` naming convention (extensible
  but relies on undiscoverable magic, and per-provider extras get awkward); a Python
  config module (changing a model becomes a code edit, the weakest fit for "new
  models come and go").
- **Consequences:** `tomllib` is stdlib in 3.11, so this costs no dependency.
  Endpoint URLs move out of `.env` and into `harness.toml` — `SEARXNG_URL` and
  `LIGHTPANDA_CDP_URL` are removed from `.env.example`, which keeps only API keys.
  There is room for per-provider rate limits when the budget scheduler arrives,
  without another config redesign.

### D4: Citation IDs — sequential per run
- **Chosen:** A per-run `SourceRegistry` assigns `S1..Sn` in fetch order; the same
  normalized URL always maps to the same ID within a run. Resolution to a markdown
  link is a pure mechanical lookup.
- **Rejected:** A URL-derived short hash (`[a3f9c1]`) — no shared state and stable
  across runs, but costs more tokens and reads badly in a report; domain-tagged IDs
  (`[example-1]`) — longest, and the domain hint nudges the model's reasoning.
- **Consequences:** IDs are meaningful only within one run, which is fine because a
  report is a standalone artifact. The registry is mutable per-run state, which
  forces D8's tool-construction shape.

### D5: Prompt rendering — stdlib `string.Template`
- **Chosen:** Prompts are `.md` files using `$variable`, rendered with
  `Template.substitute()`, which raises `KeyError` on a missing variable — R6's
  fail-loud for free.
- **Rejected:** LangChain `ChatPromptTemplate` — consistent with D1 and offers
  message-role structure, but its f-string `{var}` syntax forces `{{ }}` escaping on
  every literal brace, and orchestrator/subagent prompts are full of JSON examples.
  It also buys nothing: `deepagents` takes `system_prompt` as a plain `str`.
- **Consequences:** Prompts are flat strings, not role-structured message lists. If
  the loop plan needs multi-message prompts, it composes them from rendered strings.

### D6: Fetch payload — full markdown, capped per page
- **Chosen:** `content` (model-facing) is every page's cleaned markdown under an
  `[S1]` heading, each truncated at a config-driven per-page character cap with an
  explicit truncation marker. `artifact` (harness-facing, never sent to the model)
  carries the full untruncated structured results.
- **Rejected:** Uncapped full markdown — a 12-URL fetch is ~120k tokens in one tool
  result; digest-plus-`read_source(id)` — cheapest context and the pattern
  `deepagents`' virtual filesystem formalizes, but it adds a second tool and a source
  store for an orchestrator that does not exist yet.
- **Consequences:** The cap is the single knob bounding worst-case context. Upgrading
  to the digest pattern later is additive (a second tool reading the same registry),
  not a rewrite.

### D7: Failure taxonomy from HTTP status alone
- **Chosen:** `blocked` is concluded from status codes (403/429/503). No
  challenge-page text matching, no vendor-specific (Cloudflare/Akamai) detection.
- **Rejected:** Parsing crawl4ai's `crawl_stats` block reasons and known challenge
  markers — more precise, more surface to maintain, and the distinction does not
  change what the harness does about it.
- **Consequences:** A soft block returning HTTP 200 with a challenge body classifies
  as `fetched` with junk markdown. Accepted; see risk #2.

### D8: Tools are built by explicit factories, not module-level decorated functions
- **Chosen:** `harness/tools/__init__.py` exposes `build_tools(config, registry) ->
  list[BaseTool]` whose body is an explicit, greppable list of per-tool builders.
  Each tool module exposes `build_<name>_tool(config, registry) -> BaseTool` that
  closes over config and the run's `SourceRegistry`.
- **Rejected:** A module-level `TOOLS = [fetch_pages, search_web]` list of
  `@tool`-decorated functions — the shape originally chosen, but a decorated
  module-level function has no way to receive per-run config or the `SourceRegistry`,
  since LangChain passes only model-supplied arguments. Also rejected: a
  `contextvars`-based ambient registry (invisible coupling) and creating a fresh
  registry inside each call (breaks ID stability across calls in one run).
- **Consequences:** The extension story is unchanged in spirit — adding a tool is a
  new module plus one line in `build_tools`, and the full tool set is still visible
  in one place. Callers hold a registry for the lifetime of a run and pass it once.

## Requirements Coverage

| ID | Requirement | MoSCoW | Covered by |
|----|-------------|--------|------------|
| R1 | Normalized SearXNG search with typed failure | MUST | Phase 4 |
| R2 | Concurrent batch fetch, independent per-URL outcomes | MUST | Phase 3 |
| R3 | Boilerplate-stripped reading-grade markdown | MUST | Phase 3 |
| R4 | Stable short IDs resolving mechanically to links | MUST | Phase 2 (assignment + resolution), Phase 3 (applied to fetch payload) |
| R5 | Tools, providers, browser backends behind declared interfaces | MUST | Phase 1 (providers, browser backend), Phase 3 (backend selection), Phase 5 (tool list) |
| R6 | Prompts as versioned files with fail-loud rendering | MUST | Phase 5 |
| R7 | Validated config resolving model roles to provider + model | MUST | Phase 1 |

## Progress

- [x] Phase 1: Skeleton, dependencies, and config surface
- [x] Phase 2: Source registry and citation rendering
- [ ] Phase 3: Fetch tool
- [ ] Phase 4: Search tool
- [ ] Phase 5: Tool list and prompt loader
- [ ] Final verification

## Phases

### Phase 1: Skeleton, dependencies, and config surface

**Risk:** flagged (!#1)
**Test-first:** required
**Goal:** Create the `harness` package and a validated config surface where providers,
model roles, browser backend, and limits are declared in TOML with secrets pointed at
by environment-variable name — and settle the Lightpanda-vs-Playwright default by
actually testing the pairing before writing it down.
**Requirements:** R7, R5 (providers and browser backend as declared interfaces)
**Assumes:**
- `uv` is installed and `uv sync` works.
- Docker is available to run `lightpanda/browser`.
**Diff budget:** ~180-260 lines across 8 files (most of it config models and tests).

**Files:**
- `pyproject.toml` — modify: add runtime deps (`pydantic`, ~~`pydantic-settings`,~~
  `langchain-core`, `crawl4ai`, `httpx`) and dev deps (`pytest`, `pytest-asyncio`);
  add `[tool.pytest.ini_options]` with `asyncio_mode`.
- `harness/__init__.py` — new: package marker. Reason: nothing can import from the
  package until it exists.
- `harness/config.py` — new: TOML + env loading, pydantic models, validation. Reason:
  R7's single validated config surface; every later phase depends on it.
- `harness.toml` — new: the checked-in providers/roles/browser/limits declaration.
  Reason: it *is* the config surface D3 chose.
- `.env.example` — modify: keep only API keys; remove `SEARXNG_URL` and
  `LIGHTPANDA_CDP_URL`, which move into `harness.toml` (D3).
- `tests/__init__.py` — new: package marker for the test suite.
- `tests/test_config.py` — new: config validation coverage.
- `docs/guides/setup.md` — modify: document `harness.toml`, the revised `.env`
  contents, the Docker commands for SearXNG and Lightpanda, and `uv run pytest`.
- `docs/decisions.md` — modify: append the crawl4ai↔Lightpanda smoke-check outcome
  and the resulting default backend.

**Reuse:**
- none — new surface. This is the first source file in the repository.
- Pattern to mirror: none exists. Follow the ruff settings already in
  `pyproject.toml` (line length 100, import sorting on).

**Contracts:**
- `harness/config.py`:
  - `load_config(path: Path | None = None) -> HarnessConfig` — defaults to
    `harness.toml` at repo root; raises `ConfigError` on any validation failure.
  - `class ConfigError(Exception)`
  - `HarnessConfig` fields: `providers: dict[str, ProviderConfig]`,
    `roles: dict[str, RoleConfig]`, `browser: BrowserSettings`,
    `fetch: FetchSettings`, `search: SearchSettings`
  - `ProviderConfig(base_url: str, api_key_env: str)` with
    `api_key: str` resolved from the environment at load time
  - `RoleConfig(provider: str, model: str)` — role keys `head` and `subagent`
  - `BrowserSettings(backend: Literal["lightpanda", "playwright"], cdp_url: str | None)`
  - `FetchSettings(page_timeout_ms: int = 15000, max_concurrency: int = 5,
    per_page_char_cap: int = 12000)`
  - `SearchSettings(base_url: str, default_max_results: int = 10)`
- `harness.toml` section names, frozen for later phases: `[providers.<name>]`,
  `[roles.head]`, `[roles.subagent]`, `[browser]`, `[fetch]`, `[search]`.

**Out of scope:**
- No chat-model construction, no HTTP calls to any model provider, no
  `langchain-openai`.
- No tool modules, no `harness/tools/` package — Phase 3 creates it.
- No CLI entry point or `__main__`.
- Do not add `deepagents`, `langchain`, or `langgraph`.
- Do not restructure `docs/architecture.md` beyond what later phases need.

**Tests (write first, confirm red):**
- [ ] A valid TOML file loads into `HarnessConfig` with providers, roles, browser,
      and limits populated.
- [ ] Omitted limit values fall back to the documented defaults (15000 / 5 / 12000).
- [ ] A missing environment variable named by `api_key_env` raises `ConfigError`
      whose message names the variable.
- [ ] A role referencing an undeclared provider raises `ConfigError` naming both.
- [ ] An unknown `browser.backend` value raises `ConfigError`.
- [ ] `backend = "lightpanda"` with no `cdp_url` raises `ConfigError`.
- [ ] A malformed or missing TOML file raises `ConfigError`, not a bare
      `tomllib`/`OSError` traceback.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add dependencies to `pyproject.toml` and `uv sync`.
3. Run the smoke check: start `lightpanda/browser` in Docker, then drive a minimal
   crawl4ai run against it over CDP (`BrowserConfig(browser_mode="custom",
   cdp_url=...)`) on one simple public page. Record the outcome.
4. Create `harness/__init__.py` and `harness/config.py` with the pydantic models and
   `load_config` per **Contracts**.
5. Write `harness.toml` with the OpenCode and Cerebras providers, the `head` and
   `subagent` roles, and the browser backend defaulted to whichever the step-3 smoke
   check proved viable.
6. Update `.env.example` to keys only.
7. Run the tests; confirm they PASS (green).
8. Update `docs/guides/setup.md` and append the smoke-check outcome to
   `docs/decisions.md`.

**Acceptance criteria:**
- [x] The step-3 smoke check is recorded in `docs/decisions.md` with the observed
      result and the resulting `browser.backend` default. If crawl4ai could not drive
      Lightpanda, the entry says so and the default is `playwright`.
- [x] `uv run pytest` runs and passes from the repo root.
- [x] `uv run mypy .` no longer exits with "no .py[i] files".
- [x] `docs/guides/setup.md` lists the literal `docker run` commands for SearXNG and
      Lightpanda and the current `.env` variable set.

### Phase 2: Source registry and citation rendering

**Risk:** none
**Test-first:** required
**Goal:** Assign stable per-run `S1..Sn` IDs to URLs and resolve `[Sn]` markers into
clickable markdown links purely mechanically, with no model involvement.
**Requirements:** R4
**Assumes:**
- Phase 1's package layout exists.
**Diff budget:** ~90-140 lines across 2 files.

**Files:**
- `harness/sources.py` — new: `SourceRegistry` and URL normalization. Reason: R4's
  ID assignment and resolution is a distinct, purely offline concern that both the
  fetch tool and any future report writer depend on.
- `tests/test_sources.py` — new.

**Reuse:**
- none — new surface.
- Pattern to mirror: `harness/config.py` from Phase 1 — same module shape, pydantic
  models for data, plain functions/classes for behavior, explicit exceptions.

**Contracts:**
- `harness/sources.py`:
  - `class Source(BaseModel)` with `id: str`, `url: str`, `title: str | None`
  - `class SourceRegistry`
    - `add(url: str, title: str | None = None) -> str` — returns the ID; the same
      normalized URL always returns the same ID and does not create a second entry
    - `get(source_id: str) -> Source | None`
    - `all() -> list[Source]` — insertion order
    - `link(source_id: str) -> str` — returns `[domain](url)`; raises `KeyError` for
      an unknown ID
    - `resolve(text: str) -> str` — replaces every known `[Sn]` marker with its
      markdown link, leaving unknown markers untouched
    - `unresolved_ids(text: str) -> list[str]` — every `[Sn]`-shaped marker in the
      text with no registry entry
  - `normalize_url(url: str) -> str`
- ID format is `S` followed by a 1-based integer, frozen: later phases and the report
  writer match on `\[S\d+\]`.

**Out of scope:**
- No persistence, no cross-run ID stability, no serialization format.
- No fetching, no HTTP, no relevance ranking or source-quality scoring.
- Do not add a `read_source`-style retrieval method (explicitly deferred by D6).

**Tests (write first, confirm red):**
- [ ] IDs are assigned sequentially from `S1` in insertion order.
- [ ] The same URL added twice returns the same ID and produces one entry in `all()`.
- [ ] URLs differing only by trailing slash, default port, or fragment normalize to
      the same ID; URLs differing by query string do not.
- [ ] `link()` renders `[domain](url)` and raises `KeyError` for an unknown ID.
- [ ] `resolve()` replaces every known marker in a body of text, including several in
      one sentence and one at the very start or end.
- [ ] `resolve()` leaves an unknown marker verbatim, and `unresolved_ids()` reports
      exactly that marker.
- [ ] `resolve()` on text with no markers returns it unchanged.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `normalize_url` and `Source`.
3. Implement `SourceRegistry` per **Contracts**.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `uv run ruff check .` and `uv run mypy .` are clean for `harness/sources.py`.

### Phase 3: Fetch tool

**Risk:** flagged (!#1, !#2, !#3)
**Test-first:** required
**Goal:** One tool call fetches many URLs concurrently through crawl4ai, returns
boilerplate-stripped markdown capped per page with `[Sn]` headings for the model, and
carries full structured per-URL outcomes in the artifact — with no single URL able to
fail the batch.
**Requirements:** R2, R3, R5 (browser backend selection), R4 (applied)
**Assumes:**
- Phase 1's `FetchSettings` and `BrowserSettings` exist and validate.
- Phase 2's `SourceRegistry` exists.
- A browser backend proven usable in Phase 1 is reachable for the live check.
**Diff budget:** ~220-320 lines across 4 files.

**Files:**
- `harness/tools/__init__.py` — new: package marker only in this phase;
  `build_tools` arrives in Phase 5. Reason: the tools package must exist to hold
  tool modules.
- `harness/tools/fetch.py` — new: browser config construction, the crawl4ai call,
  outcome classification, payload assembly, and `build_fetch_tool`. Reason: R2/R3
  are one coherent concern; splitting browser setup into its own module would be
  three functions in a file.
- `tests/test_fetch.py` — new.
- `docs/guides/setup.md` — modify: add the documented manual live-check command.

**Reuse:**
- Extend `SourceRegistry` from `harness/sources.py` (Phase 2) for ID assignment — do
  NOT assign IDs inside the fetch tool.
- Read all limits from `FetchSettings` / `BrowserSettings` in `harness/config.py`
  (Phase 1) — do NOT introduce literals for timeout, concurrency, or the cap.
- Pattern to mirror: `harness/sources.py` — module shape, pydantic models for
  returned data, explicit named exceptions.

**Contracts:**
- `harness/tools/fetch.py`:
  - `FetchOutcome = Literal["fetched", "blocked", "timeout", "non_html", "error"]`
  - `class FetchedPage(BaseModel)` with `source_id: str`, `url: str`,
    `outcome: FetchOutcome`, `status_code: int | None`, `title: str | None`,
    `markdown: str`, `error: str | None`
  - `build_fetch_tool(config: HarnessConfig, registry: SourceRegistry) -> BaseTool` —
    returns a tool named `fetch_pages` declared with
    `response_format="content_and_artifact"`
  - The tool's model-facing arguments are exactly `urls: list[str]`, described by a
    pydantic v2 `args_schema`.
  - The tool returns `tuple[str, list[FetchedPage]]`: content is markdown with one
    `## [Sn] <url>` heading per URL, and the artifact is the untruncated pages.
  - `classify(status_code: int | None, error_message: str | None, content_type: str |
    None, markdown: str) -> FetchOutcome`
  - `build_browser_config(settings: BrowserSettings) -> BrowserConfig`
- Classification rules, frozen: status 403/429/503 → `blocked`; a timeout indicated by
  crawl4ai's error message → `timeout`; a non-HTML content type, or a successful
  crawl yielding empty markdown → `non_html`; any other unsuccessful crawl → `error`;
  otherwise `fetched`.
- Truncated page content ends with an explicit marker line naming the cap.

**Out of scope:**
- No robots.txt checking, no challenge-page text matching, no vendor-specific bot
  detection (D7).
- No retry logic, no rate limiting, no proxy support — crawl4ai's `RateLimiter` and
  fallback strategies stay unused in this plan.
- No PDF extraction (`PDFCrawlerStrategy`); PDFs classify as `non_html` and stop
  there.
- No caching of fetched pages to disk.
- Do not add `build_tools` to `harness/tools/__init__.py` — that is Phase 5.

**Tests (write first, confirm red):**
- [ ] An empty URL list returns empty content and an empty artifact, and raises
      nothing.
- [ ] A mixed batch where some URLs succeed and others fail returns one entry per
      input URL, with the successes intact — no failure aborts the batch.
- [ ] Each classification rule maps to its outcome: 403, 429 and 503 → `blocked`; a
      timeout error message → `timeout`; a non-HTML content type → `non_html`; a
      successful crawl with empty markdown → `non_html`; another unsuccessful crawl →
      `error`; a successful crawl with content → `fetched`.
- [ ] Every returned page carries a `source_id` registered in the `SourceRegistry`,
      and the same URL passed twice in one batch reuses one ID.
- [ ] Content exceeding `per_page_char_cap` is truncated in the model-facing content
      and carries the truncation marker, while the artifact keeps the full text.
- [ ] Model-facing content contains a `## [Sn] <url>` heading for every URL,
      including failed ones, with the outcome visible.
- [ ] `page_timeout_ms`, `max_concurrency` and `per_page_char_cap` from config reach
      the crawl4ai call rather than being hardcoded.
- [ ] `build_browser_config` produces a CDP-attached config for the `lightpanda`
      backend and a crawl4ai-managed config for `playwright`.

**Steps:**
1. Write the tests above against fixture `CrawlResult`-shaped objects; run them;
   confirm they FAIL (red).
2. Implement `build_browser_config` and the `CrawlerRunConfig` assembly, including
   `excluded_tags` and `DefaultMarkdownGenerator(content_filter=
   PruningContentFilter(...))` so `fit_markdown` is populated.
3. Implement `classify` per the frozen rules.
4. Implement the `arun_many` call with `MemoryAdaptiveDispatcher` bounded by
   `max_concurrency`, mapping each `CrawlResult` to a `FetchedPage`.
5. Implement payload assembly (registry IDs, headings, per-page cap) and wrap it as
   `build_fetch_tool`.
6. Run the tests; confirm they PASS (green).
7. Add the manual live-check command to `docs/guides/setup.md`.

**Acceptance criteria:**
- [ ] Manual live check: with the browser backend running, fetch three URLs — one
      ordinary article, one known to return 403, and one PDF — and observe
      `fetched` / `blocked` / `non_html` respectively, with the article's markdown
      free of nav and footer text.
- [ ] The live-check command is written down in `docs/guides/setup.md` and runs as
      written.
- [ ] `uv run ruff check .` and `uv run mypy .` are clean for the new files.

### Phase 4: Search tool

**Risk:** none
**Test-first:** required
**Goal:** Query the self-hosted SearXNG JSON API and return normalized results, with
an unreachable or malformed response surfacing as a typed failure value rather than an
exception.
**Requirements:** R1
**Assumes:**
- Phase 1's `SearchSettings` exists.
- Phase 3 established the tool-module shape.
**Diff budget:** ~130-190 lines across 3 files.

**Files:**
- `harness/tools/search.py` — new: the SearXNG client and `build_search_tool`.
  Reason: one module per tool, mirroring `fetch.py`.
- `tests/test_search.py` — new.
- `docs/guides/setup.md` — modify: add the search live-check command.

**Reuse:**
- Read the SearXNG base URL and default result count from `SearchSettings` in
  `harness/config.py` (Phase 1).
- Pattern to mirror: `harness/tools/fetch.py` (Phase 3) — the `build_<name>_tool`
  factory shape, pydantic result models, and typed outcomes instead of raised
  exceptions.

**Contracts:**
- `harness/tools/search.py`:
  - `class SearchResult(BaseModel)` with `title: str`, `url: str`, `snippet: str`,
    `engine: str`
  - `class SearchFailure(BaseModel)` with `reason: Literal["unreachable",
    "bad_status", "malformed"]` and `detail: str`
  - `build_search_tool(config: HarnessConfig) -> BaseTool` — returns a tool named
    `search_web` declared with `response_format="content_and_artifact"`
  - Model-facing arguments are exactly `query: str` and `max_results: int`, described
    by a pydantic v2 `args_schema`.
  - The tool returns `tuple[str, list[SearchResult] | SearchFailure]`.

**Out of scope:**
- No fetching of result URLs — that is the fetch tool's job, and the two are not
  chained in this plan.
- No source-quality ranking, deduplication across queries, or engine selection logic.
- No registration of search results in the `SourceRegistry` — IDs are assigned on
  fetch, not on search.
- No retries or backoff.

**Tests (write first, confirm red):**
- [ ] A well-formed SearXNG JSON response maps to `SearchResult` objects with title,
      URL, snippet and engine populated.
- [ ] `max_results` bounds the number of results returned.
- [ ] A connection error returns a `SearchFailure` with reason `unreachable` and
      raises nothing.
- [ ] A non-200 response returns `SearchFailure` with reason `bad_status` and the
      status in the detail.
- [ ] A 200 response whose body is not JSON, or lacks the expected `results` key,
      returns `SearchFailure` with reason `malformed`.
- [ ] A response with zero results returns an empty result list, not a failure.
- [ ] The model-facing content on failure states that the search failed and why.

**Steps:**
1. Write the tests above against a mocked `httpx` transport; run them; confirm they
   FAIL (red).
2. Implement the SearXNG request and response parsing.
3. Implement failure mapping to `SearchFailure`.
4. Wrap as `build_search_tool` with the content/artifact payload.
5. Run the tests; confirm they PASS (green).
6. Add the live-check command to `docs/guides/setup.md`.

**Acceptance criteria:**
- [ ] Manual live check: query the real SearXNG instance for a term and observe
      normalized results; then point the config at a dead URL and observe a
      `SearchFailure` with reason `unreachable` rather than a traceback.
- [ ] `uv run ruff check .` and `uv run mypy .` are clean for the new files.

### Phase 5: Tool list and prompt loader

**Risk:** flagged (!#4)
**Test-first:** required
**Goal:** Expose the full tool set through one explicit builder, and load orchestrator
and subagent prompts from versioned files with rendering that fails loud on a missing
variable.
**Requirements:** R5 (tool list), R6
**Assumes:**
- Phases 3 and 4 provide `build_fetch_tool` and `build_search_tool`.
**Diff budget:** ~150-220 lines across 6 files.

**Files:**
- `harness/tools/__init__.py` — modify: add `build_tools`.
- `harness/prompts.py` — new: prompt discovery and rendering. Reason: R6's loader is
  a distinct concern from tools and has no other home.
- `harness/prompts/orchestrator.md` — new: the head-agent prompt. Reason: R6 requires
  prompts to exist as versioned files.
- `harness/prompts/subagent.md` — new: the worker/subagent prompt. Same reason.
- `tests/test_prompts.py` — new.
- `tests/test_tools_registry.py` — new.
- `docs/architecture.md` — modify: fill in Directory Structure, Key Patterns and
  Dependencies from what this plan actually built.

**Reuse:**
- Extend `harness/tools/__init__.py` created in Phase 3 — do NOT create a separate
  registry module.
- Call `build_fetch_tool` and `build_search_tool` as they were pinned in Phases 3
  and 4 — do NOT change their signatures.
- Pattern to mirror: `harness/config.py` — explicit named exceptions with messages
  that identify the offending item.

**Contracts:**
- `harness/tools/__init__.py`:
  - `build_tools(config: HarnessConfig, registry: SourceRegistry) -> list[BaseTool]`
- `harness/prompts.py`:
  - `render(name: str, **variables: object) -> str` — loads
    `harness/prompts/<name>.md` and substitutes `$var` placeholders
  - `required_variables(name: str) -> set[str]`
  - `class PromptError(Exception)` — raised for an unknown prompt name and for a
    missing variable, naming the prompt and the variable
- Prompt files live in `harness/prompts/` as `<name>.md` and use `$variable`
  placeholders; `$$` escapes a literal dollar sign.

**Out of scope:**
- No agent, no `deepagents`, no model invocation — the prompts are never sent
  anywhere in this plan.
- No prompt versioning scheme, changelog, or A/B machinery.
- No message-role structure (D5) — prompts render to flat strings.
- Do not tune prompt wording for output quality; they are judged as artifacts.

**Tests (write first, confirm red):**
- [ ] `build_tools` returns one tool per registered tool module, with unique names
      matching the frozen `fetch_pages` and `search_web`.
- [ ] Every returned tool exposes a non-empty description and a JSON schema derived
      from its pydantic `args_schema`.
- [ ] Rendering a prompt with all required variables produces text with no `$`
      placeholders remaining.
- [ ] Rendering with a missing variable raises `PromptError` naming both the prompt
      and the missing variable.
- [ ] An unknown prompt name raises `PromptError` naming it.
- [ ] `required_variables` reports exactly the placeholders present in a file.
- [ ] A prompt containing a JSON example with literal braces renders unchanged.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `harness/prompts.py` over `string.Template`.
3. Write `orchestrator.md` and `subagent.md` with declared `$variable` placeholders.
4. Implement `build_tools` as an explicit list of builder calls.
5. Run the tests; confirm they PASS (green).
6. Update `docs/architecture.md`.

**Acceptance criteria:**
- [ ] `docs/architecture.md` documents the directory structure, the tool-contract
      pattern, and the dependency list as actually built — no "to be documented"
      placeholders left in those three sections.
- [ ] `docs/INDEX.md` Shared Resources table lists `harness/config.py`,
      `harness/sources.py`, `harness/prompts.py` and `harness/tools/`.

## Verification

- [ ] `uv run pytest` — all tests pass from the repo root.
- [ ] `uv run ruff check .` — clean.
- [ ] `uv run ruff format --check .` — clean.
- [ ] `uv run mypy .` — clean.
- [ ] Manual end-to-end sanity (not automated, not an agent run): in a Python shell,
      load the config, build a `SourceRegistry`, call `build_tools`, then
      `await search_web.ainvoke({"query": ..., "max_results": 3})` and feed three of
      the returned URLs to `await fetch_pages.ainvoke({"urls": [...]})`. Confirm the
      fetch content carries `[Sn]` headings and that `registry.resolve()` turns those
      markers into clickable links.
- [ ] `.env.example` and `harness.toml` between them name every setting the code
      reads, and no endpoint, model ID, or key appears as a literal in `harness/`.

## Notes

- The manual end-to-end sanity check in `## Verification` is deliberately not a test
  and deliberately not an agent: it is the one place the seams are observed together,
  since this plan builds no loop to exercise them.
- `.env.example` loses `SEARXNG_URL` and `LIGHTPANDA_CDP_URL` in Phase 1 (D3). Anyone
  with an existing `.env` must move those values into `harness.toml`.
- The loop plan inherits three things from here: tools must be driven with `ainvoke`
  (D1); a `SourceRegistry` is created per run and passed to `build_tools` once (D8);
  and `harness.toml`'s `[roles]` table is the only place a model ID is named (D3).
- crawl4ai's `RateLimiter`, `PDFCrawlerStrategy`, and result streaming
  (`CrawlerRunConfig(stream=True)`) are all documented and deliberately unused. They
  are the natural first upgrades when the budget scheduler in
  `docs/architecture.md` arrives — worth a `docs/backlog.md` entry.
- `deepagents` docs show `from langchain.tools import tool` while the reference docs
  place the symbol in `langchain_core.tools`. This plan uses the latter; the loop
  plan may switch once `langchain` is installed anyway.

## Risks

#1. **The crawl4ai↔Lightpanda pairing is documented by neither project.** crawl4ai
    documents CDP attachment (`browser_mode="custom"`, `cdp_url`) and Lightpanda
    documents a CDP server usable from Playwright's `connectOverCDP` — but nobody
    documents the two together. Lightpanda is Beta, implements only a
    Page/Network/Runtime/DOM subset, errors on commands outside it, and does not
    populate navigation timing (which has already broken at least one Playwright
    client). Phase 1 step 3 exists specifically to settle this before code depends on
    it. Mitigation is already designed in: `BrowserSettings.backend` selects
    `playwright` instead, and nothing else in the plan moves. If the smoke check
    fails, record it in `docs/decisions.md`, default to `playwright`, and add
    Lightpanda to `docs/backlog.md` rather than fighting it mid-plan.

#2. **Non-HTML and soft-block classification are heuristics, not signals.** crawl4ai
    does not content-type dispatch: a PDF yields empty markdown rather than an error,
    so `non_html` is inferred from content type when present and from "succeeded but
    empty" when not — which will also catch genuinely empty pages and JS-heavy pages
    Lightpanda failed to render. Separately, D7 classifies `blocked` from HTTP status
    alone, so a soft block returning 200 with a challenge body classifies as
    `fetched` with junk markdown. Both are accepted trade-offs; confirm during the
    Phase 3 live check that a real PDF and a real 403 land in the intended buckets,
    and log anything surprising to `docs/backlog.md` rather than widening the
    classifier mid-phase.

#3. **crawl4ai is on 0.9.x and moving.** Current release 0.9.2 (July 2026); 0.9.0 was
    a breaking release, though the breakage was scoped to the self-hosted Docker HTTP
    server rather than the in-process SDK this plan uses. Pin a version in
    `pyproject.toml` rather than taking a floating range, and treat any signature
    mismatch against the **Contracts** above as a version problem to surface, not a
    contract to quietly rewrite.

#4. **deepagents' support for async tools is not documented.** `@tool` binds
    `async def` to `coroutine` at the langchain-core level, and the compiled
    deepagents agent exposes `ainvoke` and `astream_events` — but no page states that
    `create_deep_agent(tools=[...])` accepts async tools, and sync-invoking an
    async-only tool raises `NotImplementedError`. This plan builds no agent, so
    nothing here breaks; the exposure is that the loop plan could discover it must
    supply a sync `func` alongside the `coroutine` via
    `StructuredTool.from_function`. Keep tool bodies free of anything that would
    resist a thin sync wrapper.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

### 2026-08-08 — Phase 1: step order and `pydantic-settings`

Two corrections, both approved by the developer before any Phase 1 code was written.

**1. Steps 2 and 3 are swapped.** As written, step 2 drove a crawl4ai smoke check while
crawl4ai was not installed until step 3 (`uv sync`) — the step could not be executed in
the stated order. Dependencies now install first; the smoke check is step 3. References
to "the step-2 smoke check" in step 5 and in the acceptance criteria are updated to
"step-3". No contract, requirement, or acceptance criterion changes.

**2. `pydantic-settings` is not added.** The **Files** entry named it as a runtime
dependency, but nothing in this phase's **Contracts** — nor anywhere else in this plan —
calls it: config loading is stdlib `tomllib`, plain pydantic v2 models, and `os.environ`
for resolving `api_key_env`. Adding it would be a dependency with no present call site,
which the right-sizing rule forbids. Struck through in **Files**. If the loop plan later
wants env-driven settings, it adds the dependency then, with a real caller.

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

### 2026-08-08 — Phase 1: `ProviderConfig.api_key` should probably be a `SecretStr`

**Deferred — needs a contract decision, not a cleanup.** The 3F review noted that
`api_key` is declared as a real *input* field, so two things follow: a literal
`api_key = "sk-..."` written into the checked-in `harness.toml` is accepted by
`extra="forbid"` and then silently overwritten by the env-resolved value, and the
resolved secret appears in `repr(config)` and `model_dump()`. `SecretStr`, or
`Field(repr=False, exclude=True)`, would fit the project's "no keys in files" invariant
better.

Not acted on because Phase 1's **Contracts** freeze `ProviderConfig(base_url: str,
api_key_env: str)` with `api_key: str`, and Phases 3-5 plus the loop plan are written
against that shape. Changing it is a contract amendment, so it belongs to a deliberate
decision rather than to review cleanup. Revisit when the loop plan first constructs a
chat model from a provider — that is the first code that actually reads `api_key`.

### 2026-08-08 — Phase 2: `link()` does not escape the URL destination

**Deferred — needs a contract decision, not a cleanup.** The 3F review noted that
`SourceRegistry.link()` renders `f"[{label}]({source.url})"` with no escaping, so a real
URL containing a space or an unbalanced `)` — both legal in practice — produces a broken
markdown link. That is precisely what R4 promises works ("renders as a clickable markdown
link").

Not acted on because the `[domain](url)` shape is frozen in Phase 2's **Contracts** and
matched by later phases. Both plausible fixes change it: percent-encoding the destination
alters the URL text a reader sees and copies, and the angle-bracket form `[label](<url>)`
changes the literal output later phases assert against. Revisit when the report writer
first renders citations into a delivered document — that is the first place a broken link
is actually user-visible. The three other Phase 2 review findings (IPv6 bracket loss,
`normalize_url` raising on a malformed port, and missing `get()`/title coverage) were
fixed in this phase rather than deferred.

## Phase Handoff Log

### 2026-08-08 — Phase 1: Skeleton, dependencies, and config surface
- Done: `harness/config.py` (pydantic models + `load_config`, every failure a
  `ConfigError` naming the offending field), `harness/__init__.py`, `harness.toml`,
  `tests/` with 15 passing tests, deps added and pinned (`crawl4ai==0.9.2`),
  `.env.example` reduced to keys, and `docs/{decisions,backlog,guides/setup}.md`
  updated. Committed as `e816371`; baseline scaffolding is `578e492`.
- Learned: **Risk #1 is settled — Lightpanda is out.** crawl4ai attaches over CDP fine,
  but `Page.goto` never resolves (no lifecycle event); Playwright control returns 200.
  `browser.backend = "playwright"`. Also: `uv` resolved the venv to Python **3.14** while
  mypy targets 3.12 (numpy stubs abort under 3.11) — so `requires-python = ">=3.11"` is
  not verified by anything. Nothing loads `.env`; use `uv run --env-file .env`.
  `harness.toml` ships literal `TODO` values that are **not** validated.
- Drift: two amendments in `## Reconciliations` (steps 2/3 swapped; `pydantic-settings`
  not added). One finding deferred to `## Discoveries` (`api_key` → `SecretStr`, a
  contract change).
- Watch-next: Phase 2 (`harness/sources.py`) is unflagged, offline, and depends on
  nothing from Phase 1 but the package layout — a clean start. Mirror `harness/config.py`:
  `ConfigDict(extra="forbid")`, explicit named exceptions, messages that name the
  offending value.
### 2026-08-08 — Phase 2: Source registry and citation rendering
- Done: `harness/sources.py` (`normalize_url`, `Source`, `SourceRegistry` with
  `add`/`get`/`all`/`link`/`resolve`/`unresolved_ids`) and `tests/test_sources.py` — 17
  tests, suite now 32 green. All Phase 2 contracts landed exactly as frozen; nothing
  outside the two planned files changed.
- Learned: **`normalize_url` is now total — it never raises.** A URL too malformed to
  parse (unterminated IPv6 literal, non-numeric or out-of-range port) is its own
  canonical form. Phase 3 can call `registry.add()` on model-supplied URLs without
  guarding, which R2 requires. IPv6 hosts keep their brackets (`.hostname` strips them).
  Identity collapses scheme/host case, trailing slash, default port and fragment;
  **query strings are preserved** — differing query = different source. `link()` labels
  with the bare hostname, `www.` NOT stripped.
- Drift: none. Three 3F findings fixed in-phase (IPv6 brackets, `normalize_url`
  totality, missing `get()`/title coverage); one deferred to `## Discoveries`
  (`link()` does not escape the URL destination — a frozen-contract question).
- Watch-next: Phase 3 (fetch tool) is flagged (!#1, !#2, !#3) and is the first phase with
  real external dependencies. Risk #1 is already settled — `browser.backend =
  "playwright"`, Lightpanda is out (see `docs/decisions.md`); do not re-attempt the CDP
  pairing. Assign IDs by calling `SourceRegistry.add()` — never mint IDs inside the fetch
  tool — and read every limit from `FetchSettings`/`BrowserSettings`, no literals.
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. -->
