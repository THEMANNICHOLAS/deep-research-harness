# PLAN: Tool Feedback and Domain Blocklist

**Status:** In Progress
**Created:** 2026-08-20
**Type:** Single plan

## Intent

**True goal:** Deep-research runs finish in ~15 minutes with a real synthesized report. Today
runs hit the 30-minute wall clock mid-synthesis because the harness silently swallows fetch
rejections (guard blocks, provenance rejections, walled domains), so the models loop retrying
URLs that can never succeed — the run `2026-08-20-172105` burned entire researcher angles on
invisible failures and produced an unusable report.

**Binding outcomes:**
- **R1** — Every tool call's outcome reaches the calling model explicitly (REPL-like): each
  requested URL gets a per-URL result — success (status, then content) or failure with an
  explicit do-not-retry instruction; a batch never returns empty or silent. Re-requesting a
  URL that already failed in this run returns the same verdict instantly, with no network work.
  - A policy rejection (guard block, provenance rejection, blocklisted domain) renders as a
    per-URL failure block that says only that the URL was rejected and must not be retried —
    never WHICH policy rejected it. Rejection reasons go only to the run log and report
    (developer decision 2026-08-20: don't inform the model or a transcript reader what the
    guard caught).
  - A genuine fetch failure (timeout, HTTP error status, empty extraction) keeps its outcome
    and status code in the block, as today — those are operational facts, not policy verdicts.
- **R2** — Zero-width characters never block a page by presence alone: they are stripped from
  fetched content, the stripped text is rescanned, and the page is blocked only if an injection
  signal still fires on the stripped text.
  - Verified false positives this fixes: anthropic.com/engineering pages, docs.langchain.com,
    openai.com — all blocked in run `172105` for zero-width chars in prose/markup/KaTeX.
- **R3** — A blocked-domain set persists across all sessions, keyed by exact hostname (never
  registrable domain), fed only by deliberate anti-bot refusals: HTTP 401, HTTP 403, or a
  detected Cloudflare-style challenge page. ~~Timeouts, 429, and 503 never persist.~~
  (Amended 2026-08-20 — see `## Reconciliations`: a *status* of 429/503/timeout never
  persists, but a 503 or 429 whose body fires a challenge marker does, because that is the
  "detected challenge page" clause of this same requirement.) Each entry
  carries the reason and a timestamp, and the set is operator-editable by hand.
  - Guard verdicts and provenance rejections never feed the set — they are policy verdicts,
    not site refusals.
- **R4** — Blocklisted domains never reach the model: matching results are dropped from
  `search_web` output before rendering (and never provenance-approved), with one aggregate
  disclosure line so the model stops hunting for them; fetch consults the set as a backstop and
  rejects instantly with a per-URL do-not-retry block (opaque, per R1). Both disclosure paths
  also land in the run log for the report's gaps section.
- **R5** — Delegation is capacity-based and harness-enforced: the cap on URLs per reader
  dispatch and on reader dispatches per researcher live in config and are enforced by the
  harness, not stated only in prompt prose.
  - Confirmed values (2026-08-20): URLs per dispatch stays 5 (already schema-enforced);
    reader dispatches per researcher = 6, newly harness-enforced.
- **R6** — Readers carry only the tools needed to read and answer in prose; they have no write
  tools. Researchers keep their note-writing tools.
- **R7** — A run always ends with synthesis: new research stops at a configured margin before
  the wall clock, so the lead writes the final answer from what it has instead of being killed
  mid-research.
  - Confirmed mechanism (2026-08-20): stream-loop soft deadline mirroring the round cap's
    existing `_SYNTHESIZE_NOW` pass; margin default 240s.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**
- Blocklist file lives under `~/deep-research/` alongside workspace/reports.
- Disclosure lines in the run log/report name the walled domains so the operator can hand-edit
  the set when a site changes posture.

**Non-goals:**
- Strict URL provenance policy is unchanged — memory-invented URLs are still rejected; the
  rejection just becomes visible to the model (R1).
- Guard signal families other than the zero-width/obfuscation fix are untouched
  (instruction_override false positives on pages quoting injection examples are accepted).
- No scanning of researcher→reader task-dispatch prose for URLs — the search and fetch choke
  points cover it.
- No TUI/display changes.
- PR #20's reverted http-first-fetch/blocklist design is ignored — this work is a fresh design,
  not a revival.
- No in-run `/`-command to tune these parameters. Every new knob (caps, margin, blocklist
  path) lands in `harness.toml` so a future picker command has a config surface to drive —
  the command itself is deferred to docs/backlog.md.

**Constraints & assumptions:**
- Base branch: `development` (at `760ab39`).
- The blocklist persists as a plain JSON file on disk — the no-database invariant holds.
- Wall clock stays configured at `[agent].wall_clock_seconds` (currently 1800); R7 adds a
  margin, not a new clock.

**Open questions:**
- none

## Codebase Map

- Entry points: `harness/__main__.py` — CLI, stream loop, round cap + `_SYNTHESIZE_NOW`
  bounded synthesis pass, wall clock (`asyncio.timeout` armed at first research tool call,
  ~lines 878-883; `TimeoutError` handler ~950-958).
- Module boundaries: one module per concern under `harness/`; one tool per module under
  `harness/tools/` with `build_<name>_tool` factories listed in `harness/tools/__init__.py`
  (`ToolSets` NamedTuple: lead/researcher/reader). Tools return typed failure values, never
  raise. Disclosure flows through `harness/runlog.py` `RunLog.record(kind, detail)` →
  `harness/report.py` `_gaps_section`.
