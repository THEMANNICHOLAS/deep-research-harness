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
  docs/plans/PLAN-research-loop.md are built. The hierarchy is now three tiers deep
  (docs/plans/source-hygiene-and-hierarchy/PLAN-Phase2-agent-hierarchy.md): the lead plans
  research angles and dispatches parallel `researcher` subagents, each of which searches the
  web and delegates page reading to its own nested `reader` subagent, with a bounded-retry
  `fetch_raw` fallback recovering a failed or empty digest. The report discloses which sources
  were digested, fell back raw, or went unread, read strictly from the registry's own state
  regardless of which tier fetched them.
- **Integrations:** SearXNG (local Docker instance checked in at
  @searxng/docker-compose.yml, JSON API enabled — a stock container is HTML-only
  and will not work), crawl4ai over
  crawl4ai-managed Playwright/Chromium (Lightpanda was tried and retired — see
  docs/decisions.md), OpenCode API for all four model roles — `kimi-k3` for the head,
  `deepseek-v4-pro` for the researcher, `deepseek-v4-flash` for the reader, and
  `gpt-5.6-luna` for the verifier. API **keys** live
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
| Source registry | @harness/sources.py | Per-run registry assigning `[Sn]` citation IDs to fetched pages; also the capture-file policy (`sources_dir`, `is_failed_capture`) shared by report/verify/tests |
| Run log | @harness/runlog.py | `RunLog`/`Incident` — per-run degraded-coverage incidents, echoed live to the terminal and disclosed in the report's gaps section |
| Paragraph unit | @harness/paragraphs.py | `Paragraph` / `split_paragraphs` / `strip_markers` / `renders_content` — the one definition of a paragraph and of "renders in `## Answer`", shared by verification and rendering (D1) |
| Claim verification | @harness/verify.py | `verify_paragraphs` — one pooled model call per paragraph, judging it against all its cited sources together, plus the consolidated reviewer paragraph |
| Report assembly | @harness/report.py | `RunOutcome` + `write_report` — numbered answer paragraphs, the reviewer paragraph under `## Sources`, disclosure sections |
| Prompt loader | @harness/prompts.py | Loads/renders `harness/prompts/*.md` `$variable` templates |
| Reader wiring | @harness/agent.py | `_reader_spec` — declares the `reader` `SubAgent`, nested one level under the researcher, never dispatched by the lead directly |
| Researcher wiring | @harness/agent.py | `_researcher_spec` — declares the `researcher` `SubAgent`, the lead's only `subagents` entry; nests `_reader_spec` via `SubAgentMiddleware` and owns `search_web`/`fetch_raw` |
| Researcher contract | @harness/prompts/subagent.md | The researcher's rendered system prompt (angle research + delegated reading) |
| Tool registry | @harness/tools/ | `build_tools` and the per-tool `build_<name>_tool` factories |
