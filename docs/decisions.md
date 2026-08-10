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
