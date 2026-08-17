# PLAN: HTTP-First Fetch Pipeline

**Status:** Complete (pending the live verification check)
**Created:** 2026-08-17
**Type:** Single plan

## Intent

**True goal:** The fetch pipeline stops hanging research agents and stops burning RAM —
plain HTTP scraping (still via crawl4ai) becomes the primary extraction strategy; Chromium
is only an escalation for JS-rendered pages.

**Binding outcomes:**
- **R1** — Every URL fetch uses HTTP-first extraction; a browser is launched only when the
  HTTP result is a JS shell / near-empty (auto-detected), retried once via Chromium.
  - A URL serving `application/pdf` is detected before its body is fetched, reported as the
    existing `non_html` outcome, and never escalated to Chromium (which cannot extract PDF
    text). No PDF file is written to disk.
- **R2** — A fetch attempt never exceeds a hard per-URL, per-strategy deadline, config-driven:
  ~3s for an HTTP attempt, ~20s ceiling for a Chromium escalation (renders can't finish in 3s).
  On expiry it counts as a failure — agents can never hang on a URL.
- **R3** — A 403 or 401 from a domain marks the whole domain skipped; the skip persists
  across runs with a 30-day TTL (storage format decided in design).
- **R4** — A failed scrape is retried at most 2 times (2 extra attempts after the first).
  - Default: retry timeouts/network errors/5xx; other 4xx are not retried; Chromium
    escalation is not a retry.
- **R5** — Skipped domains, timeouts, and exhausted retries are disclosed in results, never
  silently thinned (per the existing best-effort + disclose invariant).
- **R6** — The number of concurrently callable subagents is bounded by a configured maximum,
  so total fetch load stays bounded as the agent loop lands.
  - No agent loop exists yet: this outcome is satisfied by a validated config key that the
    future loop must honor, not by runtime enforcement in this work.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- RAM saving and speed-up are stated goals but qualitative — no measured threshold.
- Cross-agent fetch dedup via crawl4ai's SQLite cache (`CacheMode`) is deliberately not
  pursued; `BYPASS` stays, since research URLs are rarely re-fetched within a run.

**Non-goals:**
- Replacing crawl4ai as the extraction library.
- Changing search (SearXNG) or the tool registry shape.
- Tuning extraction quality / markdown output.
- Extracting text from PDFs (backlog: needs the `crawl4ai[pdf]` extra + `pypdf`).
- Building the agent loop, or any cross-process coordination machinery for it.

**Constraints & assumptions:**
- crawl4ai stays pinned at 0.9.2.
- No new database — blocklist persistence must be file-based.
- Config (deadlines, retry count, TTL, concurrency, subagent cap) lives in `harness.toml`,
  never hardcoded.
- Homelab Linux box, single-user research runs; blocklist is regenerable (no data-loss concern).
- Fetched pages are untrusted input (already true today).
- File writes stay confined to the workspace: crawl4ai's `downloads_path` must be pinned
  inside it rather than defaulting to `~/.crawl4ai/downloads`.
- No shared Chromium `user_data_dir` may be configured — crawl4ai SIGTERMs the PID holding
  a shared profile, so one agent would kill a sibling's browser.

**Open questions:**
- None. (Resolved during design: a 3s deadline is viable only via caller-side
  `asyncio.wait_for` — crawl4ai's HTTP strategy hardcodes a 10s connect timeout; the
  blocklist is a JSON `{domain: timestamp}` file; the JS-shell detector measures generated
  markdown word count.)

## Background

crawl4ai 0.9.2 facts verified against the installed package during planning, which the
design depends on and no single decision below owns:

- `AsyncHTTPCrawlerStrategy` (`crawl4ai/async_crawler_strategy.py:2466`) uses aiohttp and
  never imports Playwright; `AsyncWebCrawler.__aenter__` only delegates to the strategy, so
  an HTTP-only crawler never launches Chromium.
- Downstream processing (`aprocess_html` — markdown generation, content filtering) runs off
  `html` identically for both strategies, so extraction output is unchanged.
- `LXMLWebScrapingStrategy` is already the 0.9.2 default and `PruningContentFilter` is a
  pure DOM heuristic — the codebase is already on crawl4ai's fast paths, so speed gains must
  come from our own layer (strategy, deadline, concurrency, skipping).
- `MemoryAdaptiveDispatcher` applies only to `arun_many`; its `memory_threshold_percent` is
  measured system-wide via `psutil.virtual_memory()`, not per-process.

## Codebase Map

- Entry point: `harness/tools/fetch.py` — `build_fetch_tool(config, registry)` builds the
  `fetch_pages` LangChain tool; `_fetch()` (lines 161-195) is the single crawl4ai call site.
- Module boundaries: flat modules at `harness/` root (`config.py`, `sources.py`,
  `prompts.py`); tools live in `harness/tools/` with one `build_<name>_tool` factory each,
  assembled by `build_tools(config, registry)` in `harness/tools/__init__.py:11`.
- Reuse targets:
  - `FetchSettings` (`harness/config.py:60-68`) — `page_timeout_ms`, `max_concurrency`,
    `per_page_char_cap`, `max_urls_per_call`. `HarnessConfig` is a `_StrictModel`
    (`extra="forbid"`): an unknown `harness.toml` key fails startup loudly.
  - `classify()` and `FetchOutcome` (`harness/tools/fetch.py:30,48-70`) — the existing
    typed-failure disclosure seam; `_BLOCKED_STATUSES = frozenset({403, 429, 503})`.
  - `FetchedPage` (`harness/tools/fetch.py:73-84`) — per-URL record: outcome, status_code,
    title, markdown, error.
  - `_render()` (`harness/tools/fetch.py:133-158`) — boundary-aware truncation at
    `per_page_char_cap`. Not changed by this work.
  - `normalize_url()` (`harness/sources.py:20-61`) — URL canonicalization; the blocklist's
    host key derives from the same parsing conventions.
  - `SourceRegistry.add()` (`harness/sources.py:90-102`) — in-memory `[Sn]` citation IDs.
- Comparable prior art: `harness/tools/search.py` — same factory shape and typed-failure
  convention (`SearchFailure`); the pattern the blocklist-aware tool mirrors.
- No file-write precedent exists anywhere in `harness/` (only `config.py:102` reads
  `harness.toml`) — the blocklist is genuinely new surface.
- Tests: `tests/test_fetch.py` (~700 lines) monkeypatches `harness.tools.fetch.AsyncWebCrawler`
  with a `_FakeCrawler` returning `_FakeResult`/`_FakeMarkdown` stand-ins — fully offline.
  `tests/conftest.py` supplies `make_config`. pytest + pytest-asyncio, `asyncio_mode = "auto"`.
  No clock fixture exists.
- Commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`.

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- Making `SourceRegistry` thread-safe — `add()` has no `await` between its check and write,
  so it is already safe for in-process asyncio agents; only real threads would break it.
- Reworking `classify()`'s existing `non_html`/404 gaps (backlog items) beyond what PDF
  precheck requires.
- Any change to `_render()` truncation or the five-URL `max_urls_per_call` cap.

## Design Decisions

### D1: Per-URL `arun()` calls replace the `arun_many` batch
- **Chosen:** One long-lived HTTP-strategy `AsyncWebCrawler`; each URL goes through its own
  `arun()` wrapped in `asyncio.wait_for(...)`, bounded by our own `asyncio.Semaphore`, with
  our own retry loop.
- **Rejected:** Keeping `arun_many` + `MemoryAdaptiveDispatcher` — crawl4ai exposes no
  per-URL deadline on the batch API, so R2's no-hang guarantee would degrade to a batch-level
  timeout, and per-URL retry/escalation would have no natural seam.
- **Consequences:** `MemoryAdaptiveDispatcher`'s memory backpressure is lost (see !#2), so
  browser concurrency must be capped low and explicitly. `_RATE_LIMIT_MAX_RETRIES` and the
  dispatcher/`RateLimiter` imports go away.

### D2: HTTP status recovered by parsing `error_message`
- **Chosen:** On a non-2xx the HTTP strategy raises internally and `CrawlResult.status_code`
  is `None`; the numeric status survives only inside `error_message` (`"HTTP 403: ..."`).
  A helper parses it back out and feeds both `classify()` and the blocklist gate.
- **Rejected:** Setting `CrawlerRunConfig(max_retries>=1)` so crawl4ai surfaces the status
  itself — it buys the status at the price of hidden extra fetches we cannot deadline,
  directly fighting R2 and R4's explicit attempt budget.
- **Consequences:** A string-format dependency on a pinned library version (!#1). Any
  crawl4ai upgrade must re-verify this parse.

### D3: JS-shell detection by generated-markdown word count
- **Chosen:** An HTTP fetch that succeeds but yields fewer than `min_markdown_words` words
  of generated markdown escalates once to Chromium.
- **Rejected:** Raw-HTML shell markers (`<div id="root">`, noscript patterns) — a pattern
  list to maintain; word count measures what the agent actually consumes.
- **Consequences:** One tunable threshold; both mis-tuning directions cost real work (!#5).
  Escalation is explicitly not a retry, so it composes with R4's budget rather than
  consuming it.

### D4: Blocklist as an atomically-replaced JSON map
- **Chosen:** `{"example.com": "2026-08-17T09:30:00Z"}` at a configured path; loaded once per
  tool call, pruned on load against a 30-day TTL, written via temp-file + `os.replace`.
  A new `harness/blocklist.py` holds it — no file-persistence precedent exists to extend, and
  a flat module matches `config.py`/`sources.py`/`prompts.py`.
- **Rejected:** SQLite (contradicts the no-DB constraint for single-user runs);
  plain-text lines (hand-rolled parse where `json.load` is free); file locking (machinery
  built for an agent loop that does not exist).
- **Consequences:** Concurrent writers are last-write-wins; a lost entry is benign and
  re-learned on the next 403. Atomic replace means a reader never sees a torn file. TTL logic
  takes an injectable clock, since no clock seam exists in the test suite.

### D5: PDFs skipped via a HEAD precheck
- **Chosen:** An `httpx.head()` (already a dependency) before fetching; `application/pdf`
  short-circuits to the existing `non_html` outcome.
- **Rejected:** Adding the `crawl4ai[pdf]` extra + `pypdf` — `PDFContentScrapingStrategy`
  downloads via a synchronous `requests.get()` inside the event loop, reintroducing exactly
  the blocking class this plan removes. Also rejected: fetch-and-discard, which writes the
  file to disk anyway.
- **Consequences:** One extra round trip per URL (!#3); servers rejecting HEAD fall through
  to a normal fetch. `downloads_path` is still pinned into the workspace as a backstop for
  any other non-HTML type.

### D6: Subagent cap is a config contract, not runtime enforcement
- **Chosen:** A validated `max_subagents` key the future agent loop must honor.
- **Rejected:** Building enforcement now — there is no loop, no caller, and nothing to test
  against.
- **Consequences:** Worst-case fetch load is `max_subagents * http_concurrency` (3 * 10 = 30)
  once the loop lands. The bound is declared, not enforced, until then.

## Requirements Coverage

| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | HTTP-first, Chromium only for JS | Phase 1 (HTTP path primary), Phase 2 (escalation), Phase 3 (PDF never escalates) |
| R2 | Hard per-strategy deadline | Phase 1 (HTTP ~3s), Phase 2 (browser ~20s) |
| R3 | 403/401 skips whole domain, 30d TTL | Phase 4 |
| R4 | Max 2 retries | Phase 1 |
| R5 | Degraded coverage disclosed | Phase 1 (timeout/retry outcomes), Phase 3 (`non_html`), Phase 4 (`skipped`) |
| R6 | Bounded concurrent subagents | Phase 5 |

## Progress
- [x] Phase 1: HTTP-first fetch with hard deadline and retry budget
- [x] Phase 2: Chromium escalation for JS shells
- [x] Phase 3: PDF precheck and download containment
- [x] Phase 4: Persistent domain blocklist
- [x] Phase 5: Subagent cap config contract
- [x] Final verification (offline gates; the live mixed-URL check remains outstanding)

## Phases

### Phase 1: HTTP-first fetch with hard deadline and retry budget
**Risk:** flagged (!#1, !#2)
**Test-first:** required
**Goal:** `fetch_pages` fetches every URL over crawl4ai's HTTP strategy, per-URL, under a hard
~3s deadline with at most 2 retries — no Chromium is launched at all.
**Requirements:** R1 (HTTP path), R2 (HTTP half), R4, R5 (timeout/retry disclosure)
**Files:**
- `harness/tools/fetch.py` — swap `BrowserConfig` for `AsyncHTTPCrawlerStrategy`; replace
  `arun_many`/dispatcher with per-URL `arun()` under `asyncio.wait_for` + semaphore + retry loop
- `harness/config.py` — `FetchSettings`: add `http_deadline_ms`, `max_retries`; rename
  `max_concurrency` to `http_concurrency`
- `harness.toml` — matching `[fetch]` keys (strict model: rename must land in both)
- `tests/test_fetch.py` — extend the `_FakeCrawler` fixture with per-URL `arun`
**Diff budget:** ~180-260 lines across 4 files

**Reuse:**
- Extend `FetchSettings` in `harness/config.py` — do NOT create a new settings model
- ~~Keep `classify()`, `FetchedPage`, `_render()`, `_pair`/dedup in `harness/tools/fetch.py`
  unchanged in shape — only the fetch mechanism below them changes~~
  (amended 2026-08-17 — see `## Reconciliations` #1: `_pair` is deleted; the rest stand)
- Keep `classify()`, `FetchedPage`, `_render()`, and the URL dedup at the top of `_fetch()`
  unchanged in shape — only the fetch mechanism below them changes
- Pattern to mirror: `tests/test_fetch.py`'s `install_crawler` monkeypatch — fake at the
  `AsyncWebCrawler` boundary, never hit the network

**Contracts:**
- `_status_from_error(error: str | None) -> int | None` — parses `"HTTP <code>: ..."`;
  Phase 4's blocklist gate and `classify()` both consume it
- `async def _fetch_one(crawler, url: str, run_config, deadline_ms: int) -> FetchedPage` —
  one deadlined attempt, never raises; Phase 2 wraps it for escalation
- `FetchSettings.http_deadline_ms: int` (default 3000), `FetchSettings.max_retries: int`
  (default 2), `FetchSettings.http_concurrency: int` (default 10) — all `gt=0`
- Retryable set: timeout, network error, 5xx. Not retryable: 4xx other than the 5xx set

**Out of scope:**
- Any Chromium/browser code path (Phase 2) — this phase must not import `BrowserConfig`
- Blocklist, PDF precheck, `max_subagents`
- Touching `_render()` truncation or `max_urls_per_call`

**Tests (write first, confirm red):**
- [x] A URL exceeding the deadline yields a `timeout` outcome and never blocks siblings
- [x] Retryable failures are attempted exactly 3 times total; non-retryable 4xx exactly once
- [x] A non-2xx recovers its numeric status through `_status_from_error` into `classify()`
- [x] A successful fetch produces the same `FetchedPage`/markdown shape as before the swap
- [x] Concurrency never exceeds `http_concurrency` simultaneous in-flight fetches

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the config fields and the matching `harness.toml` keys (rename `max_concurrency`).
3. Replace the crawler construction and batch call with the per-URL deadlined loop.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `grep -rn "BrowserConfig\|arun_many\|MemoryAdaptiveDispatcher" harness/` returns nothing
- [ ] A live fetch of a static page per docs/guides/setup.md returns markdown, and no
  Chromium process appears in `ps` during the run — **not run**: needs the homelab box;
  deferred to the plan's final live verification

### Phase 2: Chromium escalation for JS shells
**Risk:** flagged (!#2, !#5)
**Test-first:** required
**Goal:** An HTTP fetch that succeeds but yields near-empty markdown escalates once to
Chromium, under its own hard deadline and a low concurrency cap.
**Requirements:** R1 (escalation), R2 (browser half)
**Assumes:**
- Phase 1's `_fetch_one` and the per-URL loop are in place.
**Files:**
- `harness/tools/fetch.py` — thin-content check, lazily-created browser crawler, escalation
  pass over thin results
- `harness/config.py` — add `min_markdown_words`, `browser_deadline_ms`, `browser_concurrency`
- `harness.toml` — matching keys
- `tests/test_fetch.py` — fake browser crawler alongside the HTTP fake
**Diff budget:** ~120-180 lines across 4 files

**Reuse:**
- Reuse Phase 1's `_fetch_one` for the browser attempt — do NOT write a second fetch path;
  it differs only by crawler instance and deadline
- ~~Keep the existing `page_timeout_ms` as the browser `CrawlerRunConfig` page timeout~~
  (amended 2026-08-17 — see `## Reconciliations` #3: the browser gets its own run config)

**Contracts:**
- `FetchSettings.min_markdown_words: int` (default 50), `browser_deadline_ms: int`
  (default 20000), `browser_concurrency: int` (default 2) — all `gt=0`
- The browser crawler is created lazily on first escalation and closed with the tool call —
  a run with no thin results launches no browser
- Escalation is at most one attempt per URL and does NOT consume the R4 retry budget

**Out of scope:**
- ~~Escalating on failure outcomes (only thin-but-successful results escalate)~~
  (amended 2026-08-17 — see `## Reconciliations` #2: an empty-but-HTML `non_html` result
  escalates too; genuine failure outcomes still never escalate)
- `text_mode` (it disables JavaScript, defeating escalation); `light_mode` is optional
- Blocklist and PDF handling

**Tests (write first, confirm red):**
- [x] A thin-markdown success escalates exactly once and returns the browser's richer result
- [x] A rich HTTP result never escalates, and no browser crawler is constructed
- [x] A failed/timed-out HTTP fetch does not escalate
- [x] An escalation exceeding `browser_deadline_ms` yields `timeout`, not a hang
- [x] Escalations in flight never exceed `browser_concurrency`

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add config fields plus `harness.toml` keys.
3. Add the thin-content predicate and the lazy browser escalation pass.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] A live fetch of a static page launches no Chromium; a live fetch of a known
  JS-rendered page returns non-empty markdown — **not run**: needs the homelab box. This is
  now the check that settles Reconciliation #3 (`wait_until="networkidle"` actually renders)
  and risk !#5's escalation rate; deferred to the plan's final live verification.

### Phase 3: PDF precheck and download containment
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** PDF URLs are identified before their body is fetched and disclosed as `non_html`,
and no crawl4ai download ever lands outside the workspace.
**Requirements:** R1 (PDF case), R5 (disclosure)
**Assumes:**
- Phase 1's per-URL loop exists to host the precheck.
**Files:**
- `harness/tools/fetch.py` — `httpx.head()` precheck ahead of the fetch; pin `downloads_path`
- `harness/config.py` — add `downloads_dir` (workspace-relative)
- `harness.toml` — matching key
- `tests/test_fetch.py` — fake HEAD responses
**Diff budget:** ~80-120 lines across 4 files

**Reuse:**
- `httpx` is already a declared dependency — do NOT add a new HTTP client
- Reuse the existing `non_html` outcome and `FetchedPage` shape; no new outcome value here

**Contracts:**
- `FetchSettings.downloads_dir: str` — passed to crawl4ai as `downloads_path` so nothing
  writes to `~/.crawl4ai/downloads`
- A HEAD that errors, times out, or is rejected falls through to a normal fetch attempt
  (never a hard failure)

**Out of scope:**
- Extracting PDF text, adding `pypdf`/`crawl4ai[pdf]`
- Fixing the backlog's broader `non_html`/404 classification gaps
- Prechecking content types other than PDF

**Tests (write first, confirm red):**
- [x] An `application/pdf` HEAD yields `non_html` with no fetch attempted and no file written
- [x] A `text/html` HEAD proceeds to a normal fetch
- [x] A HEAD that fails or times out falls through to a normal fetch
- [x] The crawler is constructed with `downloads_path` inside the configured workspace

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add `downloads_dir` config plus the `harness.toml` key and pin it on the crawler.
3. Add the HEAD precheck to the per-URL path.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] After a live run against a PDF URL, `~/.crawl4ai/downloads` gains no new file —
  **not run**: needs the homelab box. Statically confirmed during review that the 0.9.2
  fallback to `~/.crawl4ai/downloads` (`async_crawler_strategy.py:2739`) is the only such
  write path and is now closed by the pinned `downloads_path`.

### Phase 4: Persistent domain blocklist
**Risk:** flagged (!#4)
**Test-first:** required
**Goal:** A 403 or 401 records the domain in a persistent JSON blocklist; blocked domains are
skipped without a fetch for 30 days and disclosed as skipped.
**Requirements:** R3, R5 (skip disclosure)
**Assumes:**
- Phase 1's `_status_from_error` reliably recovers 403/401.
**Files:**
- `harness/blocklist.py` — NEW: load/prune/record/contains over a JSON map, injectable clock.
  New file because no file-persistence precedent exists and a flat module matches
  `config.py`/`sources.py`
- `harness/tools/fetch.py` — gate before fetch; record on 403/401; `skipped` outcome
- `harness/config.py` — add `blocklist_path`, `blocklist_ttl_days`
- `harness.toml` — matching keys
- `tests/test_blocklist.py` — NEW: TTL, prune, atomic write, malformed-file cases
- `tests/test_fetch.py` — gate and record integration
**Diff budget:** ~180-260 lines across 6 files

**Reuse:**
- Derive the host key with the same parsing conventions as `normalize_url()` in
  `harness/sources.py` — do NOT hand-roll a second URL parser
- Pattern to mirror: `harness/tools/search.py`'s typed-failure convention for the new outcome
- `make_config` in `tests/conftest.py` for config construction; `tmp_path` for the file

**Contracts:**
- `FetchOutcome` gains `"skipped"` — the disclosure value for a blocklisted domain
- `FetchSettings.blocklist_path: str`, `blocklist_ttl_days: int` (default 30, `gt=0`)
- On-disk format: JSON object mapping lowercased host to an ISO-8601 UTC timestamp
- Blocklist functions take an injectable clock (defaulting to UTC now) so TTL is testable
- Writes go through temp-file + `os.replace`; readers never observe a partial file

**Out of scope:**
- File locking or any cross-process coordination (D4)
- Blocking on 429/503 — those stay ordinary retryable/blocked outcomes
- A CLI or tool for editing the blocklist (it is hand-editable JSON)

**Tests (write first, confirm red):**
- [x] A 403 and a 401 each record the domain; a 429 does not
- [x] A blocked domain is skipped with no fetch attempted, disclosed as `skipped`
- [x] Entries older than the TTL are pruned on load; fresh entries survive
- [x] A missing or malformed blocklist file degrades to empty rather than raising
- [x] A recorded write leaves a complete, parseable file (atomic replace)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Build `harness/blocklist.py` with the clock seam.
3. Add config keys plus `harness.toml` entries.
4. Wire the gate and the record path into the per-URL loop; add the `skipped` outcome.
5. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] Two consecutive live runs against a 403ing URL show a fetch on the first and a
  `skipped` disclosure with no request on the second — **not run**: needs the homelab box;
  deferred to the plan's final live verification

### Phase 5: Subagent cap config contract
**Risk:** none
**Test-first:** required
**Goal:** A validated `max_subagents` setting exists and is documented as the bound the
future agent loop must honor.
**Requirements:** R6
**Files:**
- `harness/config.py` — add `max_subagents` to the roles/agent settings
- `harness.toml` — matching key with default 3
- `docs/architecture.md` — record the bound and its worst-case fetch load
- `tests/test_config.py` — validation coverage (create if absent)
**Diff budget:** ~30-50 lines across 4 files

**Reuse:**
- Extend the existing settings model that already validates the `head`/`subagent` role names
  (`harness/config.py:84`) — do NOT add a parallel config surface

**Contracts:**
- `max_subagents: int` (default 3, `gt=0`) — the agent loop, when built, must not run more
  than this many subagents concurrently

**Out of scope:**
- Any runtime enforcement, scheduler, or shared-budget machinery (D6)
- Building or wiring the agent loop

**Tests (write first, confirm red):**
- [x] The key loads with its default and rejects non-positive values

**Steps:**
1. Write the test above; run it; confirm it FAILS (red).
2. Add the field, the `harness.toml` key, and the architecture note.
3. Run the test; confirm it PASSES (green).

**Acceptance criteria:**
- [x] `docs/architecture.md` states the cap and the `max_subagents * http_concurrency`
  worst-case fetch load

## Verification
- [x] `uv run pytest` — 157 passed
- [x] `uv run ruff check .`
- [x] `uv run ruff format --check .`
- [x] `uv run mypy .`
- [x] Coverage stays at or above the CI 90% floor — 99% total (`blocklist.py` 98%,
  `fetch.py` 99%)
- [ ] Live check per docs/guides/setup.md: a mixed URL set (static page, JS-rendered page,
  PDF, 403ing domain) returns one disclosed outcome each and no run hangs — **not run**:
  needs the homelab box. This single check also settles Reconciliation #3
  (`wait_until="networkidle"` really renders an SPA), risk !#5's escalation rate at 50 words,
  and each phase's deferred live acceptance criterion.

## Notes
- `docs/decisions.md` currently records "Chromium via crawl4ai-managed Playwright is the only
  path, and no config key selects a browser". This plan deliberately reverses that; the
  decision log needs an entry saying so when the work lands.
- `docs/backlog.md` items this touches: the missing retry pass (closed by Phase 1) and the
  PDF classification gap (partly closed by Phase 3). The 404-as-`fetched` gap is untouched.
- Both crawl4ai landmines from the Intent constraints (`downloads_path`, shared
  `user_data_dir`) are recorded there rather than restated per phase.

## Risks
#1. **The HTTP status code is recovered by string-parsing crawl4ai's error message** — on a
    non-2xx the 0.9.2 HTTP strategy raises internally, leaving `CrawlResult.status_code` as
    `None`; the number survives only inside `error_message` as `"HTTP 403: ..."`. The 403/401
    blocklist gate therefore rests on a library-internal string format. It is stable because
    the version is pinned exactly. Confirm the parse against the installed package before
    any crawl4ai upgrade, and keep a test asserting the exact message shape.
#2. **Dropping `arun_many` removes crawl4ai's memory backpressure** — `MemoryAdaptiveDispatcher`
    only applies to the batch API, so per-URL calls lose its system-wide memory throttling.
    That is harmless on the HTTP path (small buffered bodies) but means Chromium escalation
    concurrency is bounded only by our own cap. Keep `browser_concurrency` low (2) and treat
    raising it as a decision requiring a memory measurement on the box.
#3. **The HEAD precheck adds a round trip and some servers reject HEAD** — cost is one extra
    request per URL, which the 3s deadline bounds. Servers answering HEAD with 405 or a wrong
    Content-Type fall through to a normal fetch, so the failure mode is "no worse than today",
    but a PDF served by such a server will still be downloaded; `downloads_path` containment
    is the backstop.
#4. **A transient 403 locks out a domain for 30 days** — a one-off block (aggressive WAF, a
    rate-limit answered as 403) blocklists the whole domain well beyond the incident. The file
    is hand-editable JSON and the entry expires on its own; if false positives show up in
    practice, the cheap fix is requiring two strikes before recording, not shortening the TTL.
