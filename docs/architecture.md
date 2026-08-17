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

## Failure Modes

To be documented as code is written.
