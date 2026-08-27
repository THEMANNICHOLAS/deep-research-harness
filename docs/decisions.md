# Decisions

Project decision log — the durable "why" behind non-obvious choices. One
entry per decision: what was decided, why, and what was rejected. 1-3
sentences each, append-only, newest last.

## Entries

- **Browser backend defaults to `playwright`, not `lightpanda`.** Smoke-tested crawl4ai
  `0.9.2` against `lightpanda/browser` `1.0.0-nightly.8578` over CDP: attachment
  succeeded, but `Page.goto` timed out after 60s waiting on `domcontentloaded`, while a
  Playwright control run on the same URL returned 200 — Lightpanda accepts the CDP
  connection and never emits the lifecycle event, consistent with its documented
  Page/Network/Runtime/DOM subset. `lightpanda` stays a declared
  `BrowserSettings.backend` value in @harness.toml; the gap and the `--advertise-host`
  trap are tracked in @docs/backlog.md.

- **mypy targets Python 3.12 although `requires-python` allows 3.11.** numpy's stubs
  (transitive via crawl4ai, reached through `pytest.approx`) use a PEP 695 `type`
  statement that is a syntax error under `python_version = "3.11"` and aborts the entire
  run. Rejected the narrower fix (a `numpy.*` `follow_imports = skip` override); the
  accepted cost is that 3.11-incompatible syntax in @harness/ is not caught by the type
  checker. See @pyproject.toml.

- **The Lightpanda/CDP backend is removed; Chromium via crawl4ai-managed Playwright is
  the only path, and no config key selects a browser.** `BrowserSettings` and the earlier
  entry's declared `lightpanda` value are gone — its smoke-test failure was never resolved
  and the branch was dead code besides. Its @docs/backlog.md pointer is now dangling, and
  re-adding a second backend means re-adding the config surface. See @harness/config.py and
  @harness/tools/fetch.py.

- **Truncation always cuts at the latest structural boundary, with no minimum-yield floor.**
  The `_MIN_BOUNDARY_FRACTION` guard (fall back to the hard cut when the boundary sat below
  60% of the cap) was removed as unmotivated complexity — it never fired in live use. A page
  whose only boundary is near the top now returns that much and says it was truncated; the
  no-boundary and boundary-at-0 cases still take the whole allowance. See
  @docs/plans/PLAN-crawler-refinement.md Reconciliation #3.

- **2026-08-09 — `deepagents==0.7.5` installed-package check (Phase 1, PLAN-research-loop).**
  Pinned and resolved cleanly from PyPI. Observed default backend: `StateBackend()` — matches
  the plan's `## Background` expectation (`graph.py`: `backend = backend if backend is not
  None else StateBackend()`). Observed default middleware on the main agent (no `subagents`/
  `skills`/`memory` passed): `FilesystemMiddleware`, a summarizer, `PatchToolCallsMiddleware`,
  `SubAgentMiddleware`, plus any harness-profile extra middleware and prompt-caching
  middleware. `SubAgentMiddleware` is in the **default** stack, not an opt-in: a
  `general-purpose` subagent is auto-added unless the profile disables it or the caller passes
  its own `subagents` — which is why Phase 3 must disable it explicitly rather than assume its
  absence. The summarizer is deepagents' own `_DeepAgentsSummarizationMiddleware` wrapper
  (@.venv/Lib/site-packages/deepagents/middleware/summarization.py), **not** langchain's plain
  `SummarizationMiddleware` — D7's `keep`-policy configuration in Phase 3 must be written
  against the wrapper's surface. `TodoListMiddleware` is confirmed **opt-in, and available** —
  it ships in `langchain` 1.3.14, not in `deepagents`' own namespace, so it is imported as
  `from langchain.agents.middleware import TodoListMiddleware`. Verified empirically: a default
  `create_deep_agent(model=..., tools=[])` graph carries no todo node, and passing
  `middleware=[TodoListMiddleware()]` adds `TodoListMiddleware.after_model`. This matches the
  plan's `## Background` and confirms D9 is implementable as written in Phase 3. A `graph.py`
  comment claims deepagents trims the middleware's full default prompt via `system_prompt=""`,
  but no code does so — the only instantiation in the package is a bare `TodoListMiddleware()`
  in one harness profile, so Phase 3 should expect its full default prompt unless it trims it.

