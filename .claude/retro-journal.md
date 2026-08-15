# Retro Journal

<!-- Raw session evidence, append-only. Written by /handoff and /iterate closeout;
     mined by /retro. Working agents do NOT read this file during tasks. -->

## Dismissed patterns
<!-- Maintained by /retro. Never re-propose these. -->
- (none yet)

---

## 2026-08-08 20:58 — /handoff — /implement PLAN-harness-substrate — Phase 1 of 5
- correction: recommended a targeted `numpy.*` `follow_imports = skip` mypy override to keep `python_version = "3.11"`; developer chose bumping mypy to `3.12` and accepting that `requires-python = ">=3.11"` goes unverified.
- dead-end: crawl4ai over Lightpanda CDP — attach succeeds, `Page.goto` times out at 60000ms on `domcontentloaded`; Playwright control returned 200. Do not retry.
- dead-end: PowerShell here-string `@'...'@` passed to the Bash tool is literal — a bare `@` became the commit subject; use `<<'EOF'` heredocs for multi-line git messages.
- gotcha: CONTRADICTS CLAUDE.md `## Stack` and docs/INDEX.md — both still name Lightpanda as the fetch backend; it is crawl4ai-managed Playwright now.
- gotcha: `~/.claude/context-status.json` is shared across concurrent sessions — it served another project's numbers mid-session; check `cwd`/`session_id` before trusting it.
- gotcha: `uv run` does not auto-load `.env` (needs `--env-file`/`UV_ENV_FILE`), and `uv` resolved the venv to Python 3.14 under `requires-python = ">=3.11"`.
- decision: pin fast-moving deps rather than range them (`crawl4ai==0.9.2`), and leave unknown deployment facts as literal `TODO` in checked-in config rather than fabricating endpoints.

## 2026-08-09 12:10 — /handoff — /implement PLAN-harness-substrate — Phases 2-4 of 5
- gotcha: `~/.claude/context-status.json` served another session's numbers twice in one session (once `cwd` = `ClaudeWorkflows-iterative`, once a different `session_id` for this same project) — check BOTH `cwd` and `session_id` before trusting it.
- gotcha: `.claude/planning-mode` flipped to `active` mid-session unprompted, which `planning-write-guard.sh` uses to deny source writes; `/implement` Step 2 requires it `inactive`.
- gotcha: two phases running, the implementation worker's first test pass had a hole that only reading the assertions caught (fixtures setting `fit_markdown` and `raw_markdown` to the same string; no test hitting the real request URL/`format=json`). Read subagent-written tests before authorizing implementation.
- gotcha: iterating a langchain `content_and_artifact` artifact blindly breaks on the failure path — the artifact is a single pydantic model there, and iterating one yields `(key, value)` tuples.
- dead-end: stock `searxng/searxng` container ships `formats: [html]` and cannot serve the JSON API without editing `settings.yml`.
- decision: an acceptance criterion that could not be verified was left UNCHECKED and marked PENDING in the plan rather than signed off — keeps the gap visible to the next session.

## 2026-08-12 22:44 — /handoff — Ephemeral per-run workspace — planning deferred mid-session
- correction: described what the lead agent reads from the code around it without checking `harness/prompts/orchestrator.md` — the prompt never mentions `sources/` or `S<n>.md`. Read the prompt before claiming agent behavior.
- correction: an `AskUserQuestion` set was bounced back for clarification because it rested on a premise the developer disputed — resolve the premise before re-presenting options.
- dead-end: Docker container per run — the reaper logic is identical either way (a stopped container does not self-remove on a TTL), `reports/` must live outside the sandbox so a bind mount defeats the isolation, and Playwright/Chromium must be containerized or crossed.
- dead-end: `langchain-sandbox` / PyodideSandbox — sandboxes code execution in WASM, README states file access is not supported, repo archived and deprecated 2026-01-14 at 0.0.6.
- gotcha: `deepagents` and `langgraph` are NOT installed in this repo's `.venv` (only `langchain_core` 1.5.3), so library API facts can only be confirmed against GitHub `main`, never the pinned version.
- decision: restart-after-crash means true mid-run resume via `langgraph-checkpoint-sqlite`'s `SqliteSaver`, not directory reuse — chosen over the cheaper option that reuses captured pages with a fresh agent.
