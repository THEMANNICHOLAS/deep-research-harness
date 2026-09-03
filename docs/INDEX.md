# Documentation Index

## Project Context

- **Deployment:** developer's local laptop, single user; the homelab Linux machine over SSH
  is a future step. No cloud/container deployment.
- **Status:** `python -m harness` opens a welcome screen and then a live chat session with
  the lead (docs/plans/PLAN-interactive-lead-chat.md); chat is the only supported mode. A
  positional question skips the welcome screen, and on a piped stdin it still runs headless
  to the report (`Session(interactive=False)`, the test seam) — a startup guard is a backlog
  item, not built. The lead dispatches researchers through the harness-owned `dispatch_researcher` tool
  (`asyncio.Task`s the session owns, capped by `[agent] max_researchers`), each researcher
  in turn delegates page reading to its own nested `reader` subagent via deepagents' `task`,
  with a bounded-retry `fetch_raw` fallback recovering a failed or empty digest. Every
  researcher return drains into one lead turn together with any queued user message, closed
  by a roster line; the lead may ask a clarifying question with up to four choices at any
  point. The report is written only when the lead calls `submit_report` (or a cap fires);
  chat continues unclocked afterwards over the same sources, with no new research.
  `/sources` (list captured sources), `/model` (switch a role at the next turn boundary,
  reseeding the new model with the full existing context) and `/new` (cancel running
  researchers, disarm the clock, and return to a fresh welcome screen on the same warm
  browser) all work mid-run. Runs are fail-fast: SearXNG is health-checked before any agent
  work, three consecutive mid-run search connection failures abort the run, and quitting
  before a report exists writes no report and exits nonzero. The report discloses which
  sources were digested, fell back raw, or went unread, read strictly from the registry's
  own state regardless of which tier fetched them.
- **Integrations:** SearXNG (local Docker instance checked in at
  @searxng/docker-compose.yml, JSON API enabled — a stock container is HTML-only
  and will not work), crawl4ai over
  crawl4ai-managed Playwright/Chromium (Lightpanda was tried and retired — see
  docs/decisions.md), OpenCode API for all four model roles — `kimi-k3` for the head,
  `deepseek-v4-pro` for the researcher, `deepseek-v4-flash` for the reader, and
  `qwen3.7-plus` for the verifier. API **keys** live
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
| Config models | @harness/config.py | TOML-backed `HarnessConfig` and settings, secrets by env var; also the per-run path helpers `run_workspace_dir` and `run_downloads_dir` (crawl4ai downloads land under `<workspace_dir>/<run_id>/downloads` for the CURRENT run — across `/new`, `BrowserSession.rebind_run` re-points the browser at the new run's id/log, never `$HOME`) |
| Model clients | @harness/models.py | `build_chat_model` / `preflight` — role → chat client, fail-fast and bounded retry |
| Session browser | @harness/browser.py | `BrowserSession` — the run's ONE Chromium plus its warm browser-free HTTP crawler, started at preflight (`BrowserPreflightError` → nonzero exit, no report), reused by every fetch, relaunched at most once, rebound to a fresh run/id via `rebind_run` on `/new` |
| Session loop | @harness/session.py | `Session` — event queue (`ResearcherReturn`/`UserMessage`), one lead turn per drained batch, budgets (wall clock, round cap, synthesis margin), researcher roster, slash commands (`/sources`/`/model`/`/new`), post-report chat, run exit gating |
| Dispatch tools | @harness/tools/dispatch.py | `build_dispatch_researcher_tool` / `build_submit_report_tool` — the lead's harness-owned `dispatch_researcher`/`submit_report` tools, one module per tool per the registry convention |
| Lead agent | @harness/agent.py | `build_agent` — the deepagents lead, its backend, middleware and interrupts |
| Source registry | @harness/sources.py | Per-run registry assigning `[Sn]` citation IDs to fetched pages; also the capture-file policy (`sources_dir`, `is_failed_capture`) shared by report/verify/tests |
| Run log | @harness/runlog.py | `RunLog`/`Incident` — per-run degraded-coverage incidents, echoed live to the terminal and disclosed in the report's gaps section |
| Paragraph unit | @harness/paragraphs.py | `Paragraph` / `split_paragraphs` / `strip_markers` — the one definition of a paragraph, shared by verification and rendering (D1) |
| Claim verification | @harness/verify.py | `verify_paragraphs` — one pooled model call per paragraph, plus one consolidation call on the verifier role producing the reviewer paragraph |
| Report assembly | @harness/report.py | `RunOutcome` + `write_report` — plain-prose `## Answer`, consolidated reviewer paragraph under `## Sources`, disclosure sections |
| Prompt loader | @harness/prompts.py | Loads/renders `harness/prompts/*.md` `$variable` templates |
| Reader wiring | @harness/agent.py | `_reader_spec` — declares the `reader` `SubAgent`, nested one level under the researcher, never dispatched by the lead directly |
| Researcher wiring | @harness/agent.py | `build_researcher_graph` — compiles the researcher tier as a standalone graph once per session (the lead runs with `subagents=[]` and dispatches through `dispatch_researcher`, D1); its stack `_researcher_middleware` nests `_reader_spec` via `SubAgentMiddleware`, and the tier owns `search_web`/`fetch_raw` |
| Researcher contract | @harness/prompts/subagent.md | The researcher's rendered system prompt (angle research + delegated reading) |
| Tool registry | @harness/tools/ | `build_tools` and the per-tool `build_<name>_tool` factories |
| Domain blocklist | @harness/blocklist.py | `load_blocklist`/`Blocklist` — cross-session hostname blocklist fed by anti-bot refusals; also `hostname_of`, the one URL→hostname definition |