- **2026-08-09 — deepagents seams observed against the installed package (Phase 3,
  PLAN-research-loop).** Four things the plan left to be confirmed rather than assumed.
  **Async tools:** the substrate's coroutine-only `fetch_pages`/`search_web` were accepted by
  `create_deep_agent` unchanged and the compiled graph drives fine under `ainvoke`/`astream` —
  risk !#1 is retired and no sync wrapper was needed, so @harness/tools/fetch.py and
  @harness/tools/search.py stay untouched. **Prompt composition:** deepagents places our
  rendered prompt at the HEAD of a single system message and appends its own middleware
  prompts after it (observed: our 2605 chars intact at the front of a 4114-char system
  message), so `render()` output arrives whole and risk !#3's "surviving JSON convention"
  hazard was addressed by rewriting @harness/prompts/orchestrator.md, not by fighting
  composition. **Disk backend:** `FilesystemBackend` ships and implements plain
  `BackendProtocol`, not `SandboxBackendProtocol` — risk !#8 is retired and D6's custom-backend
  contingency was never needed. **Summarizer:** passing our own summarization middleware
  REPLACES deepagents' default rather than stacking a second one, because
  `_apply_custom_middleware` merges by middleware `.name` and both classes publish the same
  name; the compiled graph carries exactly one summarization node.

- **2026-08-09 — the `execute` shell tool cannot be removed from deepagents' graph, only from
  the model's reach (Phase 3, PLAN-research-loop).** `FilesystemMiddleware` registers `execute`
  unconditionally on every backend, so it is always present in the compiled graph's tool
  registry — the plan's assumption that only `LocalShellBackend` and the sandbox backends carry
  it was wrong. Two independent defenses were taken instead of absence: a registered
  `HarnessProfile` excludes `execute` so it never enters the schema passed to the model
  (verified — the model is offered ten tools and `execute` is not among them), and
  `FilesystemBackend` is not sandbox-capable, so a call that somehow arrived would return an
  in-band error rather than run anything. The project's own tool registry, @harness/tools/,
  remains shell-free. See the Reconciliations section of @docs/plans/PLAN-research-loop.md.

- **2026-08-09 — disabling deepagents' auto-injected `general-purpose` subagent needs a
  process-global profile registration (Phase 3, PLAN-research-loop).** There is no
  `create_deep_agent` keyword for it and `subagents=[]` behaves identically to `subagents=None`;
  the only supported route is registering a harness profile that disables it, keyed by the
  `provider:model-name` string deepagents derives from the model instance. @harness/agent.py
  derives that key from the model object rather than hardcoding it, and the resulting graph has
  no `task` tool and no `SubAgentMiddleware`. Accepted residue: the registry is process-global
  and not scoped to our `base_url`, so the registration would match any client using the same
  model name in the same process — harmless for a single-agent CLI, revisit when the pyramid
  builds two agents wanting different profiles for one model ID.

- **2026-08-09 — the summarizer must be deepagents' wrapper, not langchain's plain one (Phase 3,
  PLAN-research-loop).** Both classes publish the same middleware `.name`, so either one
  cleanly replaces the default — but only deepagents' wrapper offloads evicted messages to a
  conversation-history file on the backend before dropping them from the model's context;
  langchain's issues a destructive remove-all with no recovery path, which is exactly D7's
  stated failure mode for a dropped `[Sn]`-to-finding association. The wrapper also acts through
  `wrap_model_call` rather than the legacy `before_model` hook, so it does **not** shrink the
  graph's own message list — which is what keeps R7's token sum in @harness/__main__.py honest
  on a run long enough to compress. Shipping langchain's plain middleware would have silently
  undercounted the delegation baseline. See @harness/agent.py.

- **2026-08-10 — `[roles.head]` is `deepseek-v4-flash`, and the single-agent token baseline is
  ~796k tokens for one research run (Phase 3, PLAN-research-loop).** The head role was moved off
  `kimi-k3` to `deepseek-v4-flash` on cost grounds; it needs an explicit region opt-in on the
  OpenCode workspace dashboard, and without that opt-in the endpoint returns a 403 `RegionError`
  rather than anything a config check could predict. Measured on the first passing end-to-end
  run (one question, 19 sources consulted, 12 minutes): 773,032 input tokens and 22,883 output
  tokens of which 16,539 were reasoning — 795,915 total. Input dominates by ~34x because the
  whole research history is resent every turn, so the pyramid's 3-10x delegation multiplier
  should be priced against the INPUT figure, not the total. `deepseek-v4-flash` is a reasoning
  model like `kimi-k3` before it, so the reasoning split stays the meaningful number.

