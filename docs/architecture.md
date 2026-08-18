# Architecture

## Overview

One command — `python -m harness "<question>"` (@harness/__main__.py) — takes a
research question and writes one timestamped, cited markdown report. A single
`deepagents` lead agent (@harness/agent.py) drives the substrate's tools over the
head role, streaming its todo plan to the terminal; Python then verifies the
draft's claims (@harness/verify.py) and assembles the report (@harness/report.py).
Model roles are config-declared, never literal: `[roles.head]` plans and
synthesizes, `[roles.researcher]` researches one assigned angle, `[roles.reader]`
digests fetched pages, and `[roles.verifier]` checks the draft's claims and writes
the consolidated reviewer paragraph. All four are preflighted at startup
(@harness/__main__.py `_PREFLIGHT_ROLES`).

## Agent Topology

Both delegation tiers are wired (three agent tiers total): the lead dispatches a
declared `researcher` subagent through deepagents' own `task` tool, and each
researcher dispatches a `reader` nested one level below it via a hand-built
`SubAgentMiddleware` (@harness/agent.py — that manual nesting bypasses
`create_deep_agent`'s auto-injected base middleware, which `_reader_spec` restores
explicitly). Live system prompts: @harness/prompts/subagent.md (researcher) and
@harness/prompts/reader.md. Tools split by tier (@harness/tools/__init__.py):
the lead keeps only `ask_user`, the researcher gets `search_web` and `fetch_raw`
(digest recovery belongs to whoever dispatches readers), and `fetch_pages` lives
only on the reader's toolset. Each prompt contract freezes the four fields a task
must carry (objective, output format, tools, boundaries) and the three a tier must
return (findings, source IDs, conflicts), so the seam holds without renegotiation.
Neither tier may ask the developer anything: clarification is the lead's alone, or
a tier-3 reader would interrupt mid-fan-out — which is why each tier gets its own
filtered tool list, since a deepagents subagent inherits the parent's tools unless
given its own. The reader always receives the facet it is supporting, never a bare
URL — a reader handed a URL without knowing why it mattered is the documented
telephone-game failure. Delegation costs 3-10x the tokens of a single agent and
compounds per level (baseline: the Phase 3 figure in
docs/plans/PLAN-research-loop.md).

Every run owns a `<workspace_dir>/<run_id>/` subdirectory — the agent's
`FilesystemBackend` is rooted there, and its notes, captured sources and evicted
history all live under it (@harness/config.py `run_workspace_dir` builds the path).
Runs are therefore isolated by construction rather than by filtering: two started at
once cannot read each other's notes, which a timestamp filter cannot prevent because
a concurrent run's files are newer than this run's start.

## Directory Structure

`harness/` holds the source: @harness/config.py (TOML config models),
@harness/models.py (role → chat client, with preflight and bounded retry),
@harness/agent.py (the deepagents lead), @harness/sources.py (per-run source
registry), @harness/paragraphs.py (the shared paragraph unit), @harness/verify.py
(per-paragraph pooled check), @harness/report.py (report
assembly), @harness/__main__.py (the CLI and its resume loop),
@harness/prompts.py (prompt loader) with its `.md` files in @harness/prompts/,
and @harness/tools/ (the tool registry and one module per tool). Tests live in
`tests/`, mirroring the source modules. `harness.toml` sits at the repo root.
Reports are timestamped markdown files under the configured reports directory.

## Principles & Invariants

Full rule set with rationale lives here as it's established; the always-load
subset is in @docs/INDEX.md → CLAUDE.md `## Invariants`.

## Key Patterns

Tools are LangChain-native async callables: built with `@tool`, declared
`response_format="content_and_artifact"`, and always driven with `ainvoke`.
Each tool is built by a `build_<name>_tool(config, ...)` factory in its own
module, and @harness/tools/__init__.py lists every one explicitly in
`build_tools` — adding a tool means a new module plus one line there. Tool
boundaries return typed failure values instead of raising exceptions. A
per-run @harness/sources.py `SourceRegistry` assigns `[Sn]` citation IDs as
pages are fetched. Config is TOML (@harness/config.py), with secrets named
by env var and never inlined.