- Fetch: `harness/tools/fetch.py` — `_fetch` classifies into
  `FetchOutcome = Literal["fetched","blocked","timeout","non_html","error","pdf"]`;
  `_BLOCKED_STATUSES = frozenset({403, 429, 503})` (line 33); `_render` builds the per-URL
  model-facing block (`## [{Sn}] {url}` or `## {url}` + status line + fenced markdown);
  `_failure_detail` builds run-log lines. Guard-blocked (~379-384) and provenance-rejected
  (~322-329) URLs currently vanish with no rendered block; a fully-rejected batch returns
  `("", [])` (early returns ~317, ~331; fall-through at ~478/490).
- Search: `harness/tools/search.py` — `_render` yields numbered `title — url\nsnippet` rows in
  one fence; `_drop_guarded` scans `f"{title}\n{snippet}"` per result; `_approve_survivors`
  (~105-112) calls `registry.approve` on guard survivors only; all-guard-blocked renders
  `'Search for "{query}" returned {n} results, all withheld by the injection guard.'`
- Guard: `harness/guard.py` — `scan(text) -> ScanResult(blocked, signals)`;
  `strip_invisibles(text)`; `_ZERO_WIDTH_CHARS` shared by the obfuscation regex and
  `_INVISIBLE_RE`; docstring pins "scan must see raw text" (the order R2 deliberately revises;
  fetch.py carries a matching frozen-pipeline comment).
- Registry: `harness/sources.py` — `SourceRegistry` (per-run; `approve`/`is_approved` keyed by
  `normalize_url`; `add` mints `[Sn]`); `normalize_url` lowercases `parts.hostname` internally;
  NO standalone hostname helper exists anywhere in `harness/`.
- Agent wiring: `harness/agent.py` — `_reader_spec` grants reader write tools via
  `FilesystemMiddleware(backend=backend)` (not via the tool registry);
  `create_summarization_middleware(reader_model, backend)` offloads evicted history;
  `_task_dispatch_guard` (ToolError+ToolRetry, `tools=["task"]`, max_retries=1) shared by both
  dispatch tiers; `_ToolActivityMiddleware.awrap_tool_call` already distinguishes reader vs
  researcher dispatches via `args["subagent_type"]`; `pending_digest_scope` (sources.py) is the
  per-task-attempt contextvar pattern. NO dispatch-count enforcement exists.
- Config: `harness/config.py` — pydantic `_StrictModel` (extra="forbid") subclasses
  (`FetchSettings`, `SearchSettings`, `GuardSettings`, `AgentSettings`) aggregated on
  `HarnessConfig` via `Field(default_factory=...)`; `workspace_dir`/`reports_dir` default via
  `default_factory=lambda: Path.home() / "deep-research" / ...`. `fetch.max_urls_per_call` is
  schema-enforced on `fetch_pages` (`Field(max_length=...)` + `_install_url_limit_contract`).
- Prompts: `harness/prompts/reader.md` (advertises write tools), `subagent.md` (prose budget
  "at most about 4 searches and 6 reader dispatches"), rendered via `harness/prompts.py`.
- Tests: `tests/` mirrors modules; offline via `tests/conftest.py` fixtures `install_crawler`
  (fake `AsyncWebCrawler`), `ScriptedChatModel`/`scripted_model`, `install_search_transport`
  (httpx.MockTransport), `make_config`, `approve_all`. Injection fixtures in
  `tests/fixtures/injection/` (`attack_obfuscation_zerowidth.txt` interleaves ZWSP inside
  "Ignore all previous instructions" — fires only `obfuscation` today). Run: `uv run pytest`;
  CI adds `--cov-fail-under=90`.
- Commands: `uv run pytest` / `uv run ruff check .` / `uv run ruff format --check .` /
  `uv run mypy .`
- Comparable prior art: search's all-withheld message (the visible-rejection pattern R1
  extends to fetch); the round cap's `_SYNTHESIZE_NOW` pass in `__main__` (decisions.md
  2026-08-15) — the exact mechanism Phase 5 mirrors; `guard.py`'s fixture-justified rule
  discipline — the pattern challenge-marker detection follows.

## Non-Goals
Inherits every `## Intent` non-goal — not re-listed.
- No retry machinery for transient failures (429/503/timeout) — one attempt per URL per run;
  the backlog's "nothing retries a rate-limited page" entry stays deferred.
- No lead-tier researcher-dispatch cap — the round cap and Phase 5's margin bound the lead.
- No TTL/expiry machinery on blocklist entries — the file is hand-editable; expiry is the
  operator deleting a line.

## Design Decisions

### D1: Policy rejections are opaque to the model
- **Chosen:** One uniform per-URL failure block for guard/provenance/blocklist rejections:
  the URL was rejected, do not retry it or request variants of it. The rejecting policy and
  its reason go ONLY to `RunLog` incidents (existing kinds `guard_blocked`,
  `provenance_rejected`, new `domain_blocklisted`) and thence the report.
- **Rejected:** Reason-bearing model-facing messages — they inform an adversarial page (or a
  transcript reader) what the guard caught. Developer call 2026-08-20. Cost accepted: the
  model cannot distinguish "search for this instead" (provenance) from "never available"
  (blocklist); the do-not-retry instruction covers both.