- **2026-08-14 — each paragraph's `Sources:`/`Verdict:` pair is bold and blank-line separated,
  and the verdict detail is capped at 25 words (PR #10, supersedes PLAN-report-output Phase 3's
  rendered-format contract).** The first live report showed the pair rendering flush against the
  last prose line and unstyled, where it read as two more sentences of the paragraph rather than
  as machinery about it, and verdict details running 40-60 words. The pair now sits after a
  blank line with `**Sources:**` / `**Verdict:**` labels, and the `Sources:` line ends in
  markdown's two-space hard break — without it a renderer joins the two labels onto one line,
  and a blank line between them instead would space the pair further apart than the paragraph it
  belongs to. The 25-word cap lives in @harness/prompts/verify.md alone: rejected a code-level
  trim in @harness/report.py because clipping mid-sentence is worse to read than a long verdict,
  so a model that ignores the instruction still gets its full sentence printed.

- **2026-08-14 — the run display is a full-screen Rich TUI on the alternate screen buffer
  (D1, PLAN-fail-fast-and-pinned-checklist).** Pinned todo checklist over a gray rule over a
  fixed event-log panel, `Live(screen=True)`, no Textual dependency; rejected a Textual app
  (new pinned dep, ~2x diff) and a non-fullscreen Live block. Prints made while the Live runs
  are discarded with the alt buffer, so questions and stage-timeline lines render via the
  frame or after suspend — see @harness/display.py.

- **2026-08-14 — failed runs write no report (D2, PLAN-fail-fast-and-pinned-checklist).**
  Hard errors, user abort (Ctrl+C), and wall-clock expiry with no final answer produce stderr
  error + exit 1 and nothing in reports/; round cap and post-answer wall-clock expiry stay
  disclosed reports. This narrows the best-effort + disclose invariant to runs that finish
  (CLAUDE.md reworded to match).

- **2026-08-14 — mid-run SearXNG outage aborts via a counter inside the search tool closure
  (D3, PLAN-fail-fast-and-pinned-checklist).** Three consecutive connection-level failures
  (`unreachable`/`bad_status`; `malformed` neither counts nor resets) raise
  `SearchUnavailableError`, which propagates through deepagents to the loop's generic handler
  — verified empirically; no agent-loop changes, preserving the tools-don't-touch-the-loop
  invariant. Limit: `[search] max_consecutive_failures`.

- **2026-08-14 — the startup health check probes the SearXNG JSON API, not the container
  (D4, PLAN-fail-fast-and-pinned-checklist).** One GET of `{base_url}/search?q=ping&format=json`
  asserting 200 + parseable JSON catches both container-down and the HTML-only
  stock-container misconfiguration; rejected docker inspection (hardcodes a deployment
  detail). See `preflight_search` in @harness/tools/search.py.

- **2026-08-14 — the reader is wired as a deepagents-native declared `SubAgent`, delegated to
  through the framework's own `task` tool (PLAN-reader-delegation D1).** Rejected a custom
  `read_source(url)` tool wrapping a self-compiled reader agent (drives a second agent loop
  inside a tool coroutine, breaks the "only agent.py imports deepagents" boundary) and a
  single-call digest folded into the fetch tool (cheaper, but drops the bound spawned-subagent
  shape R1 requires). The lead's toolset physically excludes `fetch_pages`; the reader's tools
  carry the SAME instance so the shared `SourceRegistry` and `sources/` directory keep R3/R4
  intact across the delegation boundary. See @docs/plans/PLAN-reader-delegation.md.

- **2026-08-14 — a reader crash is bounded-retried once, then converted to an error
  `ToolMessage`, with a lead-visible `fetch_raw` fallback recovering the raw page
  (PLAN-reader-delegation D2).** Built from langchain's existing `ToolRetryMiddleware`/
  `ToolErrorMiddleware`, scoped to the `task` tool alone, rather than a middleware that parses
  URLs out of task descriptions and drives crawl4ai itself (heaviest, most fragile option).
  Retry count is pinned at 1: retrying `task` re-runs the whole reader session, so the budget
  already doubles. See @docs/plans/PLAN-reader-delegation.md.

- **2026-08-14 — the reader reuses `[roles.subagent]` rather than a new `[roles.reader]` config
  role (PLAN-reader-delegation D3).** No present variation point justifies a config surface
  split; revisit when the researcher tier is wired and the two tiers want different models.
  See @docs/plans/PLAN-reader-delegation.md.