## Dependencies

Runtime: `pydantic`, `langchain-core`, `crawl4ai` (pinned 0.9.2), `httpx`,
`deepagents` (pinned exact on the 0.7.x line) and `langchain-openai` for the
OpenAI-compatible OpenCode endpoint. Dev: `pytest`, `pytest-asyncio`, `ruff`,
`mypy`. `deepagents` drags in `langchain-anthropic`, `langchain-google-genai`
and `langsmith`, none of which this project calls — accepted knowingly, do not
try to strip them. Deliberately not depended on: `pydantic-settings`.

## Failure Modes

Observed while building the research loop (Phases 3-6 of
docs/plans/PLAN-research-loop.md). Each is a mode the system can fail in, not a
bug that was fixed and forgotten — the fix is named so it stays visible.

- **The wrong summarizer passes every test.** langchain's plain
  `SummarizationMiddleware` and `deepagents.middleware.summarization`'s share a
  `.name`, so either one replaces the default. Only the deepagents wrapper offloads
  evicted history to the backend instead of deleting it and leaves the message list
  intact for the token sum. Installing the wrong one is silent.
- **Compression can erase attribution.** If the summarizer's `keep` policy drops
  which `[Sn]` supported which finding, the lead synthesizes from unattributed
  assertions and the claim check has nothing left to check against — and the report
  looks fine. Compression immediately before synthesis is also the regime where
  false-conclusion adoption is worst.
- **The `execute` shell tool cannot be removed from the graph.** Every deepagents
  backend binds it. The no-shell invariant is enforced by hiding it from the model's
  schema via `excluded_tools` on a registered `HarnessProfile` (@harness/agent.py) —
  a process-global registry keyed `provider:model-name`. Same registration disables
  the injected `general-purpose` subagent.
- **Any mid-run termination abandons the graph.** A transient DNS failure, the wall
  clock, or the recursion limit all exit the stream by exception, so the report can
  only be assembled from what is already on disk — which is why notes and captured
  sources are disk-backed and why the lead is told to write findings as it goes. The
  run state must be captured *inside* the stream loop; assigning it after the loop
  loses both the answer and the token usage on exactly the paths that need them.
- **Interrupts surface in both streams, as a tuple.** An `ask_user` interrupt appears
  in `updates` as `{"__interrupt__": (Interrupt(...),)}` and in `values` as the state
  dict plus that key, so code that calls `.get` on every update value raises
  `AttributeError` on the first one. Detection must also be scoped to the current
  pass, or a carried-over state re-asks the same question forever.
- **A verdict must never be matched back onto text.** The answer is split into
  paragraphs exactly once, in `harness/__main__.py`, and that one list is handed to both
  verification and rendering, so verdicts align with paragraphs by index (D2). The
  earlier scheme located a claim string back inside the answer and silently dropped any
  verdict it could not place. Rendering marks a failing bullet by list-item POSITION for
  the same reason — anything that recovers the mapping from text is fragile wherever two
  lines share a suffix. See @docs/plans/PLAN-report-output.md.
- **A 404 body classifies as `fetched`.** The substrate's fetch classification is
  known-imperfect (see docs/backlog.md) and is left that way deliberately. The
  mitigation is the per-paragraph check reading captured source files: an error page
  cannot support the paragraph citing it, so the paragraph comes back unsupported.
- **The head model may 403 without a region opt-in.** `deepseek-v4-flash` required it on
  the OpenCode workspace dashboard; the endpoint is otherwise reachable and the
  failure looks like a credential problem. `deepseek-v4-pro` (current `[roles.head]`)
  worked without it in a live check — same account opt-in likely covers the whole
  DeepSeek line, but re-verify if this 403s after an account change.
- **Faked search and a scripted model conflict in tests.** `tests/test_search.py`'s
  client fake patches the process-global `httpx.AsyncClient`, and `openai`'s
  constructor rejects any `http_client` that is not an instance of whatever that name
  is bound to at the time. Any test combining both must build the model first.