- **Consequences:** All three rejection paths must funnel through one block-builder so the
  wording cannot drift into revealing the source; tests assert the block for all three paths
  is byte-identical apart from the URL.

### D2: Every fetch failure is sticky for the run
- **Chosen:** `SourceRegistry` records every URL whose fetch did not end `fetched` (policy
  rejections AND genuine failures, keyed by `normalize_url`); a re-request short-circuits to
  the same rendered verdict with no crawler work. One attempt per URL per run.
- **Rejected:** Sticky-for-rejections-only, retry-allowed transients — the developer's goal is
  "stop retrying links altogether"; a retried timeout occasionally succeeding is not worth the
  loop risk that burned run `172105`.
- **Consequences:** The registry is the single per-run URL-state home (approval, dedup, now
  verdicts). `fetch_raw` shares the registry, so its recovery path must consult the same
  verdicts (a `fetch_raw` of a failed URL is the one sanctioned second attempt — see Phase 1
  boundary).

### D3: Blocklist is a new module, JSON, exact hostname, refusal-fed only
- **Chosen:** `harness/blocklist.py` (new file: cross-session persistence is a new concern,
  not a tool) owning load/lookup/append of
  `~/deep-research/blocked-domains.json` — `{hostname: {"reason": str, "first_seen": str}}` —
  path overridable via new `[blocklist]` config. Keyed by exact lowercased hostname. Fed only
  by observed 401/403 statuses and challenge-page markers; write is read-merge-`os.replace`.
- **Rejected:** TOML (stdlib has no writer) and plain lines (hand-rolled parsing both ways);
  registrable-domain keying (Cloudflare challenges are per-hostname/record — one walled host
  would ban working siblings); feeding from guard/provenance verdicts (policy verdicts would
  persist past policy changes and permanently filter working domains).
- **Consequences:** First cross-session file in the project. Concurrent runs race on write —
  read-merge-replace loses at most one entry, which self-heals on the next refusal (accepted,
  single-operator homelab). A hostname helper lands in this module and is the one shared
  URL→hostname definition.

### D4: Search-result filtering is the primary choke point; fetch is the backstop
- **Chosen:** Drop blocklisted-hostname results inside `search_web` before guard scan and
  `_approve_survivors`, appending one aggregate disclosure line; `fetch_pages`/`fetch_raw`
  check the blocklist pre-crawl and emit the D1 opaque rejection block.
- **Rejected:** Scanning researcher→reader task-dispatch prose for URLs — a new fragile seam
  over free text; the two existing choke points cover every route a URL can take to a crawler.
- **Consequences:** A dropped result is never rendered and never approved, so provenance
  automatically keeps rejecting it everywhere else.

### D5: The guard strips zero-width chars before scanning
- **Chosen:** `scan` strips invisibles at entry (one place; callers unchanged) and the
  zero-width rule leaves the obfuscation family (it can never fire post-strip). The
  "scan must see raw text" docstrings in guard.py and fetch.py are rewritten to the new
  contract: scan sees stripped text, and stripping is what defeats zero-width obfuscation.
- **Rejected:** Block-on-presence (status quo — measured false positives on anthropic.com,
  docs.langchain.com, openai.com in run `172105`); density thresholds (unmeasured, false
  precision).
- **Consequences:** `attack_obfuscation_zerowidth.txt` now fires `instruction_override`
  (the ZWSP-split phrase reassembles after stripping) — detection is preserved, via the honest
  family. The obfuscation family keeps only its `decode and execute` rule.

### D6: The reader-dispatch cap is a task-middleware counter
- **Chosen:** A middleware on the researcher tier counts `task(subagent_type="reader")`
  dispatches per researcher attempt (contextvar scope, mirroring `pending_digest_scope`;
  dedup by `tool_call["id"]` so a ToolRetry re-invocation is not double-counted) and past
  `[agent].max_reader_dispatches` (default 6) returns a "budget exhausted — report your
  findings now" ToolMessage without dispatching.
- **Rejected:** Prompt-only budget (status quo — proven ignored in run `172105`); schema
  enforcement (cannot count across calls).
- **Consequences:** Prompt text in `subagent.md` and the enforcement now cite one config key,
  so they cannot disagree.

### D7: The synthesis reserve mirrors the round cap
- **Chosen:** `__main__`'s stream loop checks elapsed research time between lead turns;
  crossing `wall_clock_seconds - synthesis_margin_seconds` (margin default 240) triggers the
  existing `_SYNTHESIZE_NOW` bounded synthesis pass and a run-log incident, exactly as the
  round cap does.
- **Rejected:** Dispatch-middleware rejection — fires mid-turn (tighter) but moves run
  lifecycle policy into framework middleware and adds a second stop mechanism beside the one
  that exists.
- **Consequences:** The margin only fires between lead turns; a long-running dispatch can eat
  into it (accepted — the wall clock still backstops, and a post-answer expiry still writes a
  disclosed report).

## Requirements Coverage
| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Per-URL visible, sticky, do-not-retry failures | Phase 1 |
| R2 | Strip-then-rescan zero-width guard | Phase 2 |
| R3 | Persistent hostname blocklist, refusal-fed | Phase 3 |
| R4 | Blocklist filtering at search + fetch | Phase 3 |
| R5 | Harness-enforced dispatch caps | Phase 4 |
| R6 | Readers lose write tools | Phase 4 |
| R7 | Synthesis reserve before wall clock | Phase 5 |

