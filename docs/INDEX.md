# Documentation Index

## Project Context

- **Deployment:** self-hosted homelab Linux machine, operated interactively
  over SSH. No cloud/container deployment.
- **Status:** harness substrate built — config, source registry, fetch and search
  tools, prompt loader, and the tool list. No agent loop yet.
- **Integrations:** SearXNG (local Docker instance checked in at
  @searxng/docker-compose.yml, JSON API enabled — a stock container is HTML-only
  and will not work), crawl4ai over
  crawl4ai-managed Playwright/Chromium (Lightpanda was tried and retired — see
  docs/decisions.md), OpenCode API for both model roles — `kimi-k3` for the head
  and `gpt-5.6-luna` for the subagent. API **keys** live
  in `.env`; **endpoints, model IDs and limits** live in `harness.toml` (see
  docs/guides/setup.md). Neither is ever hardcoded.
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
