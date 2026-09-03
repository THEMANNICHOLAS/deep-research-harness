# Architecture

## Overview

One command — `python -m harness` (@harness/__main__.py) — opens a welcome
screen, then a live chat session (@harness/session.py) with a `deepagents` lead
agent (@harness/agent.py) that plans research angles, dispatches them as
background researchers, and narrates each return in chat. The lead writes one
timestamped, cited markdown report only when it calls `submit_report`; Python
then verifies the draft's claims (@harness/verify.py) and assembles the report
(@harness/report.py), and chat continues afterwards over the same sources.
Model roles are config-declared, never literal:
`[roles.head]` plans and synthesizes, `[roles.researcher]` works one angle each,
`[roles.reader]` digests pages, and `[roles.verifier]` checks the draft's claims
against their cited sources.

## Agent Topology

The hierarchy is three tiers deep (Phase 2): the lead dispatches each research
angle to its own researcher through the harness-owned `dispatch_researcher` tool
(D1, PLAN-interactive-lead-chat — not deepagents' `task`, which the lead no
longer has), and each researcher — which owns `search_web` and the `fetch_raw`
recovery fallback — still delegates page reading to its own nested `reader`
subagent through deepagents' own `task` tool, so `fetch_pages` lives only on the
reader's toolset (@harness/agent.py, @harness/tools/__init__.py). The
researcher's live system prompt is @harness/prompts/subagent.md, the reader's is
@harness/prompts/reader.md. A nested subagent receives NONE of deepagents'
auto-injected middleware — `_reader_spec` re-adds the filesystem, summarization
and tool-call-patch middleware explicitly, and any future nested tier must do the
same.

Each contract freezes the four fields a task must carry
(objective, output format, tools, boundaries) and the three a tier must return
(findings, source IDs, conflicts), so the tiers can be built without renegotiating
the seam. Neither tier may ask the developer anything: clarification is the lead's
alone, or a tier-3 reader would interrupt mid-fan-out. Each tier is therefore built
with its own filtered tool list rather than inheriting the parent's —
@harness/tools/__init__.py returns a `ToolSets` split (`ask_user` to the lead,
`search_web`/`fetch_raw` to the researcher, `fetch_pages` to the reader), because a
deepagents subagent inherits the parent's tools unless given its own, which would
silently produce a reader that can search and a tier that can interrupt the
developer. The reader always receives the facet it is supporting, never a bare
URL — a reader handed a URL without knowing why it mattered is the documented
telephone-game failure. Delegation costs 3-10x the tokens of a single agent and
compounds per level, which is why runs carry a round cap and wall clock, and each
researcher's prompt carries a search/dispatch budget.

Every run owns a `<workspace_dir>/<run_id>/` subdirectory — the agent's
`FilesystemBackend` is rooted there, and its notes, captured sources and evicted
history all live under it (@harness/config.py `run_workspace_dir` builds the path).
Runs are therefore isolated by construction rather than by filtering: two started at
once cannot read each other's notes, which a timestamp filter cannot prevent because
a concurrent run's files are newer than this run's start.

## Session Loop

`harness/session.py`'s `Session` (PLAN-interactive-lead-chat D2) owns what the
one-shot CLI loop used to: an `asyncio.Queue` of events (`ResearcherReturn`,
`UserMessage`), the round cap, wall clock, synthesis margin, and the report
gate. It awaits at least one event, drains everything pending, and folds it —
researcher returns in arrival order, then any queued user text — into one
`HumanMessage` closed by a `Roster:` line, then runs one `agent.astream` turn
on the session's single `thread_id`. One lead turn per drained batch keeps the
roster no staler than the batch that produced it, at the cost of one more
head-model call per return than a single accumulate-everything turn would need.

The lead never dispatches a researcher through deepagents' own `task` tool
(D1): @harness/tools/dispatch.py's `dispatch_researcher` starts the compiled
researcher graph as a session-owned `asyncio.Task` and returns at once, so
several researchers can run in parallel and each return narrates as its own
turn — `task` stays how the *researcher* delegates to its nested reader; only
the lead's tier moved. A config cap (`[agent] max_researchers`) refuses a
dispatch past the limit rather than queuing it. Because the session started
every researcher task, the session is also what cancels them: `/new` and
Ctrl-C cancel and await the running set directly, rather than trusting
deepagents' checkpointer or graph to unwind them, since whether cancelling
`astream` propagates into a `task`-run subagent is undocumented.

The lead ends research by calling `submit_report(answer)` (D3), never by
shape-sniffing its own prose. Accepting it disarms the wall clock, runs
verification and `write_report` exactly as the one-shot loop did, and opens
post-report chat on the same thread — `dispatch_researcher`/`submit_report`
both refuse from then on, and no clock governs the remaining turns. A quit
before `submit_report` is a failed run (no report, nonzero exit); after, it is
a clean exit with the report intact.

`/model <role> <choice>` (D4) applies at the next turn boundary: it reads the
current thread's messages via `agent.aget_state`, builds a fresh agent against
the new model, mints a new `thread_id`, and reseeds the message list via
`agent.aupdate_state` — targeting the owning middleware node
(`TodoListMiddleware.after_model`) to carry the todo list across, since an
unspecified `as_node` resolves to `__start__`, whose writers cover only the
`messages` channel and silently drop everything else. `/new` (D6) cancels
every running researcher task (awaiting their `CancelledError`), disarms the
clock, drops the agent and thread, mints a fresh `run_id`, and returns to the
welcome screen; `BrowserSession.rebind_run` keeps the one warm Chromium
instance untouched (it carries no run-scoped state) and re-points only the two
per-run pieces — the `RunLog` that relaunch incidents reach, and the
browser-free HTTP crawler, rebuilt against the new run's downloads directory
with the old one closed.

Every headless invocation — every offline test, and any future
non-interactive path — runs with `Session(interactive=False)`, which keeps the
old nudge-then-fail idle backstop instead of waiting on a keyboard that isn't
there; `interactive=True` is the TTY chat path. There is deliberately no
separate one-shot CLI mode (Non-Goals, PLAN-interactive-lead-chat) — a non-TTY
invocation WITH a positional question still runs, just headless, pending a
startup guard tracked in docs/backlog.md; without a question, argparse already
refuses it (exit 2), since the welcome screen cannot be driven there.

## Directory Structure

`harness/` holds the source: @harness/config.py (TOML config models),
@harness/models.py (role → chat client, with preflight and bounded retry),
@harness/agent.py (the deepagents lead and the standalone researcher graph),
@harness/session.py (the chat session: event queue, lead turns, budgets, slash
commands, report gate), @harness/__main__.py (CLI, welcome screen, key thread
and composer, the welcome↔session loop), @harness/browser.py (the one
`BrowserSession` per process), @harness/sources.py (per-run source registry),
@harness/runlog.py (degraded-coverage incidents), @harness/activity.py (live
researcher/reader state for the TUI), @harness/display.py (Rich and plain
renderers), @harness/input.py (key decoding and the line editor),
@harness/blocklist.py (cross-session hostname blocklist), @harness/guard.py
(prompt-injection scanner), @harness/paragraphs.py (the shared paragraph
unit), @harness/verify.py (per-paragraph pooled check), @harness/report.py
(report assembly), @harness/prompts.py (prompt loader) with its `.md` files in
@harness/prompts/, and @harness/tools/ (the tool registry and one module per
tool). Tests live in
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
  paragraphs exactly once, in `harness/session.py`, and that one list is handed to both
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