## Progress
- [x] Phase 1: Visible, sticky fetch failures
- [x] Phase 2: Guard strip-then-rescan
- [x] Phase 3: Persistent domain blocklist
- [ ] Phase 4: Enforced caps and reader tool trim
- [ ] Phase 5: Synthesis reserve
- [ ] Final verification

## Phases

### Phase 1: Visible, sticky fetch failures
**Risk:** flagged (!#1)
**Test-first:** required
**Goal:** No fetch outcome is ever silent: guard-blocked and provenance-rejected URLs render
per-URL opaque do-not-retry blocks, a fully-rejected batch returns a message instead of `""`,
and every non-`fetched` URL's verdict is sticky for the run.
**Requirements:** R1
**Files:**
- `harness/tools/fetch.py` — render rejection blocks; short-circuit re-requests; replace the
  three empty-batch sites with an explanatory message
- `harness/sources.py` — per-run failed-URL verdict store on `SourceRegistry`
- `harness/tools/fallback.py` — `fetch_raw` consults the same verdicts
- `tests/test_fetch.py`, `tests/test_sources.py`, `tests/test_fallback.py` — new behavior
**Diff budget:** ~180-300 lines across ~6 files

**Reuse:**
- Extend `_render`/`_failure_detail` in `harness/tools/fetch.py` — do NOT build a parallel
  rendering path
- Verdict store lives on `SourceRegistry` (`harness/sources.py`), keyed by `normalize_url` —
  the existing per-run URL-state home
- Pattern to mirror: search.py's all-withheld message (visible rejection); existing
  `run_log.record` incident kinds stay unchanged

**Contracts:**
- Rejection block (all three policy paths, identical but for the URL):
  `## {url}` + `rejected — do not retry this URL or request variants of it` (D1)
- `SourceRegistry.record_failure(url: str, rendered_block: str) -> None` and
  `SourceRegistry.failed_block(url: str) -> str | None` — Phase 3's blocklist backstop and
  `fetch_raw` both consume these
- A batch where every URL was rejected/failed returns that per-URL content, never `""`

**Out of scope:**
- No blocklist yet (Phase 3); no change to guard behavior (Phase 2); no change to WHAT gets
  rejected — only to what the model sees; no retry machinery; no prompt edits

**Tests (write first, confirm red):**
- [x] A provenance-rejected URL and a guard-blocked URL each render the opaque block, and the
  two blocks are identical apart from the URL (D1)
- [x] A batch of only rejected URLs returns per-URL blocks, not an empty string
- [x] A re-requested failed URL returns the same block with zero crawler invocations
  (`install_crawler` records calls)
- [x] Genuine failures (timeout/error/blocked) still render outcome + status as today
- [x] `fetch_raw` on a failed URL returns the stored verdict without crawling

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the verdict store to `SourceRegistry`; funnel all three rejection paths through one
   block-builder in fetch.py; replace the empty-batch returns; wire `fetch_raw`.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `uv run pytest tests/test_fetch.py tests/test_sources.py tests/test_fallback.py` green
- [x] Existing run-log incidents (`guard_blocked`, `provenance_rejected`, `fetch_failed`)
  still recorded with reasons — verified by existing tests staying green

### Phase 2: Guard strip-then-rescan
**Risk:** none
**Test-first:** required
**Goal:** Zero-width characters alone never block a page; obfuscated attack phrases are still
caught because stripping reassembles them before the scan.
**Requirements:** R2
**Files:**
- `harness/guard.py` — `scan` strips invisibles at entry; remove the zero-width obfuscation
  rule; rewrite the ordering docstrings
- `harness/tools/fetch.py` — update the frozen-pipeline comment (behavior unchanged here)
- `tests/test_guard.py`, `tests/test_fetch.py` — updated expectations
**Diff budget:** ~60-120 lines across ~4 files

**Reuse:**
- Extend `scan` and the existing `_INVISIBLE_RE`/`strip_invisibles` — do NOT add a second
  stripping pass; `strip_invisibles` on survivors stays (idempotent, harmless)
- Fixtures: `tests/fixtures/injection/` — the existing set is the measure of coverage

**Contracts:**
- `scan(text)` semantics: verdict is computed on invisible-stripped text; zero-width presence
  alone yields `blocked=False, signals=[]`

**Out of scope:**
- No new signal families or rules; no changes to `sanitize_for_report`/`fence`; no relaxation
  of any other family

**Tests (write first, confirm red):**
- [x] A benign page containing only ZWSP/ZWNJ/BOM is not blocked
- [x] `attack_obfuscation_zerowidth.txt` still blocks — now via `instruction_override`
  (renamed to `attack_instruction_override_zerowidth.txt`; see the handoff log)
- [x] `strip_invisibles` before `scan` and after produce the same downstream bytes (order
  freeze replaced, not weakened)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Reorder inside `scan`; drop the zero-width rule; update docstrings and the fetch.py comment.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `uv run pytest tests/test_guard.py` green; full suite green (fixture expectation
  changes contained)

### Phase 3: Persistent domain blocklist
**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** Hostnames that deliberately refuse bots (401/403/challenge page) persist to a JSON
file across sessions, are filtered out of search results with one disclosure line, and are
rejected instantly at fetch.
**Requirements:** R3, R4
**Assumes:**
- Phase 1's `SourceRegistry.record_failure`/`failed_block` and opaque-block contract exist
**Files:**
- `harness/blocklist.py` — NEW: the persistence concern (load/contains/add, hostname helper);
  new file because cross-session persistence is a new concern, not a tool
- `harness/config.py` — NEW `[blocklist]` `_StrictModel` (path override; default
  `~/deep-research/blocked-domains.json`)
- `harness/tools/fetch.py` — feed (401/403/challenge markers post-crawl) + pre-crawl backstop;
  add `401` to `_BLOCKED_STATUSES`
- `harness/tools/search.py` — hostname filter before guard/approval + aggregate disclosure line
- `harness.toml` — the new section
- `tests/test_blocklist.py` (new), `tests/test_fetch.py`, `tests/test_search.py`,
  `tests/test_config.py`; `tests/fixtures/challenge/` (new) — one fixture per challenge marker
**Diff budget:** ~300-450 lines across ~9 files

**Reuse:**
- Hostname extraction mirrors `normalize_url`'s own `urlsplit(...).hostname` handling
  (`harness/sources.py`) — defined ONCE in blocklist.py
- Challenge markers follow guard.py's fixture-justified rule discipline: no marker without a
  fixture in `tests/fixtures/challenge/` that fires it
- Config section mirrors `GuardSettings`' shape; disclosure via `run_log.record` with new kind
  `domain_blocklisted`
- Rejection rendering: Phase 1's block-builder, unchanged

**Contracts:**
- File format: `{hostname: {"reason": "401"|"403"|"challenge", "first_seen": ISO-8601}}` —
  hand-editable, unknown keys preserved on rewrite
- `load_blocklist(path: Path) -> Blocklist`; `Blocklist.contains(hostname: str) -> bool`;
  `Blocklist.add(hostname: str, reason: str) -> None` (read-merge-`os.replace`)
- `hostname_of(url: str) -> str | None` — the one shared URL→hostname definition
- Search disclosure line (aggregate, end of results): names the count; the run-log incident
  names the hostnames
- ~~429/503/timeouts NEVER call `Blocklist.add`~~ → a 429/503/timeout STATUS never calls
  `Blocklist.add`; a non-`fetched` page whose body fires a challenge marker does, whatever
  its status (2026-08-20 reconciliation)
- Challenge markers are checked ONLY when `outcome == "blocked"` (i.e. the status is in
  `_BLOCKED_STATUSES`), so they can never fire on real page content; no body-length threshold
  exists (developer decision 2026-08-20, tightened from `!= "fetched"` after the Phase 3
  judgment review — see `## Reconciliations`)

**Out of scope:**
- No TTL/expiry; no blocklist UI; no widening of `classify` beyond adding 401 to
  `_BLOCKED_STATUSES`; no change to guard scanning of results; no researcher-prose scanning

**Tests (write first, confirm red):**
- [x] Round-trip: add → file on disk → fresh load → contains (and a hand-edited unknown key
  survives a rewrite)
- [x] Feed matrix: 401, 403, and each challenge fixture add the hostname; ~~429/503/timeout/~~
  ~~guard-block/provenance-rejection do NOT~~ a bare 429/503/timeout (no marker in the body),
  a guard block and a provenance rejection do NOT; a 503 whose body fires a challenge marker
  DOES (2026-08-20 reconciliation)
- [x] A `fetched` page containing a marker phrase in real prose does NOT add its hostname, and
  neither does a `non_html` one (the `outcome == "blocked"` scoping is what makes the
  length-threshold-free design safe)
- [x] Search: a blocklisted-hostname result is dropped, unapproved, and the disclosure line
  appears; a clean search has no disclosure line
- [x] Fetch backstop: a blocklisted URL renders the Phase-1 opaque block with zero crawler
  invocations
- [x] A 401 response classifies `blocked` (new status)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Build blocklist.py + config section; wire the fetch feed/backstop and search filter.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `uv run pytest` green
- [ ] Manual: run the setup-guide fetch live-check against `https://httpbin.org/status/403`
  twice — first run adds the hostname to `blocked-domains.json`, second run rejects it
  pre-crawl (zero browser work), and the file is human-readable

### Phase 4: Enforced caps and reader tool trim
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** A researcher physically cannot exceed its reader-dispatch budget, and readers have
no write tools.
**Requirements:** R5, R6
**Files:**
- `harness/agent.py` — dispatch-cap middleware on the researcher tier; drop
  `FilesystemMiddleware` from `_reader_spec`
- `harness/config.py` — `max_reader_dispatches` (default 6) on `AgentSettings`
- `harness/prompts/reader.md` — remove write-tool section; `harness/prompts/subagent.md` —
  budget text now states the enforced cap
- `harness.toml` — the new key
- `tests/test_agent.py`, `tests/test_config.py`, `tests/test_prompts.py`
**Diff budget:** ~150-250 lines across ~7 files

**Reuse:**
- Middleware shape mirrors `_ToolActivityMiddleware` (`awrap_tool_call`, keyed on
  `tool_call["name"] == "task"` and `subagent_type == "reader"`); per-attempt scoping mirrors
  `pending_digest_scope` (`harness/sources.py`); retry dedup by `tool_call["id"]` mirrors
  `_ToolActivityMiddleware._reader_ids_by_call`
- Config field mirrors `AgentSettings`' existing bounded ints

**Contracts:**
- Past-cap dispatch returns a ToolMessage: budget exhausted, report findings now — it never
  reaches the reader subgraph
- Reader toolset after the trim: `fetch_pages` only (summarization middleware stays — it
  offloads evicted history to the backend internally and grants no tools)

**Out of scope:**
- No lead-tier dispatch cap; no changes to `_task_dispatch_guard` retry semantics; no
  researcher tool changes; no reader prompt content changes beyond removing tool mentions

**Tests (write first, confirm red):**
- [ ] The 7th reader dispatch in one researcher attempt returns the budget message and spawns
  no reader; a second researcher attempt gets a fresh budget
- [ ] A ToolRetry re-invocation of the same `tool_call["id"]` is not double-counted
- [ ] The reader's bound tool names contain no `write_file`/`edit_file`/`ls`/`glob`/`grep`
- [ ] Rendered prompts state the config value, not a literal

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the config key, the middleware, the `_reader_spec` trim, and the prompt edits.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] `uv run pytest` green
- [ ] Reader summarization still offloads to the backend with `FilesystemMiddleware` gone —
  covered by an existing-or-new agent test, not assumed

