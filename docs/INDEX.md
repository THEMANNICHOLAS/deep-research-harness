# Documentation Index

## Project Context

- **Deployment:** self-hosted homelab Linux machine, operated interactively
  over SSH. No cloud/container deployment.
- **Status:** research loop runs end to end — `python -m harness "<question>"` drives a
  deepagents lead agent over the substrate's tools, may ask clarifying questions
  before researching, stops at a round cap or wall clock, checks each claim against its own
  cited source, and writes a timestamped cited report. Runs are fail-fast: SearXNG is
  health-checked before any agent work, three consecutive mid-run search connection
  failures abort the run, and a failed run (hard error, user abort, answer-less wall-clock
  expiry) writes no report and exits nonzero (PLAN-fail-fast-and-pinned-checklist). On a
  TTY the run renders as a full-screen TUI: pinned todo checklist over a scrolling event
  log, post-run summary on the normal terminal. All seven phases of
  docs/plans/PLAN-research-loop.md are built. The reader tier is now wired
  (docs/plans/PLAN-reader-delegation.md): the lead delegates page reading to a declared
  `reader` subagent rather than fetching directly, a bounded-retry `fetch_raw` fallback
  recovers a failed or empty digest, and the report discloses which sources were digested,
  fell back raw, or went unread. The researcher tier remains only a frozen prompt contract —
  nothing delegates to it yet.
- **Integrations:** SearXNG (local Docker instance checked in at
  @searxng/docker-compose.yml, JSON API enabled — a stock container is HTML-only
  and will not work), crawl4ai over
  crawl4ai-managed Playwright/Chromium (Lightpanda was tried and retired — see
  docs/decisions.md), OpenCode API for both model roles — `deepseek-v4-pro` for the
  head and `gpt-5.6-luna` for the subagent. API **keys** live
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
| LATER-PROBLEMS.md | Known defects accepted into a merge rather than fixed, with why and what fixing takes (repo root) |
| docs/decisions.md | Decision log |
| docs/guides/setup.md | Install, env vars, prerequisites |

## Shared Resources

| Resource | Location | Purpose |
|---|---|---|
| Config models | @harness/config.py | TOML-backed `HarnessConfig` and settings, secrets by env var |
| Model clients | @harness/models.py | `build_chat_model` / `preflight` — role → chat client, fail-fast and bounded retry |
| Lead agent | @harness/agent.py | `build_agent` — the deepagents lead, its backend, middleware and interrupts |
| Source registry | @harness/sources.py | Per-run registry assigning `[Sn]` citation IDs to fetched pages |
| Paragraph unit | @harness/paragraphs.py | `Paragraph` / `split_paragraphs` / `strip_markers` — the one definition of a paragraph, shared by verification and rendering (D1) |
| Claim verification | @harness/verify.py | `verify_paragraphs` — one pooled model call per paragraph, judging it against all its cited sources together |
| Report assembly | @harness/report.py | `RunOutcome` + `write_report` — per-paragraph `Sources:`/`Verdict:` rendering, disclosure sections |
| Prompt loader | @harness/prompts.py | Loads/renders `harness/prompts/*.md` `$variable` templates |
| Reader wiring | @harness/agent.py | Declares the `reader` `SubAgent` spec and routes the shared `fetch_pages` instance to it — the lead's only route to page content |
| Researcher contract | @harness/prompts/subagent.md | Frozen researcher delegation contract — still unwired |
| Tool registry | @harness/tools/ | `build_tools` and the per-tool `build_<name>_tool` factories |