#5. **The thin-content threshold is a tuning guess** — too high and ordinary short pages
    escalate to Chromium (slow, defeats the plan's purpose); too low and real JS shells are
    returned as near-empty sources. Start at 50 words, and check the escalation rate on a live
    run before treating the default as settled.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

#1. 2026-08-17 — Phase 1: the Reuse line requires keeping `_pair()` unchanged, but D1's
    per-URL design makes it unreachable — `asyncio.gather` preserves input order, so each
    result is already bound to its own URL and there is nothing left to pair. The hazard
    `_pair` was written for (crawl4ai's `arun_many` returning results in arbitrary order,
    or two results for one URL under memory pressure) is structurally impossible once each
    URL gets its own `arun()` call. → **Amendment:** delete `_pair()` and the two
    `arun_many`-specific tests that pin it (`tests/test_fetch.py` ~lines 505-540), and add
    a test asserting output order matches input order — the property `_pair` used to buy,
    now guaranteed by `gather`. Every other name in the struck bullet (`classify`,
    `FetchedPage`, `_render`, the URL dedup) is kept unchanged as written. No requirement
    is affected: R1/R2/R4/R5 say nothing about the pairing mechanism.

#2. 2026-08-17 — Phase 2: restricting escalation to `outcome == "fetched"` excludes the
    canonical JS shell and so fails R1. A page serving `<div id="root"></div>` returns 200
    `text/html` with empty generated markdown, and `classify()` maps "no error + empty
    markdown" to `non_html` — so the zero-word case, the strongest shell signal R1 names,
    was the one case that could never escalate. → **Amendment:** `_is_thin` escalates a
    `non_html` result too, but only when it looks like an empty HTML page rather than a real
    non-HTML resource: no error, and a content type that is HTML or absent. To make that
    decidable, `FetchedPage` gains `content_type: str | None` (already read by `_content_type`
    for `classify()`, previously discarded). Genuine failure outcomes — `timeout`, `error`,
    `blocked` — still never escalate, so the struck line's intent is preserved. Side benefit:
    a PDF's `application/pdf` fails the HTML check, so it does not escalate even before
    Phase 3's HEAD precheck lands.

#3. 2026-08-17 — Phase 2: reusing the HTTP `CrawlerRunConfig` for the browser attempt
    defeats the escalation. crawl4ai 0.9.2 defaults `wait_until="domcontentloaded"` and
    `delay_before_return_html=0.1`, and DOMContentLoaded fires before client-side render —
    so Chromium can return the same near-empty markdown the HTTP path already produced, at
    the cost of a full browser launch. The shared `page_timeout=page_timeout_ms` (15000) also
    binds 5s before `browser_deadline_ms` (20000), leaving that key mostly inert. →
    **Amendment:** the browser gets its own `CrawlerRunConfig` with a render-aware wait and
    `page_timeout` aligned to `browser_deadline_ms`; `asyncio.wait_for` remains the hard
    no-hang bound. The HTTP run config is unchanged. Risk !#5's live escalation-rate check
    should now also confirm escalated pages come back non-empty.

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

#1. 2026-08-17 — Phase 1, **deferred**: dropping `BrowserConfig(verbose=False)` lets one
    `Crawl4AI <version>` banner per `_fetch()` call reach stdout. `AsyncWebCrawler.__init__`
    falls back to `BrowserConfig()` (whose `verbose` defaults `True`) to build its logger, and
    `start()` prints the banner before any `CrawlerRunConfig` is read. Per-`arun` logging is
    still silenced (`arun` sets `self.logger.verbose` from the run config), so this is cosmetic
    noise, not leaked crawl detail. Suppressing it would mean importing `BrowserConfig`, which
    Phase 1's Out-of-scope forbids. Revisit in Phase 2, which legitimately constructs a browser
    crawler and can set the flag there.

#2. 2026-08-17 — Phase 2, **acted on**: `min_markdown_words=50` is correct for production but
    retroactively makes 11 pre-existing Phase 1 fixtures ("fine", "A content", `"x"*500` with
    no spaces) count as JS shells and escalate, since none were written with a word budget in
    mind. → `make_config` in `tests/conftest.py` defaults `min_markdown_words` to 1, so
    escalation is opt-in per test and every pre-Phase-2 test keeps the semantics it was
    written with; the Phase 2 tests pass 50 explicitly. `FetchSettings`/`harness.toml` keep
    the real default of 50, now pinned by a test asserting the model default directly so the
    divergence between test factory and production cannot drift unnoticed.

## Phase Handoff Log

### 2026-08-17 — Phase 1: HTTP-first fetch with hard deadline and retry budget
- Done: `_fetch()` now runs one `arun()` per URL over `AsyncHTTPCrawlerStrategy`, each attempt
  under `asyncio.wait_for` and an `asyncio.Semaphore(http_concurrency)`, with an explicit
  `_fetch_with_retries` budget. `_status_from_error`, `_fetch_one`, `_is_retryable` added;
  `_pair`, `_MEMORY_THRESHOLD_PERCENT`, `_RATE_LIMIT_MAX_RETRIES` deleted. `FetchSettings`
  gained `http_deadline_ms`/`max_retries` and renamed `max_concurrency` to `http_concurrency`
  (propagated to `harness.toml`, `conftest.py`, `test_config.py`). 121 tests green; ruff,
  format, mypy clean.
- Learned: `AsyncHTTPCrawlerStrategy`/`HTTPCrawlerConfig` are NOT exported from top-level
  `crawl4ai` — import from `crawl4ai.async_crawler_strategy`. `HTTPCrawlerConfig` has no
  `verbose` field. Production never sees a bare `"HTTP 403: ..."`: `AsyncWebCrawler.arun`
  catches the internal `HTTPStatusError` and stores it wrapped in a traceback blob with
  trailing code context, so the !#1 parse survives only because `Error:` precedes
  `Code context:` and the regex takes the first match — now pinned by a test.
  `_fetch_one` deliberately leaves `source_id=""`; the caller assigns `[Sn]` in input order
  after `gather`, because `SourceRegistry.add()` numbers by insertion and registering from
  concurrent tasks would number by completion order.
- Drift: Reconciliation #1 — `_pair()` deleted as unreachable under per-URL fetching
  (approved); replaced by an input-order test. Discovery #1 logged and deferred (stdout
  banner from the dropped `BrowserConfig(verbose=False)`).
