# PLAN: Fetch Lifecycle and TUI Hygiene

**Status:** In Progress
**Created:** 2026-08-21
**Type:** Single plan

## Intent

**True goal:** Make the harness robust at the two edges the developer actually hits operating
it: the browser (failures surface at run start instead of mid-run; fetches stop paying a full
Chromium launch per tool call; most pages skip Chromium entirely), and the TUI (no upstream
error text can ever flood the screen — the 2026-08-21 incident was one crawl4ai error owning
~25 lines plus 11 persistent provenance alerts filling the terminal).

**Binding outcomes:**
- **R1** — A run fails fast at startup (nonzero exit, no report) if Chromium cannot launch —
  the same bar as the existing SearXNG health check.
- **R2** — One Chromium process per session: launched once at startup, reused by every fetch
  call; the per-call browser launch/teardown cost is gone.
  - Browser dies mid-run: disclose + one relaunch attempt (best-effort + disclose), not a
    failed run. Confirmed default.
- **R3** — Anything a tool emits reaches the live TUI as at most ONE bounded line per event;
  multi-line upstream text (e.g. crawl4ai's error dumps with embedded call logs and code
  context) can never own the screen. The report file keeps full detail.
- **R4** — Live TUI alerts are ephemeral: a rolling window of the most recent alerts plus a
  running total, replacing today's grow-forever persistent list. Full incident disclosure in
  the end-of-run summary and the report's gaps section is unchanged.
  - Rolling window size N=3. Confirmed default.
- **R5** — The TUI persistently shows a running source counter — registered usable sources
  (`[Sn]` minted), per the developer's choice — updated as the run progresses.
- **R6** — Fetching is HTTP-first: a plain HTTP attempt per URL, escalating to the session's
  warm Chromium only when needed. Baseline: PR #20's implementation and plan
  (`9056d4f`, `PLAN-http-first-fetch.md` in git history), PORTED onto the current fetch
  pipeline — its blocklist and PDF phases are superseded by what already landed on
  `development` (PR #37 blocklist, current PDF batch).
  - Escalation trigger: thin/bare extracted text only (developer: "the little/bare text is
    the hint"). Confirmed default.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- New tool calls keep overwriting previous ones on screen (already the ToolCall log's
  behavior — preserve it through any renderer changes).
- The rolling alert region also shows the running total at that moment.

**Non-goals:**
- Report/disclosure content is unchanged — full error detail, full incident list.
- Provenance-guard policy is unchanged — rejections still happen, only their rendering
  gets compact.
- No redesign of incident recording (`RunLog` stays as the collection mechanism).

**Constraints & assumptions:**
- Single-user homelab deployment; the TUI is a Rich Live full-screen render over SSH.
- PR #20 was reverted ONLY because it was merged to the wrong branch (main instead of
  development) — the feature itself was not found defective. Both the merge (`e62bd4d`) and
  revert (`f8a9cad`) are in `development` history; the code survives at `9056d4f`.
- Since PR #20, `fetch.py`/`config.py` diverged by ~1,000 lines (guard, provenance,
  blocklist, PDF batch) — R6 is a port, not a revert-of-the-revert.

**Open questions:**
- none

## Background