- **2026-08-14 — read-mode (digested/fallback/unread) is recorded as a field on
  `SourceRegistry`'s `Source`, not derived from parsing `<undigested>` markers out of capture
  files (PLAN-reader-delegation D4).** The registry is already the per-source metadata home
  @harness/report.py walks to render disclosure; string-parsing capture bodies would couple
  report rendering to page text it must not trust to parse. See
  @docs/plans/PLAN-reader-delegation.md.

- **2026-08-14 — one delegation may carry one or more URLs, bounded by
  `fetch.max_urls_per_call`, rather than a strict one-URL-per-task rule
  (PLAN-reader-delegation D5).** A strict one-URL rule would multiply the 3-10x delegation
  overhead per page with no fidelity gain; the orchestrator prompt states the batching bound
  instead. See @docs/plans/PLAN-reader-delegation.md.

- **2026-08-14 — "digested" is marked at the delegation boundary, not at fetch time (PR #13
  review).** The reader's `fetch_pages` call only nominates the source IDs it captured
  (@harness/sources.py `note_digest_candidate`, context-local per `task` attempt);
  @harness/agent.py's `_ReaderDigestMiddleware` promotes them to `digested` only when the
  task call returns a non-empty digest. Rejected marking inside the fetch tool: a reader that
  fetched then crashed (or returned empty) left sources disclosed as "Digested via the
  reader" though no digest ever reached the lead — a false-positive disclosure against R5.
  For the same reason `fetch_raw` never downgrades an already-`digested` source.

- **2026-08-14 — `[roles.head]` moved from `deepseek-v4-flash` to `deepseek-v4-pro`
  (developer request).** Confirmed `deepseek-v4-pro` is listed by the OpenCode
  `/models` endpoint and returns 200 on a live `chat/completions` call with the
  existing `OPENCODE_API_KEY` and no additional region opt-in — the flash-tier's
  documented 403 gotcha did not reproduce for pro. `[roles.subagent]` is unchanged
  (`gpt-5.6-luna`).

- **2026-08-15 — degraded-coverage incidents flow through a dedicated per-run `RunLog`
  (@harness/runlog.py), not the `SourceRegistry` (developer decision via AskUserQuestion).**
  Tools record search failures, failed/blocked fetches, dropped malformed results, and
  capture-write failures; `__main__` echoes each as a terminal `Alert` and the report lists
  them under `## Gaps and disclosures` even when verification never ran. Chosen over hanging
  incidents off the registry to keep the citation registry single-purpose and give the future
  TUI/toolpacks a neutral seam. One shared instance per run — builders default a missing log
  only so incident-agnostic tests stay unchanged.

- **2026-08-15 — CLI startup cut from ~6s to ~0.5s: lazy imports plus removing
  `langchain-google-genai` via a uv dependency override (@pyproject.toml `[tool.uv]`).**
  crawl4ai imports inside `_fetch` (first fetch pays it, overlapped with model latency);
  `harness.agent`/`harness.models`/langgraph import inside `main()` after the renderer starts;
  `verify.py` defers `harness.models` into `verify_paragraphs` so report/verify stay light.
  deepagents only imports langchain-google-genai inside try/except ImportError, so the
  impossible-marker override (`sys_platform == 'never'`) drops ~2s of google.genai import —
  MUST be revisited on any deepagents bump (an unconditional import there would crash).
  Capture-file policy (`FETCH_FAILED_PREFIX`/`is_failed_capture`/`sources_dir`) moved to
  @harness/sources.py so report/verify/conftest no longer drag crawl4ai.

- **2026-08-15 — `build_chat_model` is called as a module attribute
  (`models.build_chat_model(...)`) everywhere, never imported by value.** Tests patch the one
  definition (`harness.models.build_chat_model`); the old three-target patch list in
  tests/conftest.py let any new by-value importer silently dial the real endpoint. Tests
  needing DIFFERENT clients for the lead vs the verification pass now dispatch inside the one
  patched callable (by role, or by main()'s fixed resolution order: preflight, lead, reader,
  verify).

