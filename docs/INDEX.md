# Documentation Index

## Project Context

- **Deployment:** self-hosted homelab Linux machine, operated interactively
  over SSH. No cloud/container deployment.
- **Status:** greenfield — no source code yet.
- **Integrations:** SearXNG (existing Docker instance, JSON API — configured),
  self-hosted Lightpanda via crawl4ai over CDP (TODO — deploy), Cerebras API
  for worker-model triage (TODO — key), OpenCode API for orchestrator/
  synthesis models (TODO — key). All keys/endpoints via `.env` (see
  docs/guides/setup.md), never hardcoded.
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
| (none yet) | — | — |