crawl4ai 0.9.2 facts verified against the installed package during planning:
- `AsyncWebCrawler` exposes explicit `start()`/`close()` (documented "without context
  manager"); `arun_many` is reusable on a started instance and opens one page per URL inside
  the SAME browser process (`async_webcrawler.py:176-204`, `browser_manager.py:1542`).
- There is NO crash detection: a dead Chromium leaves a stale handle (`RuntimeError` only when
  `self.browser is None`), and `close()` never resets `ready` — relaunch must be ours.
- `PDFCrawlerStrategy` is plain `requests` + pypdf — launches no browser at all.
- `AsyncHTTPCrawlerStrategy` (browser-free, aiohttp) exists; its session holds a connection
  pool + DNS cache, genuinely useful kept warm across calls.
- Browser contexts are keyed by run-config signature, LRU-capped at 20;
  `max_pages_before_recycle` (default 0 = off) can recycle a long-lived browser.

## Codebase Map

- Entry points: `harness/__main__.py:main` — config load → renderer start → model preflights
  (~line 669) → SearXNG preflight (~678, catch → `renderer.close()`, stderr, `return 1`, no
  report) → registry/run_log → `build_agent` → astream loop → verification → `write_report`
  in `try/finally: renderer.close()` (~1141-1163). One `asyncio.run(main())` for the run.
- Module boundaries: tools built by `build_tools(config, registry, run_log=None)` in
  `harness/tools/__init__.py:25`; shared per-run state (registry, run_log, blocklist) threads
  as plain args into each `build_<name>_tool`. `fetch_raw` (`harness/tools/fallback.py:40`)
  calls `harness.tools.fetch._fetch` — the SOLE crawler launch site.
- Fetch pipeline (`harness/tools/fetch.py:394-670`, `_fetch`): dedup/merge → replay
  already-failed → provenance (`registry.is_approved`) → pre-crawl blocklist → PDF/HTML
  partition (`_looks_like_pdf_url`) → shared `MemoryAdaptiveDispatcher` → HTML batch via
  `async with _crawler_class()(...)` (line 507) → guard scan → `classify` (pdf outcome
  reroutes to PDF batch) → `_feed_blocklist` → mint `[Sn]` only if fetched → PDF batch via
  second per-call crawler (line 585) → assembly. Up to 2 crawler launches per call today.
- Display (`harness/display.py`): event dataclasses; `RichRenderer` frame =
  `Group(*_timeline, *_alerts, header, ...)` (~514); `Alert` branch appends unbounded
  (~610-626); `ToolCall` rows keyed by `call_id`, overwritten in place, finished rows trimmed
  to `_TOOL_LOG_TAIL = 8`; `PlainRenderer.emit` Alert branch = bare `print` (~195). Alert
  text originates from 13 `run_log.record(...)` sites; the only unbounded detail is
  `_failure_detail` (`fetch.py:262-272`) embedding raw crawl4ai `error_message`.
  `__main__._emit_new_alerts` (~697-712) is the single RunLog→renderer chokepoint.
- Reuse targets: preflight exemplars `preflight_search` (`harness/tools/search.py:299`) and
  `preflight` (`harness/models.py:64`); `strip_invisibles` (`harness/guard.py:83`);
  `Blocklist`/`fires_challenge_marker` (`harness/blocklist.py`); `classify`
  (`fetch.py:90-95`).
- PR #20 baseline (git only, `git show 9056d4f:harness/tools/fetch.py`): HTTP batch via
  `AsyncHTTPCrawlerStrategy` + `HTTPCrawlerConfig`, `_is_thin(page, min_markdown_words)`
  thin detection, `_escalate_one` browser retry, escalated result wins. Its blocklist
  (TTL-based) and absent PDF handling are SUPERSEDED — do not port.
- Tests: pytest (asyncio_mode auto), `tests/`; crawler faked by `install_crawler`
  (`tests/conftest.py:121-142`, monkeypatches `fetch._crawler_class` + `_pdf_crawler_parts`);
  fail-fast exemplar `tests/test_agent.py:1699` (SearXNG unreachable → nonzero, no report);
  display asserted via `Console(file=StringIO())` + `_strip_ansi`, parametrized
  `["plain", "rich"]` (`tests/test_display.py`).
- Commands: `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` /
  `uv run mypy .`

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- No port of PR #20's blocklist or its (absent) PDF handling — superseded by
  `harness/blocklist.py` and the current PDF batch.
- No new fetch config knobs beyond `min_markdown_words` (D6) — PR #20's six knobs are not
  re-added until a measured need exists.
- `PlainRenderer` stays append-only — no rolling window in non-TTY output (a scrolling
  stream is already ephemeral); it gets only the one-line bound + encoding fix.
- No change to the PDF batch's per-call crawler (browser-free and cheap).

## Design Decisions

### D1: Where the session browser lives
- **Chosen:** A new `BrowserSession` (new module `harness/browser.py`) holding one started
  `AsyncWebCrawler`; created and `start()`ed in `main()` immediately after the SearXNG
  preflight — a failed start IS the R1 preflight failure — threaded as one more argument
  through `build_tools` (like registry/run_log), closed in the run's `finally` beside
  `renderer.close()`. Dead browser mid-run: one relaunch attempt per run, disclosed as an
  incident; a second death fails that fetch call, not the run.
- **Rejected:** lazy module-level singleton in `fetch.py` — zero signature churn but hidden
  state, awkward relaunch tests, and startup failure would surface mid-run, undercutting R1.
  Also rejected: per-call status quo (fails R2). This SUPERSEDES
  PLAN-crawler-refinement.md D3 ("browser lifecycle stays per-call"): D3 rejected reuse on
  the no-crash-detection failure mode; the developer now accepts the relaunch machinery as
  the cost, with new evidence (Background) that `start()`/`arun_many` reuse is supported.
- **Consequences:** `build_tools` and `build_fetch_tool` grow a `browser` parameter;
  `tests/conftest.py` gains a fake session seam; Phase 2 hangs the warm HTTP strategy off
  the same object.

### D2: HTTP→Chromium escalation trigger (R6)
- **Chosen:** thin extracted text only — markdown below `min_markdown_words` after the HTTP
  attempt escalates that URL once through the warm browser; the escalated result wins.
  Transport failures (connection refused, timeout, empty body) produce no/empty markdown and
  are therefore thin — subsumed, no second trigger needed.
- **Rejected:** broader trigger (thin OR 403/429/503 OR challenge marker) — challenge pages
  are bare text and already caught by thin detection; a 403 carrying a full-sized HTML body
  is rarely browser-recoverable. Developer: "the little/bare text is the hint."
- **Consequences:** a non-2xx with a substantial body never escalates (accepted); the
  arxiv-style extensionless PDF that killed browser navigation ("Download is starting") now
  arrives via HTTP, classifies `pdf` by content-type, and reroutes cleanly to the PDF batch.

### D3: Where R3's one-line bound lives
- **Chosen:** display-side, at the Alert chokepoints — one helper applied in both renderers'
  Alert branches (first line, char cap, `strip_invisibles`), plus `errors="replace"` at the
  plain-print encode boundary (fixes LATER-PROBLEMS.md "non-ASCII incident detail aborts on
  Windows"; UTF-8 terminals unaffected — replace, never strip).
- **Rejected:** truncating at `run_log.record(...)` call sites — 13 sites, and it would thin
  the report's `## Gaps and disclosures`, violating the non-goal that reports keep full
  detail.
- **Consequences:** RunLog details stay verbatim; any future emitter is bounded for free.

### D4: Alert region shape (R4)
- **Chosen:** `_alerts` becomes a rolling window (`deque(maxlen=3)`) rendered with a running
  total line (e.g. `warnings: 12 this run — full list in report`). `RunFinished` summary and
  report disclosure untouched.
- **Rejected:** latest-only single slot (bursts invisible); count-only (no at-a-glance text).
- **Consequences:** the "persistent line" docstring contract on `Alert` is rewritten; the
  existing persistence test is updated, not deleted — it becomes a window/total test.

### D5: R5 counter semantics and wiring
- **Chosen:** count = registered usable sources (`[Sn]` minted; developer's pick). Wiring: a
  new frozen display event carrying the count, emitted from `__main__`'s existing per-chunk
  poll (same site as `_emit_new_alerts`) whenever the value changed; rendered as a persistent
  line in the live frame.
- **Rejected:** fetch-attempted count or usable/attempted pair — developer chose "registered
  sources that are actually used."
- **Consequences:** `SourceRegistry` exposes a cheap count; non-TTY renderer prints the
  count only when it changes.

### D6: Minimal new config for HTTP-first
- **Chosen:** one new key, `fetch.min_markdown_words` (escalation threshold). The HTTP pass
  reuses existing `page_timeout_ms` and `max_concurrency`.
- **Rejected:** re-adding PR #20's six knobs (`http_concurrency`, `http_deadline_ms`,
  `max_retries`, `browser_deadline_ms`, ...) — YAGNI until a live run shows the shared
  values are wrong; adding a knob later is cheap (backlog note, not plan scope).
- **Consequences:** if HTTP and browser passes ever need different timeouts, that is a
  config addition, not a redesign.

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Chromium preflight fail-fast | Phase 1 (startup failure → nonzero exit, no report) |
| R2 | One Chromium per session | Phase 1 (session browser + one-relaunch) |
| R3 | One bounded line per TUI event | Phase 3 (bounding helper at Alert chokepoints) |
| R4 | Rolling ephemeral alerts + total | Phase 3 (window of 3 + running total) |
| R5 | Live usable-source counter | Phase 4 (new count event + persistent line) |
| R6 | HTTP-first with warm-browser escalation | Phase 2 (ported PR #20 skeleton) |

## Progress
- [x] Phase 1: Session browser + Chromium preflight
- [ ] Phase 2: HTTP-first fetch with thin-text escalation
- [ ] Phase 3: TUI alert hygiene
- [ ] Phase 4: Live source counter
- [ ] Final verification

## Phases

### Phase 1: Session browser + Chromium preflight
**Risk:** flagged (!#1) (!#2)
**Test-first:** required
**Goal:** One Chromium launched at startup (failure exits like the SearXNG preflight), held
for the whole run, reused by every fetch call, relaunched at most once if it dies.
**Requirements:** R1, R2
**Files:**
- `harness/browser.py` — new: `BrowserSession` (start/close/arun_many-with-relaunch). New
  module so `__main__` can own the lifecycle without importing fetch-tool internals.
- `harness/__main__.py` — start session after SearXNG preflight (same catch→stderr→exit-1
  shape); close in the existing `finally` beside `renderer.close()`.
- `harness/tools/__init__.py` — `build_tools`/factory signatures gain the session argument.
- `harness/agent.py` — added 2026-08-24, see `## Reconciliations`: `build_agent` forwards the
  session to `build_tools`, whose only call site this is.
- `harness/tools/fetch.py` — HTML batch uses the session instead of
  `async with _crawler_class()(...)`; PDF batch untouched.
- `tests/conftest.py` — extend `install_crawler` seam with a fake session (constructible
  dead/dying for relaunch tests).
- `tests/test_browser.py` — new: unit home for `BrowserSession` (mirrors per-module test
  layout).
**Diff budget:** ~250-380 lines across ~6 files (tests included)

**Reuse:**
- Pattern to mirror: `preflight_search` + its `__main__.py` catch block (`search.py:299`,
  `__main__.py:678-686`) — typed error, stderr line, exit 1, no report.
- Extend `install_crawler` in `tests/conftest.py` — do NOT invent a parallel fixture.
- Fail-fast test exemplar: `tests/test_agent.py:1699`.

**Contracts:**
- `BrowserSession.start() -> None` — raises a typed error on launch failure (R1's signal).
- `BrowserSession.close() -> None` — idempotent; called from `main()`'s `finally`.
- `BrowserSession.arun_many(urls, config, dispatcher) -> list` — one relaunch-and-retry per
  run on batch-level failure, recording a `RunLog` incident via a caller-supplied hook or
  return signal; second death raises to the caller (that fetch call fails, run continues).
- `build_tools(config, registry, run_log=None, browser=None)` — `None` browser preserves
  current per-call behavior for tests that don't care.

**Out of scope:**
- No HTTP strategy yet (Phase 2). ~~No change to `fallback.py` (it rides `_fetch`).~~ (struck
  2026-08-24 — see `## Reconciliations`.) No PDF
  batch changes. No display changes. No retry machinery beyond the single relaunch.

**Tests (write first, confirm red):**
- [ ] Startup: session start failure → `main()` exits nonzero, writes no report (mirror the
  SearXNG test).
- [ ] Reuse: two `fetch_pages` calls construct the underlying crawler exactly once.
- [ ] Relaunch: batch failure on a dead session → one relaunch + retry succeeds, incident
  recorded; a second death fails the call (per-URL failure blocks), not the run.
- [ ] Teardown: `close()` called on normal exit and on error paths.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement `BrowserSession`; wire preflight + teardown in `__main__`; thread through
   `build_tools` into `_fetch`'s HTML batch.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] Full suite green; `uv run mypy .` clean with the new module.

### Phase 2: HTTP-first fetch with thin-text escalation
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** Non-PDF URLs are fetched by a warm browser-free HTTP pass first; only URLs whose
extracted text is thin escalate (once) through the session browser; pipeline ordering
(provenance → blocklist → guard → classify → mint) is preserved.
**Requirements:** R6
**Assumes:**
- Phase 1's `BrowserSession` exists and is threaded into `_fetch`.
**Files:**
- `harness/browser.py` — session additionally holds the warm `AsyncHTTPCrawlerStrategy`
  crawler (started/closed with the browser).
- `harness/tools/fetch.py` — HTTP batch + `_is_thin` + escalation subset, ported from
  `git show 9056d4f:harness/tools/fetch.py` into the CURRENT ordering; `pdf` outcomes from
  the HTTP pass reroute to the existing PDF batch.
- `harness/config.py`, `harness.toml` — `fetch.min_markdown_words`.
- `tests/test_fetch.py` — new cases ride the existing fixture seam.
**Diff budget:** ~300-500 lines across ~4 files (tests included)

**Reuse:**
- Port skeleton: `_fetch_with_retries`/`_is_thin`/`_escalate_one` shapes from `9056d4f`
  (adapt, don't transplant — the surrounding ordering changed; retries themselves are out,
  per D6/no-new-knobs).
- Existing: `classify`, guard `scan`, `_feed_blocklist`, PDF reroute mechanism, dispatcher.

**Contracts:**
- Escalation rule frozen per D2: escalate iff extracted markdown word count <
  `min_markdown_words`; escalated result wins unconditionally; at most one escalation per
  URL per call.

**Out of scope:**
- PR #20's blocklist/TTL, `skipped` outcome, and extra config knobs. No changes to
  provenance/guard/blocklist POLICY. No PDF batch changes beyond receiving reroutes. No
  per-URL retry loops.

**Tests (write first, confirm red):**
- [ ] A rich HTML page fetched via HTTP never touches the browser.
- [ ] A thin page escalates once; the browser result wins; a thin browser result still
  classifies by the normal rules.
- [ ] Transport failure (no body) escalates via the same thin rule.
- [ ] HTTP-discovered PDF content-type reroutes to the PDF batch (arxiv-style URL).
- [ ] Ordering preserved: provenance-rejected and blocklisted URLs are never HTTP-fetched.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Port and adapt the HTTP pass + escalation into `_fetch`; add the config key.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Existing `test_fetch.py` suite still green (ordering/policy unchanged).
- [ ] `harness.toml` documents the new key next to its `fetch` siblings.

### Phase 3: TUI alert hygiene
**Risk:** none
**Test-first:** required
**Goal:** Every alert reaches either renderer as one bounded line; the live alert region is
a rolling last-3 window with a running total; a non-encodable character can no longer abort
a Windows run.
**Requirements:** R3, R4
**Files:**
- `harness/display.py` — bounding helper applied in both renderers' Alert branches;
  `_alerts` → rolling window + total line; plain-print encode boundary gets
  `errors="replace"`.
- `tests/test_display.py` — extend the existing alert/summary tests.
**Diff budget:** ~120-220 lines across 2 files (tests included)

**Reuse:**
- `strip_invisibles` (`harness/guard.py:83`) inside the helper — do NOT write a second
  invisible-char stripper.
- Test shape: parametrized `["plain", "rich"]` alert tests (`tests/test_display.py:1505`).

**Out of scope:**
- `run_log.record` call sites and `RunLog` itself (report stays verbatim). `RunFinished`
  summary content. ToolCall log behavior (`_TOOL_LOG_TAIL`, in-place overwrite) — preserved,
  not modified. Timeline rendering.

**Tests (write first, confirm red):**
- [ ] A multi-line alert (real crawl4ai-shaped dump) renders as exactly one capped line in
  both renderers.
- [ ] A 4th alert evicts the 1st; the running total still counts all 4.
- [ ] The full incident list still reaches the `RunFinished` summary count and (existing
  test) the report gaps section — unchanged.
- [ ] An alert containing a non-cp1252 character renders (replaced) instead of raising on a
  cp1252-encoding stream.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the helper, window, and encode fix; update the `Alert` docstring contract.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] LATER-PROBLEMS.md entry for the Windows abort updated to resolved-by-this-change.

### Phase 4: Live source counter
**Risk:** none
**Test-first:** required
**Goal:** The TUI persistently shows the count of registered usable sources, updating as the
run progresses.
**Requirements:** R5
**Files:**
- `harness/display.py` — new frozen count event + persistent line in the live frame; plain
  renderer prints on change.
- `harness/__main__.py` — emit from the existing per-chunk poll beside `_emit_new_alerts`
  when the count changed.
- `harness/sources.py` — expose the cheap usable-source count.
- `tests/test_display.py`, `tests/test_agent.py` — coverage for event + emission.
**Diff budget:** ~80-150 lines across ~5 files (tests included)

**Reuse:**
- Event + emission pattern: `TodosUpdated`/`RoundsUpdated` (replacement-not-delta events)
  and the `_emit_new_alerts` poll site.

**Contracts:**
- One frozen dataclass event carrying the usable-source count (name/field pinned at
  implementation; consumed only by display).

**Out of scope:**
- Attempted/unusable counts. Search-result counts. Report changes.

**Tests (write first, confirm red):**
- [ ] Count renders and updates in the rich frame; plain renderer prints only on change.
- [ ] Emission fires when the registry grows, not on every chunk.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement event, emission, and rendering.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] A scripted full-run test shows the counter present alongside alerts/todos (no layout
  regression in existing frame tests).

## Verification
- [ ] `uv run pytest` — full suite green.
- [ ] `uv run ruff check .` && `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] Manual (homelab, post-merge): one live run — startup shows the Chromium preflight
  passing; a research question completes with the alert window bounded and the source
  counter climbing; kill the Chromium process mid-run once and observe disclose+relaunch.

## Notes
- Phase order is risk-first: the session browser is the seam HTTP-first builds on.
  Phases 3 and 4 are independent of 1-2 and of each other.
- The 2026-08-21 arxiv failure mode ("Page.goto: Download is starting") is expected to
  disappear as a side effect of D2 (HTTP pass reroutes extensionless PDFs by content-type).
- Backlog candidates discovered, NOT in scope: separate HTTP-pass timeout/concurrency knobs
  (D6); `max_pages_before_recycle` as a hedge if a long session accumulates browser state.

## Risks
#1. **Dead-browser detection is heuristic.** crawl4ai has no crash detection (Background);
    `BrowserSession` infers death from batch-level failure. A hung-but-alive browser
    surfaces as per-URL timeouts, not a relaunch — acceptable (timeouts are already
    disclosed), but the relaunch path itself must be exercised in the Phase 1 tests and the
    manual kill-Chromium check in `## Verification`.
#2. **Session-long browser accumulates state.** Contexts are LRU-capped at 20 per browser,
    but memory growth over a long multi-round run is unmeasured. Mitigation exists upstream
    (`max_pages_before_recycle`) and is deliberately NOT wired now — watch the first live
    runs; wire the knob only on evidence (backlog note in `## Notes`).
#3. **HTTP-pass fingerprint may fetch worse than Chromium.** aiohttp's UA/TLS profile may
    yield more thin/blocked responses than today's browser-first path, raising escalation
    volume and total latency on hostile sites. The escalation fallback bounds the damage
    (worst case ≈ today's behavior + one cheap HTTP miss); `min_markdown_words` is the
    tuning point. Confirm on the first live runs before tuning anything.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

2026-08-24 — Phase 1: "No change to `fallback.py`" contradicts R2. `fallback.py:40` calls
`_fetch(urls, config, registry, run_log, blocklist)` positionally, so once `_fetch` takes the
session browser from its CALLER (D1 rejected a module-level singleton, which is the only design
that would have kept fallback.py untouched), `fetch_raw` cannot reach the session without being
threaded. Leaving it unchanged would silently keep `fetch_raw` on a per-call browser launch,
failing R2's "reused by every fetch call" for that path. → AMENDMENT: `fallback.py` IS in scope
for Phase 1, limited to threading only — `build_fallback_tool` gains the same `browser`
parameter as `build_fetch_tool` and forwards it to `_fetch`. No other change to `fallback.py`;
its retry/marker logic is untouched.

2026-08-24 — Phase 1: the phase's `**Files:**` list omits `harness/agent.py`, and the Codebase
Map implied `build_tools` was reachable from `__main__.py`. It is not: `build_tools` has exactly
ONE call site, `harness/agent.py:636` inside `build_agent`. Threading the session only as far as
`__main__` would leave it unreachable by `fetch_pages`/`fetch_raw`, silently failing R2 in every
real run while the unit tests (which call `build_tools` directly) still passed. → AMENDMENT:
`harness/agent.py` is in Phase 1 scope, limited to a pass-through — `build_agent` gains a
`browser` parameter forwarded verbatim to `build_tools`. `build_agent` owns NO lifecycle over it;
`main()` keeps start/close. Diff budget grows from ~6 to ~7 files.

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

2026-08-24 — Phase 1: the Codebase Map says teardown goes in "the existing `finally` beside
`renderer.close()`", but that `finally` (`__main__.py:1146-1163`) wraps report-writing only —
`renderer.close()` is called INLINE before each of the three early `return 1` paths. Of those,
only the agent-build `ModelError` path (`__main__.py:805-810`) can be reached after the session
would exist, so closing solely in the `finally` leaks the browser on exactly that one path.
→ Suggested action: close the session at BOTH sites, mirroring how `renderer.close()` itself is
already handled, rather than restructuring `main()` into one outer try/finally (bigger diff,
out of Phase 1's scope).

2026-08-24 — Phase 1: the plan says start the session "immediately after the SearXNG preflight"
(`__main__.py:686`), but `run_log = RunLog()` is not constructed until line 700, and
`BrowserSession` needs it to record its own relaunch incident (the Contracts line leaves this as
"a caller-supplied hook or return signal"). → Suggested action: construct and start the session
immediately AFTER `run_log = RunLog()` and pass `run_log` in, so the session records its own
incident with no extra hook. Only `started_at`/`registry`/`approve` sit in between — none can
fail meaningfully — so this is still a startup preflight and R1's fail-fast is unaffected.

2026-08-24 — Phase 1: BOTH discoveries above dispositioned **act now** by the developer — folded
into Phase 1's implementation rather than deferred.

2026-08-24 — Phase 1: a SECOND browser death costs a wasted reader subagent run. The raise from
`BrowserSession.arun_many` propagates out of `_fetch`/`fetch_pages`, and is not in
`_PASS_THROUGH_TASK_FAILURES` (`harness/agent.py:129`), so `_retry_on_non_search_abort` re-runs
the whole reader before `_task_failure_handler` renders "READER FAILED". NOTE the rejected fix:
adding `BrowserPreflightError` to that tuple would make `_handle` return `None`, propagating to
`__main__`'s abort handling — i.e. ABORTING THE RUN, contradicting the Phase 1 contract ("second
death... run continues") and the best-effort+disclose invariant. Any real fix needs a THIRD
category (skip the retry, still soft-fail the reader). → DEFERRED by the developer: the cost is
one wasted re-run after an already-rare second death, the run still completes with disclosed
degraded coverage, and a third failure taxonomy in `agent.py` is not worth an unmeasured cost.

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. MUST remain the LAST section of this file:
/implement's Step 2 reads the plan up to this heading plus only the log's final entry, so
never add a section below it. -->

### 2026-08-24 — Phase 1: Session browser + Chromium preflight
- Done: `harness/browser.py` (`BrowserSession` + `BrowserPreflightError`), startup preflight in
  `__main__` with the SearXNG catch/stderr/exit-1 shape, teardown at both exit sites, and the
  session threaded `build_agent` -> `build_tools` -> fetch/fallback into `_fetch`'s HTML batch.
  801 tests pass; ruff/format/mypy clean. Two review rounds; 5 findings fixed, 1 deferred.
- Learned: (1) `build_tools`'s ONLY call site is `harness/agent.py:636`, not `__main__` — the
  session must be threaded through `build_agent`, or R2 is silently false in production while
  every unit test passes. (2) `patch_run` must neutralize `BrowserSession.start`, or ~30
  `main()` tests launch a REAL Chromium (this cut the suite from 107s to 64s). (3) The relaunch
  needs a lock AND a generation counter: siblings already in flight ride it, and an ARRIVAL
  during the None window must wait on the lock rather than be told a relaunch failed.
  (4) `_PASS_THROUGH_TASK_FAILURES` means ABORT THE RUN, not "fail this subagent fast" — do not
  put browser errors there.
- Drift: two entries in `## Reconciliations` (fallback.py scoped in for threading; agent.py
  added to the file list), plus three `## Discoveries` (two acted, one deferred). All approved.
- Watch-next: Phase 2 hangs the warm HTTP strategy off this same `BrowserSession` (D1's stated
  consequence) — `self._config` is stored but currently unread, and exists for exactly that.
  Phase 2 must also keep `patch_run`'s browser neutralization working once the HTTP strategy
  starts/closes alongside the browser.
