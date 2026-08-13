# Documentation Index

## Project Context

- **Deployment:** self-hosted homelab Linux machine, operated interactively
  over SSH. No cloud/container deployment.
- **Status:** harness substrate built — config, source registry, fetch and search
  tools, prompt loader, and the tool list. No agent loop yet.
- **Integrations:** SearXNG (local Docker instance checked in at
  @searxng/docker-compose.yml, JSON API enabled — a stock container is HTML-only
  and will not work), crawl4ai over
  crawl4ai-managed Playwright/Chromium, Cerebras API for worker-model triage (TODO — key),
  OpenCode API for orchestrator/synthesis models (TODO — key). API **keys** live
  in `.env`; **endpoints, model IDs and limits** live in `harness.toml` (see
  docs/guides/setup.md). Neither is ever hardcoded.
- **CI:** GitHub Actions, running on a self-hosted runner with the default tags
  (`self-hosted`, `Linux`, `X64`). One workflow, @.github/workflows/ci.yml, runs the four
  quality gates plus a 90% coverage floor on pull requests to `main` and pushes to `main`.
  The runner's own configuration is deliberately not recorded here — see the Phase 4 entry
  in @docs/plans/PLAN-ci-pipeline.md `## Reconciliations`.
- **Constraints:** Python; no shell tool in the tool registry; file writes
  confined to a designated workspace + reports directory; model routing
  (orchestrator + fallback + worker) is config-driven, not hardcoded.

## Documentation Map

| Path | Contents |
|---|---|
| docs/requirements/ | REQUIREMENTS docs from `/requirements-gathering` |
| docs/plans/ | Implementation plans from `/devlead` |
| docs/architecture.md | System architecture, principles, invariants |
| docs/backlog.md | Deferred work and predicted issues |
| docs/decisions.md | Decision log |
| docs/guides/setup.md | Install, env vars, prerequisites |

## Shared Resources

| Resource | Location | Purpose |
|---|---|---|
| Config models | @harness/config.py | TOML-backed `HarnessConfig` and settings, secrets by env var |
| Source registry | @harness/sources.py | Per-run registry assigning `[Sn]` citation IDs to fetched pages |
| Prompt loader | @harness/prompts.py | Loads/renders `harness/prompts/*.md` `$variable` templates |
| Tool registry | @harness/tools/ | `build_tools` and the per-tool `build_<name>_tool` factories |