### Phase 5: Synthesis reserve
**Risk:** flagged (!#4)
**Test-first:** required
**Goal:** A run whose research phase nears the wall clock synthesizes instead of dying: the
stream loop triggers the existing bounded synthesis pass at the margin.
**Requirements:** R7
**Files:**
- `harness/__main__.py` — elapsed-time check between lead turns; trigger `_SYNTHESIZE_NOW`;
  run-log incident + cut-short disclosure naming the margin
- `harness/config.py` — `synthesis_margin_seconds` (default 240) on `AgentSettings`,
  validated `< wall_clock_seconds`
- `harness.toml` — the new key
- `tests/test_agent.py` (or the `__main__`-level test home the round-cap tests use),
  `tests/test_config.py`
**Diff budget:** ~100-180 lines across ~5 files

**Reuse:**
- Mirror the round cap end to end: its turn-loop check, its `_SYNTHESIZE_NOW` injection, its
  disclosure wording, and its existing tests
  (`test_main_writes_a_cut_short_report_when_the_wall_clock_expires_with_an_answer` is the
  shape to copy)
- Elapsed time reads the same start point the wall clock arms on (first research tool call)

**Contracts:**
- `[agent] synthesis_margin_seconds` — config key; `0` disables the reserve

**Out of scope:**
- No change to the hard wall clock or the failed-run/no-report policy; no mid-dispatch
  cancellation; no new stop mechanism beyond the mirrored one

**Tests (write first, confirm red):**
- [ ] Crossing the margin between turns triggers exactly one bounded synthesis pass and the
  report discloses the margin cut (mirror the round-cap test)
- [ ] A run finishing inside the margin is untouched (no incident, no synthesis injection)
- [ ] `synthesis_margin_seconds >= wall_clock_seconds` fails config validation; `0` disables

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the config key and the stream-loop check mirroring the round-cap path.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] `uv run pytest` green
- [ ] Manual: a live run with `wall_clock_seconds=300, synthesis_margin_seconds=120` writes a
  report whose disclosures name the margin, not a dead run

