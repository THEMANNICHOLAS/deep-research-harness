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
