# Architecture

## Overview

One command — `python -m harness "<question>"` (@harness/__main__.py) — takes a
research question and writes one timestamped, cited markdown report. A single
`deepagents` lead agent (@harness/agent.py) drives the substrate's tools over the
head role, streaming its todo plan to the terminal; Python then verifies the
draft's claims (@harness/verify.py) and assembles the report (@harness/report.py).
Model roles are config-declared, never literal: `[roles.head]` plans, synthesizes
and checks claims, `[roles.subagent]` is the cheap worker held for the later
delegation tiers.

## Agent Topology

Today there is exactly one agent. The researcher and reader tiers exist only as
frozen prompt contracts — @harness/prompts/subagent.md (researcher) and
@harness/prompts/reader.md (reader) — and nothing delegates to them; wiring them
is the next round's work. Each contract freezes the four fields a task must carry
(objective, output format, tools, boundaries) and the three a tier must return
(findings, source IDs, conflicts), so the tiers can be built without renegotiating
the seam. Neither tier may ask the developer anything: clarification is the lead's
alone, or a tier-3 reader would interrupt mid-fan-out. Both tiers must therefore be
built with a filtered tool list rather than the lead's — @harness/tools/__init__.py
always returns `search_web` and `ask_user`, and a deepagents subagent inherits the
parent's tools unless given its own, which would silently produce a reader that can
search and a tier that can interrupt the developer. The reader always receives
the facet it is supporting, never a bare URL — a reader handed a URL without
knowing why it mattered is the documented telephone-game failure. Delegation costs
3-10x the tokens of a single agent and compounds per level, which is why the tiers
wait for a measured baseline (the Phase 3 figure in docs/plans/PLAN-research-loop.md).

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
- **The head model 403s without a region opt-in.** `deepseek-v4-flash` requires it on
  the OpenCode workspace dashboard; the endpoint is otherwise reachable and the
  failure looks like a credential problem.
- **Faked search and a scripted model conflict in tests.** `tests/test_search.py`'s
  client fake patches the process-global `httpx.AsyncClient`, and `openai`'s
  constructor rejects any `http_client` that is not an instance of whatever that name
  is bound to at the time. Any test combining both must build the model first.
