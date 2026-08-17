# Architecture

## Overview

To be documented as code is written. Direction so far: an orchestrator–worker
agent loop — a smart model (GLM 5.2, DeepSeek V4 Pro as config-swappable
fallback) plans and synthesizes, while a cheap fast model does parallel
triage/extraction under a rate/token budget scheduler. The worker role is
itself config-swappable (e.g. to an OpenCode-served model) if the initial
choice proves rate-limit-constrained in practice.

## Directory Structure

`harness/` holds the source: @harness/config.py (TOML config models),
@harness/sources.py (per-run source registry), @harness/prompts.py (prompt
loader) with its `.md` files in @harness/prompts/, and @harness/tools/ (the
tool registry and one module per tool). Tests live in `tests/`, mirroring
the source modules. `harness.toml` sits at the repo root. Reports will be
timestamped markdown files on disk (not yet built).

## Principles & Invariants

Full rule set with rationale lives here as it's established; the always-load
subset is in @docs/INDEX.md → CLAUDE.md `## Invariants`.

## Key Patterns

Tools are LangChain-native async callables: built with `@tool`, declared
`response_format="content_and_artifact"`, and always driven with `ainvoke`.
Each tool is built by a `build_<name>_tool(config, ...)` factory in its own
module, and @harness/tools/__init__.py lists every one explicitly in
`build_tools` — adding a tool means a new module plus one line there. Tool
boundaries return typed failure values instead of raising exceptions. A
per-run @harness/sources.py `SourceRegistry` assigns `[Sn]` citation IDs as
pages are fetched. Config is TOML (@harness/config.py), with secrets named
by env var and never inlined.

## Dependencies

Runtime: `pydantic`, `langchain-core`, `crawl4ai` (pinned 0.9.2), `httpx`.
Dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy`. Deliberately not depended
on: `deepagents`, `langchain`, `langgraph`, `pydantic-settings`.

## Concurrency Bounds

Fetch is HTTP-first: every URL goes through crawl4ai's HTTP strategy under a per-attempt
deadline, and Chromium is launched only to escalate a page whose extracted markdown reads
like a JS shell. Three `harness.toml` keys bound the load. `fetch.http_concurrency` (10)
caps in-flight HTTP fetches per tool call, `fetch.browser_concurrency` (2) caps concurrent
Chromium escalations — deliberately low, since dropping crawl4ai's `arun_many` also dropped
its `MemoryAdaptiveDispatcher` memory backpressure — and `max_subagents` (3) is the number
of subagents the agent loop may run at once.

`max_subagents` is a **declared contract, not runtime enforcement** (D6): no agent loop
exists yet, so nothing counts or schedules subagents. When the loop is built it must honor
this key. Worst-case concurrent fetch load once it does is
`max_subagents * fetch.http_concurrency` = 3 * 10 = **30 concurrent HTTP requests**, plus up
to `max_subagents * fetch.browser_concurrency` = 6 concurrent Chromium renders. Raising
either factor multiplies that product, and raising `browser_concurrency` in particular
warrants a memory measurement on the box first.

## Failure Modes

To be documented as code is written.
