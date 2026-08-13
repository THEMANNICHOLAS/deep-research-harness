# Documentation Index

## Project Context

- **Deployment:** self-hosted homelab Linux machine, operated interactively
  over SSH. No cloud/container deployment.
- **Status:** research loop runs end to end — `python -m harness "<question>"` drives a
  single deepagents lead agent over the substrate's tools, may ask clarifying questions
  before researching, stops at a round cap or wall clock, checks each claim against its own
  cited source, and writes a timestamped cited report. All seven phases of
  docs/plans/PLAN-research-loop.md are built. The researcher and reader tiers exist only as
  frozen prompt contracts — nothing delegates to them yet; wiring them is the next round.
- **Integrations:** SearXNG (local Docker instance checked in at
  @searxng/docker-compose.yml, JSON API enabled — a stock container is HTML-only
  and will not work), crawl4ai over
  crawl4ai-managed Playwright/Chromium (Lightpanda was tried and retired — see
  docs/decisions.md), OpenCode API for both model roles — `deepseek-v4-flash` for the
  head and `gpt-5.6-luna` for the subagent. API **keys** live
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
| Model clients | @harness/models.py | `build_chat_model` / `preflight` — role → chat client, fail-fast and bounded retry |
| Lead agent | @harness/agent.py | `build_agent` — the deepagents lead, its backend, middleware and interrupts |
| Source registry | @harness/sources.py | Per-run registry assigning `[Sn]` citation IDs to fetched pages |
| Claim verification | @harness/verify.py | `extract_claims` / `verify_claims` — per-claim check against its own captured source |
| Report assembly | @harness/report.py | `RunOutcome` + `write_report` — marker placement, citation resolution, disclosure sections |
| Prompt loader | @harness/prompts.py | Loads/renders `harness/prompts/*.md` `$variable` templates |
| Tier contracts | @harness/prompts/subagent.md, @harness/prompts/reader.md | Frozen researcher and reader delegation contracts — unwired |
| Tool registry | @harness/tools/ | `build_tools` and the per-tool `build_<name>_tool` factories |