## Verification
- [ ] `uv run pytest` (CI adds `--cov-fail-under=90` — keep new modules covered)
- [ ] `uv run ruff check .` && `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] End-to-end live run (needs `.env` + SearXNG, see docs/guides/setup.md): a question known
  to surface walled domains (e.g. anthropic.com sources) finishes under the wall clock with a
  synthesized answer; `blocked-domains.json` gains entries; the report's
  `## Gaps and disclosures` names the rejections the model never saw reasons for

## Notes
- docs/backlog.md gains one entry from this plan: the deferred in-run `/`-command for tuning
  the new config knobs (see Intent non-goals).
- CLAUDE.md's Invariants say degraded coverage is "answered and disclosed" — Phase 1 is the
  change that makes the model-facing half of that true; wording there does not need to change.

## Risks
#1. **The rejection short-circuit could mask a needed re-fetch.** D2 makes every failure
    sticky for the run, including transients (timeout, 429). A page that would have succeeded
    on a second try is lost for that run. Chosen deliberately (developer: stop retrying links
    altogether); the alternative (sticky policy-rejections only) is a two-line change if reach
    measurably suffers. Confirm during the live check that coverage stays acceptable.
#2. **A challenge-marker false positive persists across sessions.** A legitimate page whose
    prose contains a marker phrase would blocklist its hostname permanently. Mitigations:
    markers are checked only alongside a refusal-shaped response (not on every fetched page),
    each marker needs a fixture, entries are disclosed in every report, and the JSON is
    hand-editable. Confirm marker scoping in 3C before implementation.
#3. **Dropping `FilesystemMiddleware` may perturb reader summarization.** deepagents'
    summarization wrapper offloads evicted history via the backend, and the plan asserts it
    grants no tools — verify against the installed package during the phase, and keep the
    middleware list otherwise untouched (architecture.md: a nested subagent gets NO
    auto-injected middleware, so every remaining entry is load-bearing).
#4. **The margin only fires between lead turns.** A researcher dispatch that runs long can
    overshoot into the reserve; the hard wall clock still fires and a no-answer expiry still
    writes no report. Accepted with the mirror design (D7); the live check in Phase 5's
    acceptance criteria is the evidence it works in practice.

