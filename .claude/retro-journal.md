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

## 2026-08-20 10:49 — /handoff — /implement PLAN-tui-redesign — Phases 4-5 of 6
- gotcha: CONTRADICTS plan handoff log — `UV_PROJECT_ENVIRONMENT=C:/Users/sting/...` does not exist on this machine; plain `uv run` resolves the worktree `.venv`. Never brief subagents with it.
- gotcha: `Console.print(some_str, style=...)` loses the style to Rich's `ReprHighlighter`, platform-dependently (POSIX paints paths magenta, Windows only partially) — always `Text(value, style=...)` for dynamic text.
- gotcha: `assert "38;2;r;g;b" in raw` is too weak to prove a style applied; a shredded line still contains the escape. Assert the whole value in ONE span.
- gotcha: mypy does not check untyped test bodies, so a test calling a function with a wrong positional argument passes all four quality gates.
- gotcha: `implement-commit-guard.sh` reads `.claude/implement-state.json` at PreToolUse, before the command runs — setting `commit_allowed` and committing in one compound Bash call is always blocked.
- dead-end: Bash heredoc (`<<'PY'`) for multi-line Python/markdown fails with `unexpected EOF while looking for matching \`''\`` despite the quoted delimiter; use the Write tool then `uv run python <path>`.
- decision: a test asserting a flagged risk's outer symptom is not a test of the risk — verify concurrency regression tests BOTH ways (must fail against a deliberately broken implementation) before trusting them.

## 2026-08-25 09:59 — /handoff — PR 38 fetch lifecycle and TUI hygiene, Phases 2-3
- correction: two of my own /implement impl-plan instructions were defects caught only by the 3F review (hoisting imports out of a try that a caller catches; fencing an encoding fix to one branch on a false "our own strings" premise) — a green suite did not surface either.
- correction: reviewer findings can be real with the wrong mechanism — a cited crawl4ai `'text\html'` typo did not exist in the installed package, and a cp1252 crash was blamed on U+2026 which encodes fine at byte 0x85. Verify the mechanism before acting on the finding.
- dead-end: `console.print(Text(..., no_wrap=True))` cannot prove or refute Rich one-row truncation — `Console.print` resets `no_wrap` from its own render options; only in-Group (live-frame) rendering honors it.
- gotcha: crawl4ai's HTTP strategy writes any non-`text/html` response body to `downloads_path`, defaulting to `~/.crawl4ai/downloads` — outside the workspace, unbounded.
- gotcha: a Bash heredoc on a `&&` chain binds to the wrong command (`python -c "..." && git commit -F - <<'EOF'` silently skipped the python), and piping a quality gate to `tail` masks its exit code.
- gotcha: `Live(screen=True)` redirects `sys.stdout` via a FileProxy until `close()`, silently swallowing print-debugging and capsys assertions in mixed-renderer tests.

## 2026-09-03 18:44 — /handoff — interactive-lead-chat Phases 5-7
- correction: orphaned uncommitted worker output (dead session/rate-limit kill): adopt-and-verify — keep the diff if targeted tests pass, compensate missing red-first with full gate + judgment review; never reset blind
- dead-end: `aupdate_state({"messages":...,"todos":...})` on a fresh thread silently drops non-input channels (`as_node` -> `__start__`); seed todos with a targeted `as_node="TodoListMiddleware.after_model"` call
- dead-end: asserting a /model switch done at `thread_id` change races `_seed_todos` — wait for the switch's `CommandReply`; the race masqueraded as an API limitation
- gotcha: uv auto-updated to 0.12.9 mid-session tripping `required-version ==0.12.3`; fixed via `uv self update 0.12.3`
- gotcha: langchain middleware lists nest LATER entries INNER — converters (ToolError/ToolRetry) must sit LATER in the list than anything needing their outputs