- **2026-08-15 — the round cap is counted by `__main__`'s stream loop in MODEL TURNS;
  `recursion_limit` is only a runaway backstop (`max_rounds * 20 + 100`).** The old
  `max_rounds * 2 + 1` mapping assumed 2 supersteps per round, but middleware `after_model`
  nodes sit inside the loop (4/round measured), silently halving the advertised budget — a
  20-round config delivered ~10. Supersteps are framework topology and drift on upgrade;
  turns are counted in code we own (deduped by message id; the reader subagent's internal
  turns never reach the outer stream, so rounds are the lead's). The cap is now run-level —
  clarification resumes no longer refresh it — and a run capped mid-research gets ONE bounded
  synthesis pass (`_SYNTHESIZE_NOW`, recursion_limit 10) after the capped round's tools
  finish, so the report carries a real final answer instead of mid-run chatter, with the
  round cap still disclosed. Reusable rule: reach for recursion_limit as a crash-stop
  backstop, never as a semantic budget.

- **2026-08-15 — Phase 2 model availability verified live; rate limits unexamined by choice.**
  `preflight` against opencode succeeded for both new Phase 2 models: `kimi-k3` OK and
  `deepseek-v4-flash` OK (the existing region opt-in covers `-flash`; no new opt-in step was
  required). The R6 role list stands with no substitutes.
  The OpenCode dashboard exposes no RPM/TPM figures the developer could find; decision
  (developer, 2026-08-15): ignore limits until one is actually hit, so Step 3's
  researcher-count guidance uses a conservative default fan-out rather than a measured bound.

- **2026-08-25 — `[roles.verifier]` moved from `gpt-5.6-luna` to `qwen3.7-plus`
  (developer request).** The OpenCode endpoint started 500ing on every
  `gpt-5.6-luna` preflight chat call (`Internal server error`), surfaced as a
  fail-fast startup abort. `qwen3.7-plus` was already a listed `[roles.head]`
  `choices` slug; swapped in with no other config changes.

- **2026-08-27 — Synthesis reserve enforced inside researcher dispatches (research-throughput D1).**
  `_ResearcherDispatchMiddleware` wraps the lead's `task` tool: each researcher runs under
  `asyncio.wait_for(remaining-until-margin)` and, past the margin, new dispatches are refused with
  a "synthesize now" ToolMessage; both record `research_deadline_reached`. Rejected: a sibling
  timer cancelling the whole `astream` — loses the in-flight superstep and races the hard clock.

- **2026-08-27 — Guard requires directive context (research-throughput D2).** The bare `system`
  rules fire only when followed (same/next line, blank lines allowed) by second-person or
  imperative instruction text, and `exfil_markup` only on the image form or a template-syntax
  query value, so compose YAML, INI `[system]`, spec lines, shell snippets and `?apikey=` docs
  links survive. Rejected: dropping the rules (loses `SYSTEM: you are now...`) and skipping
  fenced regions (an attacker wraps the payload in a fence).

- **2026-08-27 — One run-wide HTTP pool sized by `fetch.max_connections` (research-throughput D3).**
  `max_concurrency` keeps its per-call dispatcher-permit meaning; `memory_threshold_percent`
  (crawl4ai's 90.0) replaces a 75.0 constant. Rejected: one crawler per researcher dispatch —
  tools are built once per run, so it needs a dispatch-scoped registry for the same connections.

- **2026-08-27 — Search-failure abort stays run-wide (research-throughput D4).** No dispatch identity
  reaches a `@tool`, and run-wide counting is the right detector for "SearXNG is down"; the R4
  per-researcher clause was trimmed rather than adding a contextvar.

- **2026-08-27 — Researcher fan-out enforced by middleware, search budget prompt-only
  (research-throughput D5).** `max_concurrent_researchers` is an in-flight CONCURRENCY cap
  (later waves allowed) refused with `researcher_budget_exhausted`; `searches_per_researcher` is
  rendered into the prompt only, since the reader-dispatch cap already bounds per-researcher
  cost. Rejected: prompt-only fan-out — the failed 1800s run showed prose limits do not hold.

- **2026-08-27 — Per-role request timeouts; task retry replays only transient failures
  (research-throughput D6).** `RoleConfig.request_timeout_seconds` (None → `[agent]` value)
  bounds researcher/reader at 60s while head/verifier keep 120s. `_NON_RETRYABLE_TASK_FAILURES`
  adds `openai.BadRequestError` and the builtin `TimeoutError` to the retry exclusion as a
  separate superset of `_PASS_THROUGH_TASK_FAILURES`, which doubles as the failure handler's
  propagate list; `openai.APITimeoutError` subclasses `APIConnectionError` and stays retryable.