## Reconciliations
<!-- Drift amendments written by /implement during execution. Append-only. Empty at plan creation. -->

### 2026-08-20 — Phase 1: D2 said two contradictory things about `fetch_raw`

D2's Consequences call a `fetch_raw` of a failed URL "the one sanctioned second attempt", while
Phase 1's own `**Tests (write first, confirm red):**` bullet requires that "`fetch_raw` on a
failed URL returns the stored verdict without crawling". Both cannot hold. **Shipped: the test
bullet's reading** — `fetch_raw` calls the shared `_fetch`, whose replay filter short-circuits
first, so a genuinely failed URL replays its verdict through `fetch_raw` too.

The D2 Consequences sentence is superseded and should be read as: `fetch_raw`'s sanctioned
second attempt is on a URL that fetched **successfully** but whose reader digest was empty or
failed. Such a URL has no stored verdict, so it still crawls; only non-`fetched` outcomes
short-circuit.

Noted for later, not acted on: restoring the other reading — one sanctioned recovery crawl per
URL, reachable only via `fetch_raw` — is the cheapest available mitigation for risk #1 if the
live check shows reach suffering.

### 2026-08-20 — Phase 3: R3 forbids persisting a 503 that IS a challenge page

R3 and Phase 3's Contracts both state flatly that 429/503/timeouts never persist, while the
same requirement asks the set to be fed by "a detected Cloudflare-style challenge page".
Those collide: Cloudflare serves managed challenges with **403**, and under-attack-mode
challenges historically with **503**, so the literal 503 exclusion would discard the single
case where marker detection earns its keep — a 503 body that is a wall rather than an
overload. A 200-status interstitial exists too but is the minority shape.

**Shipped (developer decision 2026-08-20):** the feed is
- `status in {401, 403}` → `add(hostname, str(status))`, unconditional; and
- `outcome != "fetched"` **and** the body fires a challenge marker → `add(hostname, "challenge")`,
  whatever the status.

So a *bare* 429/503/timeout still never persists — what persists is a challenge page, which is
what R3 asked for. Read R3's struck sentence as being about statuses alone.

**Scoping (risk #2, confirmed at 3C as the risk asks, then tightened):** markers are checked
ONLY on a `blocked` outcome — `status_code in _BLOCKED_STATUSES` ({401, 403, 429, 503}), which
is refusal-shaped by construction. That is what makes the design safe with **no body-length
threshold**.

Confirmed at 3C as `outcome != "fetched"` and tightened to `== "blocked"` after the Phase 3
judgment review, which found the looser form unsafe: `classify` also returns `non_html` for ANY
non-HTML content type and `error`/`timeout` whenever a result carries an error message, and all
three can carry genuine extracted page text. A `text/plain` RFC, changelog or mailing-list
archive containing the ordinary English phrase "just a moment" would have been walled
permanently. The tightening costs nothing: 401/403 already feed by status, so the marker's only
remaining job is a 429/503 carrying a challenge body — the case this entry exists for — and an
empty-extraction page has no marker text to match. It also lands the code back on risk #2's own
words, "alongside a refusal-shaped response".

The residual gap is accepted: a 200 interstitial whose challenge text DOES extract classifies
`fetched`, so it is never blocklisted and renders as a junk source.

The false positive that actually bites is not the one risk #2 describes (legit prose quoting a
marker) — it is a **rate-triggered** challenge: Turnstile walls a host you hit hard, and the
entry outlives the burst that caused it. The hand-editable JSON and the per-report disclosure
are the mitigation; no TTL (Non-Goals).

## Discoveries
<!-- Non-contradictory findings logged by /implement during execution. Append-only, empty at plan creation. -->

### 2026-08-20 — Phase 1: a provenance rejection could outlive the approval that invalidated it

