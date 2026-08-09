# Fast Research Harness

Self-hosted, Perplexity-style research agent with cited sources, built as a
reusable agent-loop/tool-registry harness (research is the first toolpack,
not the ceiling).

## Commands

### Root

| Command | Description |
|---|---|
| `uv run pytest` | Test suite (offline, fixture-based) |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Format check |
| `uv run mypy .` | Typecheck |

## Quality Gate
- test: uv run pytest
- lint: uv run ruff check .
- format: uv run ruff format --check .
- typecheck: uv run mypy .

## Architecture

- `docs/` — project documentation (see docs/INDEX.md).
- `.claude/` — workflow config, plans, and agent scaffolding.
- Source layout not yet established — see docs/architecture.md, filled in
  as `/devlead` and `/implement` produce it.

## Invariants

- No shell tool in the tool registry — filesystem read/grep/glob is
  read-only; writes are confined to a designated workspace + reports dir.
- Model routing (orchestrator, fallback, worker) is config/env-driven —
  never hardcode endpoints, model IDs, or keys.
- Adding a new tool must require no changes to the agent loop itself.
- Best-effort + disclose: degraded coverage (rate limits, fetch failures)
  is answered and disclosed, never silently thinned or hidden.

## Stack

- Language: Python (>=3.11), managed with uv.
- Dev tooling: ruff (lint + format), mypy (typecheck, targets 3.12 — see
  docs/decisions.md), pytest.
- No database — reports are timestamped markdown files on disk.
- Fetch/extraction: crawl4ai over crawl4ai-managed Playwright/Chromium
  (Lightpanda was tried and retired — see docs/decisions.md).
- Search: self-hosted SearXNG (JSON API).
- Models: OpenCode API (GLM 5.2 default orchestrator, DeepSeek V4 Pro
  fallback); Cerebras API (Gemma 4 31B default worker, config-swappable).
- Deployment: homelab Linux machine, operated over SSH.

## Patterns

- Commits: conventional commits — `type(scope): summary`.
- PR bodies follow .github/pull_request_template.md.

## Code Reuse (CRITICAL)
Before creating ANY new utility, helper, hook, or shared component:
1. Search existing code for similar implementations
2. Check @docs/INDEX.md for documented shared resources
3. If similar functionality exists — extend it, don't duplicate it
If unsure whether something exists, ASK rather than creating a new one.

The same applies within files and tests: repeated setup becomes a fixture
(see tests/conftest.py), repeated model/config settings become a shared base
class, and a constant or policy statement lives in exactly one place. If the
same lines are about to appear a third time, factor them out instead of
pasting them again.

## Documentation

See @docs/INDEX.md for the full documentation map. Requirements live in
docs/requirements/ (empty — none written yet), plans in docs/plans/,
deferred work in docs/backlog.md, decision log in docs/decisions.md.

## Workflow Commands
- `/iterate` — Fast path for Small/Medium changes (test-first loop against a ledger)
- `/iterate-continue` — Resume a paused /iterate session after a /clear
- `/requirements-gathering` — Interrogate requirements for larger work (precursor to /devlead)
- `/devlead` — Start a planning session (produces plan in docs/plans/)
- `/implement` — Execute a plan using subagents
- `/handoff` — Capture session context for a clean handoff
- `/continue` — Resume from the previous session's handoff