- Watch-next: Phase 2 constructs a browser crawler — set `BrowserConfig(verbose=False)` there
  to close Discovery #1. Reuse `_fetch_one` for the browser attempt (do not write a second
  fetch path) and remember escalation must NOT consume the R4 retry budget.

### 2026-08-17 — Phase 2: Chromium escalation for JS shells
- Done: `_is_thin` + `_escalate_one` added; `_fetch()` lazily builds a browser
  `AsyncWebCrawler(config=BrowserConfig(verbose=False))` only when thin results exist and
  re-fetches each once under `browser_deadline_ms` and `Semaphore(browser_concurrency)`,
  reusing `_fetch_one`. `FetchSettings` gained `min_markdown_words` (50),
  `browser_deadline_ms` (20000), `browser_concurrency` (2). `FetchedPage` gained
  `content_type: str | None = None`. 131 tests green; ruff, format, mypy clean.
- Learned: `classify()` maps a 200 `text/html` page with empty markdown to `non_html`, NOT
  `fetched` — so the canonical SPA shell needed an explicit escalation branch (Recon #2);
  `_is_thin` now also escalates `non_html` when `content_type` is absent or HTML-like, which
  incidentally keeps PDFs out. crawl4ai 0.9.2's `CrawlerRunConfig` defaults
  `wait_until="domcontentloaded"` and `delay_before_return_html=0.1`, passed straight to
  Playwright's `page.goto`, so the browser needed its own run config with
  `wait_until="networkidle"` (Recon #3). Test fixtures now use per-instance
  `in_flight`/`max_in_flight` via `fake_cls.instances[N]`, since two crawlers share the class.
- Drift: Reconciliations #2 and #3 (both approved, both from the 3F review's Major findings).
  Discovery #2 logged and acted on: `make_config` defaults `min_markdown_words` to 1 so
  pre-Phase-2 tests keep their semantics; production's 50 is pinned in `tests/test_config.py`.
  Discovery #1 closed for the browser path only; the HTTP crawler still prints one banner.
- Watch-next: two escalation behaviors rest on unrun live checks — that `networkidle` really
  renders an SPA, and risk !#5's escalation rate at 50 words. Phase 3's HEAD precheck is the
  next thing to touch `_fetch()`'s per-URL path; it must land ahead of the fetch, and its
  `downloads_path` goes on `HTTPCrawlerConfig`, not the strategy.

### 2026-08-17 — Phase 3: PDF precheck and download containment
- Done: `_is_pdf()` HEAD precheck runs once per URL at the top of `_fetch_with_retries`
  (inside the semaphore, before the retry loop); an `application/pdf` 2xx short-circuits to
  `non_html` with no body fetched. `downloads_path` pinned on both `HTTPCrawlerConfig` and
  `BrowserConfig` from the new `FetchSettings.downloads_dir`. 139 tests green; ruff, format,
  mypy clean.
- Learned: `httpx.InvalidURL` is NOT a subclass of `httpx.HTTPError` — a malformed URL raises
  during URL parsing, before any transport, so `_is_pdf`'s except must be broad or the
  exception escapes `asyncio.gather` (called without `return_exceptions`) and sinks the whole
  `fetch_pages` call. Found as a Blocker in review, fixed with a regression test. httpx's
  `timeout=` is PER-PHASE (connect/read/write/pool each get the full value), not a total, so
  the HEAD is additionally wrapped in `asyncio.wait_for`. crawl4ai's only
  `~/.crawl4ai/downloads` write path is the `browser_config.downloads_path or <default>`
  fallback at `async_crawler_strategy.py:2739`, now closed. `BrowserConfig.downloads_path`
  only matters under `accept_downloads` (defaults False).
- Drift: none. Every `_fetch()` test now needs a HEAD stub, so `tests/test_fetch.py` gained an
  autouse `_default_head_response` fixture returning `text/html`; review confirmed it is
  offline-safe via `httpx.MockTransport` and that no precheck test rides it implicitly.
- Watch-next: Phase 4 adds the `skipped` outcome and gates on the blocklist — the gate must
  sit BEFORE `_is_pdf` so a blocked domain costs no request at all, and `FetchOutcome` is a
  `Literal`, so adding `"skipped"` touches the type and every exhaustive check over it.

### 2026-08-17 — Phase 4: Persistent domain blocklist
- Done: new `harness/blocklist.py` (`load`/`record` over a JSON `{host: iso-timestamp}` map,
  injectable clock, TTL pruned on load, atomic temp-file + `os.replace`). `FetchOutcome` gains
  `"skipped"`; `_fetch()` loads the blocklist once per call and gates BEFORE the HEAD precheck
  and before crawler construction, so a blocked domain costs zero requests and an all-skipped
  call builds no crawler; 401/403 hosts are recorded once each after the gather. New
  `harness/config.py` keys `blocklist_path` and `blocklist_ttl_days`. 155 tests green; ruff,
  format, mypy clean.
- Learned: `datetime.fromisoformat` accepts an offset-LESS `"2026-08-17T09:30:00"` and a bare
  `"2026-08-17"`, returning a naive datetime whose comparison against an aware cutoff raises
  `TypeError` — `load` now coerces naive to UTC. `record` swallows `OSError` (it runs after
  the gather, and the blocklist is regenerable, so a read-only workspace must not discard a
  finished batch) and merges into `load`'s pruned view rather than the raw file, so expired
  entries actually leave. `record` therefore takes `ttl_days`. `_host_of` reuses
  `normalize_url` and cannot raise.
- Drift: none. Three review findings (2 Major, 1 Minor) fixed in-phase with regression tests.
- Watch-next: Phase 5 is small and config-only — `max_subagents` goes on the settings model
  that already validates the `head`/`subagent` role names, plus a `docs/architecture.md` note
  stating the `max_subagents * http_concurrency` worst-case fetch load. Also still open: the
  `docs/decisions.md` entry this plan reverses (see `## Notes`) and the whole live-check set.
- Pattern worth carrying: three phases running, the defect was an exception type missing from
  an `except` clause on a path contracted never to fail the batch. At a batch boundary, guard
  broadly by default; narrow only where a specific type is genuinely handled differently.

### 2026-08-17 — Phase 5: Subagent cap config contract
- Done: `HarnessConfig.max_subagents` (default 3, `gt=0`) plus the `harness.toml` key, a
  `## Concurrency Bounds` section in `docs/architecture.md`, and config tests for the default
  and non-positive rejection. No runtime enforcement, per D6. Also closed the plan's
  `## Notes` item: three entries appended to `docs/decisions.md` recording that this work
  reverses the earlier "Chromium is the only path, no config key selects a browser" decision,
  the blocklist design, and the declared-not-enforced subagent cap. 157 tests green; ruff,
  format, mypy clean; coverage 99% against the 90% floor.
- Learned: `HarnessConfig` is `extra="forbid"`, so a rejection test for a not-yet-existing key
  passes for the WRONG reason (unknown key) — the `gt=0` bound had to be mutation-checked
  after the field landed to prove the test actually binds.
- Drift: none. Review verdict clean; one Minor applied (the `3 * 10 = 30` arithmetic now lives
  only in `docs/architecture.md`, not restated in `config.py`).
- Watch-next: nothing blocks further phases — the plan is done offline. The one outstanding
  item is the live mixed-URL run on the homelab box, which settles Reconciliation #3, risk
  !#5's 50-word threshold, and the four deferred per-phase live criteria in one pass.
<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. -->