Surfaced by the Phase 1 judgment review; the plan considers stickiness for transients (risk #1)
but never a rejection that later becomes valid. Sequence: the model invents a URL from memory →
strict provenance rejects it and the verdict is stored → `search_web` surfaces that same real
URL and `_approve_survivors` approves it → the model fetches it → the replay filter runs before
the provenance check, replays the rejection, and a legitimate source is lost for the run.

**Acted on in Phase 1** (developer decision 2026-08-20). `SourceRegistry.approve` now clears a
stored verdict when — and only when — the URL was not already approved. The invariant that makes
this safe: every non-provenance verdict (guard block, genuine failure, Phase 3's blocklist) is
recorded downstream of `_fetch`'s provenance check and therefore only ever for an
already-approved URL, which that branch cannot reach. Guard-block and failure stickiness is
unaffected, and is pinned by its own regression test.

**Phase 3 must not break this.** If the blocklist backstop is placed BEFORE the provenance check,
it will record verdicts for unapproved URLs and a later approval will clear them. That is
self-healing (the next pre-crawl blocklist check re-rejects) but no longer covered by the
invariant above — check it deliberately rather than assuming.

### 2026-08-20 — Phase 1: every failure now carries a do-not-retry line, not just policy ones

R1 asks for "failure with an explicit do-not-retry instruction"; as first built, only the three
opaque policy paths carried one, so a replayed `blocked — status 403` was byte-identical to a
fresh one and told a retrying model nothing. `_render` now appends `_DO_NOT_RETRY_LINE` to any
non-`fetched` page — on the FIRST failure, not just the replay, since a model that has to spend
the retry to learn the retry is futile has already looped once. Genuine failures keep their
outcome and status per R1; the line is separate text from `_REJECTION_LINE`'s opaque one.

## Phase Handoff Log
<!-- Written by /implement at each 3G phase gate. Append-only, empty at plan creation. MUST remain the LAST section of this file. -->

### 2026-08-20 — Phase 1: Visible, sticky fetch failures
- Done: `SourceRegistry` gained `_failed` + `record_failure`/`failed_block` (keyed by
  `normalize_url`); all three rejection paths in `fetch.py` funnel through one `_rejection_block`
  builder; `_fetch` walks a `requested` list so every deduped URL yields exactly one block;
  `fetch_raw` surfaces stored verdicts instead of silent holes. 706 tests green, four gates clean.
- Learned: (a) D2 kills retry-after-transient-failure, which two pre-existing R5 tests asserted —
  both rewritten to assert the reach cost out loud rather than deleted. (b) `approve()` now
  clears a verdict on FIRST approval only — see the `## Discoveries` entry; the invariant it
  rests on is that every non-provenance verdict is recorded downstream of the provenance check.
  (c) `_render` appends `_DO_NOT_RETRY_LINE` to every non-`fetched` page, not just policy blocks.
- Drift: one `## Reconciliations` entry — D2's Consequences and Phase 1's test bullet disagreed
  about whether `fetch_raw` may re-crawl a failed URL; the no-re-crawl reading shipped.
- Watch-next: Phase 2 is the only unflagged phase and is self-contained (guard strip-then-rescan).
  Phase 3 is where the Phase 1 invariant can be broken — read the `## Discoveries` warning about
  placing the blocklist backstop relative to the provenance check before wiring it.

### 2026-08-20 — Phase 2: Guard strip-then-rescan
- Done: `scan` strips invisibles as its first statement, so the verdict is computed on stripped
  text; the presence-only zero-width regex left the `obfuscation` family (which keeps
  `decode and execute` and its base64 fixture). Four stale "scan must see raw text" comments
  rewritten across guard.py and fetch.py. 709 tests green, four gates clean.
- Learned: `tests/test_guard.py::test_each_family_blocks_its_attack_fixtures` globs fixtures by
  `attack_{family}_*`, so a fixture that changes which family it fires MUST be renamed —
  `attack_obfuscation_zerowidth.txt` became `attack_instruction_override_zerowidth.txt`. Three
  separate tests carried `guard=GuardSettings(enabled=False)` purely to dodge zero-width
  blocking; all three now run with the guard ON and are stronger for it.
- Drift: none.
- Watch-next: Phase 3 is the big one (~300-450 lines, 9 files, flagged !#2) and is where Phase
  1's `approve()`-clears invariant can be broken — read the `## Discoveries` entry on blocklist
  backstop placement relative to the provenance check BEFORE wiring it. Risk #2 also asks that
  challenge-marker scoping be confirmed at 3C: markers must only be checked alongside a
  refusal-shaped response, never on every fetched page.

### 2026-08-20 — Phase 3: Persistent domain blocklist
- Done: new `harness/blocklist.py` (`hostname_of`, `fires_challenge_marker`, `Blocklist`,
  `load_blocklist`, `resolve_blocklist`) persisting `{hostname: {reason, first_seen}}` via
  read-merge-`os.replace`; `[blocklist]` config section + `harness.toml`; 401 added to
  `_BLOCKED_STATUSES`; `_feed_blocklist` post-classify in both fetch batches; pre-crawl backstop
  placed AFTER the provenance check; `_drop_blocklisted` in search ahead of the guard and
  `_approve_survivors`; one shared `Blocklist` loaded once in `build_tools`. 757 tests green,
  four gates clean. Diff ran ~2.3x the plan's band (~1030 lines/18 files vs ~300-450/9) —
  ~60% of it the tests and fixtures the phase's own test list demanded, i.e. band mis-sizing,
  not scope creep.
- Learned: (a) The plan's `outcome != "fetched"` marker scoping was UNSAFE and is now
  `outcome == "blocked"` — `classify` returns `non_html` for any non-HTML content type and
  `error`/`timeout` on any error message, all of which carry real page text, so a `text/plain`
  RFC saying "just a moment" would have been walled permanently. See `## Reconciliations`.
  (b) `tests/test_tools_registry.py` had three spies pinned to the old builder arity, and one
  captured the run_log as `args[-1]`; the last now selects it by type. Neither worker caught
  this because the plan's verification scope named only five test modules — a signature change
  needs a full-suite run, not a targeted one. (c) Run-log kinds are free-form strings with no
  registry, so a new kind needs no `report.py`/`runlog.py` change.
- Drift: one `## Reconciliations` entry — R3 and the Phase 3 Contracts both said 429/503/timeouts
  never persist, but a challenge-bearing 503 is exactly R3's "detected challenge page"; a bare
  429/503/timeout still never persists. Amended and developer-approved, then tightened again
  after the judgment review (same entry).
- Watch-next: Phase 4 (flagged !#3) drops `FilesystemMiddleware` from `_reader_spec`. Risk #3
  says to verify against the INSTALLED deepagents that its summarization wrapper really grants
  no tools — architecture.md notes a nested subagent gets no auto-injected middleware, so every
  remaining entry is load-bearing. Also note Phase 3 did NOT run its manual live-check
  (`https://httpbin.org/status/403` twice); that acceptance box is still open and needs `.env`
  + SearXNG.
