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
  read-only; the agent's writes are confined to the workspace dir alone.
  The reports dir is written by `harness/report.py`, not reachable from
  the agent's backend at all.
- Model routing (orchestrator, fallback, worker) is config/env-driven —
  never hardcode endpoints, model IDs, or keys.
- Adding a new tool must require no changes to the agent loop itself. The one
  standing exception is a tool that must STOP the loop to work — `ask_user`
  is registered in `harness/agent.py`'s `interrupt_on`, because an interrupt
  is a property of the loop, not of the tool. Everything else stays additive.
- Best-effort + disclose: degraded coverage (rate limits, fetch failures)
  is answered and disclosed, never silently thinned or hidden.

## Stack

- Language: Python — `requires-python` floor is 3.11, but `.python-version`
  pins 3.12 for uv and CI, matching mypy's target (see docs/decisions.md).
- Package manager: uv, itself pinned to exactly 0.12.3 by `[tool.uv]
  required-version` in @pyproject.toml — any other uv refuses to run every uv
  command. CI installs its uv from that same key.
- Runtime deps (declared in @pyproject.toml): pydantic, langchain-core,
  crawl4ai (pinned `==0.9.2`), httpx. Dev deps: ruff, mypy, pytest,
  pytest-asyncio, pytest-cov (pinned `==7.1.0`, pulls in coverage).
- Everything not marked pinned above still carries a `>=` floor and so floats
  on re-resolve; `uv.lock` holds the resolved set and is the source of truth for
  what is actually installed (referenced by name, not `@` — an `@` prefix would
  inline the whole ~559k-token lockfile into every session's context).
  Converting those floors to `==` is a docs/backlog.md item.
- No database — reports are timestamped markdown files on disk.
- Fetch/extraction: crawl4ai over crawl4ai-managed Playwright/Chromium.
- Search: self-hosted SearXNG (JSON API).
- Models: OpenCode API serves both roles — `[roles.head]` = `deepseek-v4-flash`,
  `[roles.subagent]` = `gpt-5.6-luna`. Config-swappable; no other provider is
  declared today. `deepseek-v4-flash` requires a region opt-in on the OpenCode
  workspace dashboard — without it the endpoint 403s (see docs/decisions.md).
- Deployment: homelab Linux machine, operated over SSH.

## Patterns

- Commits: conventional commits — `type(scope): summary`.
- PR bodies follow .github/pull_request_template.md.
- Pin every dependency to an exact version (`==X.Y.Z`), never a `>=` floor — a
  floor still lets the resolved version float on the next re-resolve. This
  applies to tools and actions too: uv via `required-version`, GitHub Actions by
  commit SHA with a trailing version comment.

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
