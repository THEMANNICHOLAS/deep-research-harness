# PLAN: Crawler Refinement

**Status:** In Progress
**Created:** 2026-08-10
**Type:** Single plan

## Intent

**True goal:** `harness/tools/fetch.py` is the only way research subagents read a web page,
and it currently relaunches Chromium on every call, carries a browser backend that has never
worked, cuts long pages mid-word, and passes one-line boilerplate through to the model. Make
it cheaper to call and cleaner in output without losing the content that explains *why* a
source reached its conclusion.

**Binding outcomes:**

- **R1** — Lightpanda/CDP is gone from every live surface: code, config, tests, and the docs a
  reader would treat as current. No configuration key selects a browser backend; Chromium is
  the only path and is not presented as a choice.
  - Historical records are exempt and stay verbatim: `docs/plans/PLAN-harness-substrate.md`
    (33 mentions) describes what was actually built, and rewriting it would falsify the
    record. Existing `docs/decisions.md` entries are likewise not edited — that log declares
    itself append-only (`docs/decisions.md:5`), so the removal is recorded by APPENDING a new
    entry, leaving the original "playwright, not lightpanda" entry intact as the reason the
    backend was doomed in the first place.
  - `docs/backlog.md`'s Lightpanda entry is live, not historical — it describes work someone
    might pick up — so it goes.
- **R2** — A page whose markdown exceeds the per-page cap ends at a heading or paragraph
  boundary rather than mid-word, and the model is told it was truncated.
  - When no boundary exists before the cap, it cuts at the cap and still discloses — a page
    with no structure must never come back empty or uncut.
  - The full untruncated markdown stays on the artifact, unchanged.
- **R3** — Short boilerplate blocks (nav residue, category-link stubs, the "Search / N
  languages" fragment) stop reaching the model, confirmed against a real fetched page rather
  than a fixture.
- ~~**R4** — Fetching pages across many tool calls does not relaunch the browser each time; the
  startup cost is paid once per run rather than once per call.~~ **Dropped 2026-08-10** — see
  D3. The crawler stays constructed and closed per call.
- ~~**R5** — A rate-limited source is retried at most once before its concurrency slot returns
  to the rest of the batch, and an exhausted retry still surfaces as `blocked`.~~
  **Amended 2026-08-11** — crawl4ai 0.9.2 has no retry to bound; see Reconciliation #1. The
  binding outcome is now: a rate-limited source holds its concurrency permit for the shortest
  backoff the library allows, and still surfaces as `blocked`.
- **R6** — Every input URL yields exactly one reported outcome, and no code path can attribute
  one page's body to another page's `[Sn]` citation marker.
- **R7** — A single `fetch_pages` call covers at most 5 URLs. The limit is stated to the model
  rather than discovered by having sources vanish, and an operator can change it without a
  code edit.
  - 5 is an engineering judgment, not a measured optimum — see D1 for the evidence and its
    limits.
  - The limit counts URLs **as submitted**, not after deduplication. D2 enforces it in the
    input schema, which necessarily runs before `_fetch` dedups — so six URLs of which two are
    duplicates is rejected, not silently collapsed to four and accepted.

**Preferences (negotiable — may be trimmed on cost grounds without re-asking):**

- The `min_word_threshold` value should be chosen by looking at a real fetched page, not picked
  in the abstract. Small (3-5) is the starting assumption.
- Boilerplate removal is tuning, not a correctness gate — a residue that survives is a backlog
  item, not a failed phase.

**Non-goals:**

- No ceiling on total output *across* calls. R7 bounds one call to 5 URLs (~15k tokens at the
  current cap); it does not and cannot bound what a loop accumulates over four calls.
  Constraining how often the tool is called is prompt/orchestration work owned by the
  research-loop session, not this tool.
  - Superseded a prior non-goal ("no batch ceiling at all"), reversed by the developer on
    2026-08-10 on context-degradation grounds.
- No memory or RAM redesign. One Chromium process serving 5 concurrent pages is acceptable on
  this hardware; the 75% dispatcher threshold stays as cheap insurance and nothing is built
  around it.
- No query-aware, relevance-scored, or LLM-based content filtering.
- No change to `classify()` or the `FetchOutcome` vocabulary. Both known gaps stay in
  @docs/backlog.md: a 404 serving an HTML body classifies `fetched`, and PDFs classify
  `error` rather than `non_html`. Decided 2026-08-10 with the cost understood — widening the
  frozen `FetchOutcome` Literal is cheapest right now, while nothing downstream consumes
  outcomes, and gets more expensive once the research loop switches on them. A future session
  picking this up should know it was deferred deliberately, not overlooked.
- No neural or learned classification anywhere in this tool. Both backlog gaps are decidable
  from the `content-type` header and `status_code`, which are already extracted
  (`_content_type` at `harness/tools/fetch.py:95-101`). The only case that would justify
  semantic judgment — soft-404s, paywalls, and consent walls, which return HTTP 200 with real
  HTML — is out of scope here, and would still be approached with heuristics rather than a
  trained model: the test suite is fully offline and deterministic, and a learned classifier
  would forfeit both properties.
- No agent loop, orchestration, or research-loop work of any kind.
- No new fetch capability — nothing here adds a tool, an outcome value, or a config key beyond
  what the outcomes above require.

**Constraints & assumptions:**

- `crawl4ai` is pinned `==0.9.2` (@pyproject.toml). Every library fact in `## Background` was
  verified against that installed version and may not hold on any other.
- Nothing in production calls `build_tools` yet — only `tests/test_tools_registry.py` does. So
  no caller breaks when the config surface or the tool schema changes here; the cost lands on
  the research-loop session whenever it starts consuming them.
- `docs/plans/PLAN-research-loop.md` is owned by a concurrent session. It must not be read or
  modified by this work.
- The test suite launches zero browsers today and must continue to. The CI runner has 1GB.
- The homelab box is driven interactively over SSH, so a failure that poisons the rest of a
  process costs a whole research run, not one page.
- **The working tree is not clean at plan start.** Three files carry uncommitted changes that
  this plan is responsible for landing, not re-deriving:
  - `harness/tools/fetch.py` — `verbose=False` on both `BrowserConfig` branches and on
    `CrawlerRunConfig`; `memory_threshold_percent=75.0` (crawl4ai's default is 90);
    `RateLimiter(max_retries=2)` on the dispatcher, with `_MEMORY_THRESHOLD_PERCENT` and
    `_RATE_LIMIT_MAX_RETRIES` as commented constants. R5 changes the retry constant to `1`.
  - `tests/test_fetch.py` — `test_dispatcher_is_memory_bounded_and_rate_limited` and
    `test_crawl4ai_logging_is_silenced_on_both_configs` added; the long
    `test_result_whose_url_differs_from_the_input_is_still_paired` renamed to
    `test_result_whose_url_diff_from_input_paired`.
  - `CLAUDE.md` — the `@uv.lock` reference de-`@`-ed, because the `@` prefix inlined the whole
    ~559k-token lockfile into every session's context. Unrelated to the crawler; it should be
    committed separately and first.
  - All four quality gates were green on this state (98 passed).

**Open questions:**

- ~~Classifier scope~~ — settled 2026-08-10: frozen. See the classifier entries in the
  **Non-goals** list above.
- ~~Browser lifecycle shape~~ — settled 2026-08-10: stays per-call, R4 dropped. See D3.
- ~~Whether `docs/plans/PLAN-harness-substrate.md` is scrubbed~~ — settled 2026-08-10: left
  untouched. It is a completed plan describing what was actually built, and rewriting it would
  falsify the record. Its 33 Lightpanda mentions are historical, not live references.

None remaining.
- ~~How R7's limit is enforced~~ — settled, see D2.

## Background

Verified against the installed crawl4ai 0.9.2 during planning; no `## Design Decisions` entry
owns these yet.

- `CrawlerRunConfig.word_count_threshold` is inert: it is passed to
  `LXMLWebScrapingStrategy._scrap` and never read, with `remove_empty_elements_fast` called on
  a hardcoded `1` (`content_scraping_strategy.py:877`). We never set it, so nothing needs
  removing — the working equivalent is `PruningContentFilter(min_word_threshold=N)`, which
  forces a sub-threshold block's score to `-1.0` and guarantees removal
  (`content_filter_strategy.py:757-764`). It is a hard kill, not a weight.
- No chunking strategy can shape `result.markdown`: chunking runs only inside the
  `if config.extraction_strategy` branch (`async_webcrawler.py:922-927`) and feeds
  `extracted_content`, while markdown is generated earlier at line 872.
  `MarkdownGenerationResult` exposes five flat strings and no heading tree or block scores
  (`models.py:120-128`), so boundary detection must parse the markdown text itself.
- `max_concurrency` becomes the dispatcher's `max_session_permit` and bounds concurrent page
  crawls, not browser processes: in default mode `default_context = self.browser` and pages
  are created per crawl (`browser_manager.py:940,1542`). One `_fetch` call is therefore ONE
  Chromium process serving up to 5 concurrent pages — not five processes.
- `BrowserConfig.browser_mode="cdp"` never matches: `__post_init__` tests for `"custom"`
  (`async_configs.py:920`), so our current Lightpanda branch works only incidentally because
  `cdp_url` is set. The CDP attach is hardcoded to `playwright.chromium.connect_over_cdp`
  regardless of `browser_type` (`browser_manager.py:891`).

## Codebase Map

- Entry points: `harness/tools/fetch.py` — the `fetch_pages` tool; `build_fetch_tool` is its
  factory, `_fetch` its implementation.
- Module boundaries: `harness/config.py` holds all TOML-backed settings models;
  `harness/tools/` holds one module per tool plus `__init__.py`'s `build_tools`.
- Reuse targets:
  - `harness/config.py:61-69` `BrowserSettings` — `backend: Literal["lightpanda","playwright"]`,
    `cdp_url`, and a validator requiring `cdp_url` for lightpanda. The removal target for R1.
  - `harness/config.py:72-77` `FetchSettings` — `page_timeout_ms=15000`, `max_concurrency=5`,
    `per_page_char_cap=12000`.
  - `harness/tools/fetch.py:45-53` `build_browser_config` — collapses to a constant once R1
    lands.
  - `harness/tools/fetch.py:155-171` `_render` — owns the current head-slice truncation at
    lines 166-169; the R2 target.
  - `harness/tools/fetch.py:122-152` `_pair` — the R6 target; its positional second pass is at
    lines 144-151.
  - `harness/tools/fetch.py:206-207` — the crawler is constructed and closed inside `_fetch`;
    the R4 target.
- Comparable prior art: `harness/tools/search.py` — same failure-as-data shape (typed failure
  model, `_render` for model-facing text, `build_*_tool` factory closing over config). Mirror
  its structure for anything new.
- Tests: `pytest` with `asyncio_mode = "auto"`, in `tests/`. `tests/conftest.py:15-47` holds
  `make_config`; `tests/test_fetch.py:14-79` holds the `_FakeMarkdown` / `_FakeResult` /
  `_make_fake_crawler_class` fakes and the opt-in `install_crawler` fixture that monkeypatches
  `harness.tools.fetch.AsyncWebCrawler`. No test anywhere constructs a real `AsyncWebCrawler`.
- Known test impact of R1: `tests/test_config.py:149`
  `test_lightpanda_backend_without_cdp_url_raises_config_error` and `tests/test_fetch.py:327`
  `test_build_browser_config_maps_backend_to_browser_mode` both die; `tests/conftest.py:38`
  `make_config` needs its `cdp_url` argument dropped. `tests/test_config.py:139`
  `test_unknown_browser_backend_raises_config_error` survives only if a `backend` field
  survives.
- Known test impact of R2: `tests/test_fetch.py:173`
  `test_content_is_truncated_at_the_cap_but_artifact_keeps_full_text` drives 500 characters
  against a cap of 50 and asserts the cap number appears in the rendered text and the artifact
  keeps the full 500.
- Callers: `build_fetch_tool` ← `harness/tools/__init__.py:14` and `tests/test_fetch.py:301`.
  `build_tools` ← `tests/test_tools_registry.py` only. Nothing calls any close/shutdown on a
  built tool anywhere in the repo.
- Commands: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy .`.

## Non-Goals

Inherits every `## Intent` non-goal — not re-listed.

## Design Decisions

### D1: How many URLs one `fetch_pages` call may cover
- **Chosen:** 5, operator-adjustable via config. Enforcement is D2.
- **Rejected:** unlimited — the original position. Reversed on evidence that per-source
  accuracy degrades as sources accumulate in one context.
- **Evidence, with its limits:** "Lost in the Middle" (Liu et al., TACL) finds a U-shaped
  accuracy curve over retrieved documents, ~15-20 points of swing attributable to position
  alone at 20 documents; distractor studies show multi-document QA degrading even when all
  relevant content is present. Both use ~20 SHORT documents, so they establish the direction,
  not that 5 beats 8. No study was found supporting the converse claim that one source per
  agent yields shallow output; the nearest evidence is Anthropic's multi-agent research system
  (3-5 parallel subagents, ~90% over single-agent), which is about agents having separate
  context windows, not sources per agent. **5 is judgment, not a measured optimum.**
- **Consequences:** at `per_page_char_cap=12000`, a full call is ~60k characters (~15k tokens)
  — comfortably inside where degradation bites, which makes this cheap insurance rather than
  the primary lever. `per_page_char_cap` remains the bigger knob. The cap bounds one call and
  cannot bound a loop that calls four times; that constraint belongs to the research-loop
  session.

### D2: How R7's limit is enforced
- **Chosen:** both layers. The limit is stated in prose where the model actually reads it —
  the `fetch_pages` docstring and the `urls` field description — AND enforced as a
  config-driven `max_length` on the input model. `FetchPagesInput` moves inside
  `build_fetch_tool` so it can close over config, exactly as `build_search_tool` already does
  for `SearchWebInput` (`harness/tools/search.py:121-131`). Nothing outside `fetch.py`
  references `FetchPagesInput`, so the move has no import blast radius.
- **Rejected:** runtime truncate-and-disclose — never errors, but the model learns the rule
  only after spending a call, and it has to reason about a partial result it did not ask for.
- **Rejected:** schema constraint alone — `max_length` reaches the model as JSON Schema
  `maxItems`, but models do not reliably honor schema constraints. The prose is what prevents
  the overshoot; the schema is what catches it when prevention fails. The two are not
  redundant, they cover different failure modes.
- **Consequences:** an over-limit call fails validation before any fetch happens, surfacing as
  a recoverable tool message rather than an exception escaping the call. This is the single
  place `fetch_pages` returns an error instead of data — a deliberate exception to the
  failure-as-data design, accepted because it costs a turn only when prose guidance has
  already failed. The limit is now written in three places (config default, docstring, field
  description); the config value is authoritative and the prose must reference the number
  without contradicting it.

### D3: Browser lifecycle stays per-call
- **Chosen:** leave `harness/tools/fetch.py:206-207` alone — the `AsyncWebCrawler` is still
  constructed and closed inside `_fetch` on every call, relaunching Chromium each time. R4 is
  dropped.
- **Rejected:** one long-lived crawler started once and reused. It IS supportable in 0.9.2 —
  `start()` has no one-shot flag (`async_webcrawler.py:176-186`) and `arun_many` builds a
  fresh dispatcher per call (`async_webcrawler.py:1054-1066`) — and it would also collapse
  overlapping tool calls from N browser processes to one. It lost on failure mode, not
  feasibility: there is no crash detection anywhere in `browser_manager.py`, so a dead
  Chromium leaves a stale handle and every subsequent call fails, and `close()` never resets
  `ready` (`async_webcrawler.py:188-197`). Today a browser crash costs one tool call; reused,
  it would cost the rest of the process — on a box driven interactively over SSH, a whole
  research run.
- **Rejected:** reuse plus restart-on-failure, which fixes that but adds health-check and
  retry machinery this tool does not otherwise need, for a latency win nobody has measured.
- **Consequences:** per-call Chromium startup remains an unmeasured cost, paid on every
  `fetch_pages` call. Revisiting requires a real measurement on the homelab box, not this
  workstation. Note the interaction with R1: the CDP/`use_managed_browser` path is the one
  that is NOT restart-safe (`managed_browser` is built once in `__init__` and never rebuilt,
  so `close()` then `start()` raises `AttributeError` —
  `browser_manager.py:769,885,2085-2092`), so removing Lightpanda leaves the codebase in the
  state where reuse would be viable if it is ever wanted.

## Requirements Coverage

| ID | Outcome | Covered by |
|----|---------|------------|
| R1 | Lightpanda removed | Phase 2 (`grep -ri lightpanda` hits only historical records) |
| R2 | Boundary-aware truncation | Phase 4 (cut lands on a heading/paragraph break; hard-cut fallback) |
| R3 | Short-block boilerplate removed | Phase 5 (threshold reaches the filter; live before/after check) |
| R5 | ~~Rate-limited source retries once~~ Rate-limit backoff bounded, still `blocked` | Phase 1 (`max_retries=1` caps per-domain backoff growth — see Reconciliation #1) |
| R6 | One outcome per URL, no misattribution | Phase 1 (unmatched URL reports `error`, never borrows a body) |
| R7 | At most 5 URLs per call | Phase 3 (over-limit call rejected before any fetch) |

## Progress

- [x] Phase 1: Baseline — land existing work, retry policy, pairing
- [x] Phase 2: Remove Lightpanda
- [x] Phase 3: Cap a call at five URLs
- [ ] Phase 4: Boundary-aware truncation
- [ ] Phase 5: Prune short boilerplate blocks
- [ ] Final verification

## Phases

### Phase 1: Baseline — land existing work, retry policy, pairing
**Risk:** flagged (!#1)
**Test-first:** required
**Goal:** Land the uncommitted hardening already in the tree, drop the rate-limit retry to one,
and delete `_pair`'s unreachable positional fallback.
**Requirements:** R5, R6
**Assumes:**
- The three modified files described in `## Intent` constraints are still present and the
  suite is green on them. If the tree has changed, STOP and re-check rather than improvising.
**Files:**
- `CLAUDE.md` — modify; the `@uv.lock` de-referencing. Commit FIRST and ALONE — unrelated to
  the crawler, and every session in every worktree pays for it until it lands.
- `harness/tools/fetch.py` — modify; `_RATE_LIMIT_MAX_RETRIES` 2 → 1, and delete the
  positional-fallback block in `_pair` (lines 144-151).
- `tests/test_fetch.py` — modify; retry assertion, and replace the fallback's only coverage.
**Diff budget:** ~25-45 lines across 3 files

**Reuse:**
- Extend `_pair` and `_RATE_LIMIT_MAX_RETRIES` in `harness/tools/fetch.py` — do NOT add a
  pairing helper or a retry-policy module.
- Pattern to mirror: `test_dispatcher_is_memory_bounded_and_rate_limited` in
  `tests/test_fetch.py` — assert against the object handed to crawl4ai, with the reason for
  the number in a comment.

**Contracts:**
- `_RATE_LIMIT_MAX_RETRIES = 1` — ~~one retry, two attempts total.~~ caps per-domain backoff
  growth at one doubling; there is no re-crawl in 0.9.2 (Reconciliation #1). Later phases must
  not change the value.
- `_pair(urls, results) -> list[tuple[str, object | None]]` — signature unchanged; an input URL
  with no exact-URL match now pairs with `None` rather than borrowing an unclaimed result.

**Out of scope:**
- The crawler lifecycle — it stays per-call (D3).
- `classify()` and `FetchOutcome` (Intent non-goal).
- The truncation logic in `_render` — that is Phase 4.
- The `verbose` and memory-threshold values already in the tree; land them as they are.

**Tests (write first, confirm red):**
- [x] The dispatcher is configured for exactly one rate-limit retry. (Red on `assert 2 == 1`;
  the assertion's *meaning* was corrected per Reconciliation #1 — it pins a backoff cap, not a
  retry count.)
- [x] An input URL with no matching result yields exactly one `error` outcome and an empty
  body — the `None` branch survives the fallback's removal. (Green before implementation by
  design; a survival guard for a branch that was previously untested.)
- [x] A result carrying a URL that matches no input never supplies a body to an unrelated
  input URL. (Red on `assert 'fetched' == 'error'`.) **This replaces `test_result_whose_url_diff_from_input_paired`, which asserted
  the opposite and is the fallback's only coverage — it must be deleted, not weakened.**

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Commit the `CLAUDE.md` fix on its own, before touching any code.
3. Flip `_RATE_LIMIT_MAX_RETRIES` to `1`; delete `_pair`'s positional-fallback block and the
   now-dead `claimed_ids`/`leftovers` bookkeeping.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `git log --oneline` shows the CLAUDE.md commit strictly before the fetch commit. (Both
  commits made at the 3G gate in that order — the commit guard only opens the commit window
  after 3E is green, so the plan's "commit before touching any code" ordering was satisfied as
  two ordered commits from one dirty tree. `CLAUDE.md` was never touched by the code work, so
  the diffs stayed unentangled.)
- [x] `_pair` contains no iteration over unclaimed results.

### Phase 2: Remove Lightpanda
**Risk:** flagged (!#2)
**Test-first:** required
**Goal:** Delete the Lightpanda/CDP backend from every live surface, leaving Chromium as the
only path and no configuration key that selects a browser.
**Requirements:** R1
**Files:**
- `harness/config.py` — delete `BrowserSettings`, its validator, and `HarnessConfig`'s
  `browser` field.
- `harness.toml` — delete the `[browser]` table.
- `harness/tools/fetch.py` — delete `build_browser_config`; construct `BrowserConfig(verbose=False)`
  directly at its one call site.
- `tests/conftest.py` — `make_config` stops building browser settings.
- `tests/test_config.py` — delete the two backend tests; drop `backend` from the TOML fixtures.
- `tests/test_fetch.py` — delete `test_build_browser_config_maps_backend_to_browser_mode`;
  keep the `verbose` assertions, retargeted at the inline config.
- `CLAUDE.md`, `docs/INDEX.md`, `docs/guides/setup.md`, `docs/backlog.md` — remove live
  references, including backlog.md's Lightpanda entry in full.
- `docs/decisions.md` — APPEND one entry recording the removal. Edit no existing entry.
**Diff budget:** ~90-150 lines across 10 files, overwhelmingly deletions

**Reuse:**
- none — new surface. This phase deletes. The one addition, the `docs/decisions.md` entry,
  mirrors the shape of the entries already in that file (what was decided, why, what was
  rejected, 1-3 sentences).

**Contracts:**
- `HarnessConfig` has no `browser` attribute.
- `harness.toml` has no `[browser]` table.
- `build_browser_config` no longer exists in `harness/tools/fetch.py`.

**Out of scope:**
- `docs/plans/PLAN-harness-substrate.md` and every pre-existing `docs/decisions.md` entry —
  historical records, exempt per R1. Do not rewrite them.
- `FetchSettings` — untouched here; Phase 3 owns it.
- Truncation, pruning, and the retry constant.

**Tests (write first, confirm red):**
- [x] The shipped `harness.toml` loads with no browser section present.
  (`test_shipped_harness_toml_has_no_browser_surface`; red on `assert not True`.)
- [x] A config file that still contains a `[browser]` table is rejected — the strict models
  forbid extras, so this proves the key is genuinely gone rather than merely unused.
  (`test_browser_table_is_rejected_now_that_the_backend_is_gone`; red on DID NOT RAISE.)
- [x] The `BrowserConfig` handed to crawl4ai for a fetch still has `verbose` false. (Covered by
  the existing `test_crawl4ai_logging_is_silenced_on_both_configs`, retargeted at the inline
  config as the plan directs — green throughout, no new test written.)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Delete the config surface, then the `fetch.py` branch, then update the test fixtures.
3. Strip live references from the four docs; append the `docs/decisions.md` entry.
4. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] `grep -ri lightpanda .` returns hits ONLY in `docs/plans/PLAN-harness-substrate.md`,
  pre-existing `docs/decisions.md` entries, and this plan. (Verified. Two hits the criterion's
  wording did not anticipate, both correct: the NEW `docs/decisions.md` entry names Lightpanda
  because R1 mandates recording the removal there, and `docs/plans/.impl/` scratch files are
  gitignored and deleted at session end. Zero hits in `harness/`, `tests/`, `harness.toml`,
  `CLAUDE.md`, `docs/INDEX.md`, `docs/guides/setup.md`, `docs/backlog.md`.)
- [x] `docs/decisions.md` has one new trailing entry and no modified existing entry
  (`git diff` shows additions only in that file). (`git diff --numstat` → `13 0`.)
- [x] Beyond the plan's file list: `.env.example` also advertised the removed key and was
  fixed — see the 2026-08-12 `## Discoveries` entry. The `grep` criterion could not catch it
  because the line said "CDP", not "Lightpanda".

### Phase 3: Cap a call at five URLs
**Risk:** flagged (!#3)
**Test-first:** required
**Goal:** Limit one `fetch_pages` call to five URLs — stated to the model in prose, enforced in
the input schema, and adjustable without a code edit.
**Requirements:** R7
**Files:**
- `harness/config.py` — add `max_urls_per_call` to `FetchSettings`, default 5.
- `harness.toml` — add the key under `[fetch]`.
- `harness/tools/fetch.py` — move `FetchPagesInput` inside `build_fetch_tool` so it closes over
  config; `max_length` from the setting; state the limit in the `fetch_pages` docstring and in
  the `urls` field description.
- `tests/test_fetch.py` — limit behaviour and schema-follows-config.
- `tests/test_config.py` — the new setting loads and defaults.
**Diff budget:** ~50-80 lines across 5 files

**Reuse:**
- Pattern to mirror: `build_search_tool` in `harness/tools/search.py:118-145` — it already
  defines its input model inside the factory precisely so a config value can reach the schema.
  Follow it exactly; do NOT add a validation helper or a decorator.
- Extend `FetchSettings` in `harness/config.py:72-77` — do NOT create a new settings model.

**Contracts:**
- `FetchSettings.max_urls_per_call: int` — default `5`, must be > 0.
- `harness.toml` key `[fetch] max_urls_per_call`.
- The `fetch_pages` args schema carries a `maxItems` equal to that setting.

**Out of scope:**
- Any cross-call or total-output ceiling (Intent non-goal) — this bounds one call only.
- `max_concurrency` and `per_page_char_cap` — unchanged.
- Anything that constrains how OFTEN the tool is called; that is the research-loop session's.

**Tests (write first, confirm red):**
- [x] A call carrying more than the configured number of URLs is rejected before any fetch
  is attempted. (Red on `DID NOT RAISE ValidationError`; the assertion was then reshaped by the
  judgment review to expect an error `ToolMessage` — see the 2026-08-12 `## Discoveries` entry.)
- [x] A call at exactly the limit succeeds and fetches every URL. (Green before implementation
  by design — a survival guard proving the cap does not break the at-limit case.)
- [x] The limit in the tool's schema follows the config value rather than a literal — build a
  tool from a config with a different limit and assert the schema moved. (Red on
  `KeyError: 'maxItems'`.)
- [x] Both the tool description and the `urls` field description state the limit. (Red on
  `assert '5' in ...`.)

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Add the setting and its `harness.toml` key; move `FetchPagesInput` into the factory and
   wire `max_length`; write the limit into both prose surfaces.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [x] The number appears in the docstring and field description as text consistent with the
  config default — config remains authoritative and the prose does not contradict it. (Neither
  prose surface hardcodes the number: the `urls` field description interpolates
  `config.fetch.max_urls_per_call`, and because a literal docstring cannot interpolate a config
  value, one interpolated sentence is appended to `fetch_pages.description` after the `@tool`
  decorator. `test_both_prose_surfaces_state_the_url_limit` derives its expected number from
  the config, so it fails if either surface ever goes stale.)
- [x] Beyond the plan's Steps: `fetch_pages.handle_validation_error` was wired so the rejection
  is a recoverable tool message, which D2's Consequences and risk #3 both assert but no step
  required — see the 2026-08-12 `## Discoveries` entry.

### Phase 4: Boundary-aware truncation
**Risk:** flagged (!#4)
**Test-first:** required
**Goal:** An over-cap page ends at a heading or paragraph break rather than mid-word, falls
back to a hard cut when no usable boundary exists, and always discloses that it was truncated.
**Requirements:** R2
**Files:**
- `harness/tools/fetch.py` — replace the head-slice at lines 166-169 inside `_render`.
- `tests/test_fetch.py` — boundary cases.
**Diff budget:** ~50-80 lines across 2 files

**Reuse:**
- Extend `_render` in `harness/tools/fetch.py:155-171` — do NOT add a module, a class, or a
  markdown-parsing dependency. `MarkdownGenerationResult` exposes flat strings only (see
  `## Background`), so this is string work on `#` headings and blank-line breaks.
- Pattern to mirror: `harness/tools/search.py:72-82` `_render` — one private render helper per
  tool, model-facing text assembled in one place.

**Contracts:**
- The rendered block still contains the configured cap number and a statement that content was
  omitted. Exact wording is free; the number and the disclosure are not.
- `FetchedPage.markdown` on the artifact remains the full untruncated text.

**Out of scope:**
- Changing `per_page_char_cap`'s default value.
- Any whole-batch or cross-page budget.
- Reordering, summarising, or selecting content by relevance — cut only.

**Tests (write first, confirm red):**
- [ ] Text at or under the cap is returned unchanged, with no truncation notice.
- [ ] Over-cap text ends at the latest heading or paragraph break at or before the cap.
- [ ] Text with no boundary before the cap falls back to a hard cut and still discloses.
- [ ] A boundary so early that it would discard most of the allowance is rejected in favour of
  the hard cut — a structured page must never come back nearly empty.
- [ ] The artifact keeps the full untruncated markdown while the rendered content is shorter.

**Steps:**
1. Write the tests above; run them; confirm they FAIL (red).
2. Implement the cut, naming the "boundary too early to be worth taking" floor as a module
   constant with its rationale in a comment, alongside the existing tuning constants.
3. Run the tests; confirm they PASS (green).

**Acceptance criteria:**
- [ ] `test_content_is_truncated_at_the_cap_but_artifact_keeps_full_text` still passes, or is
  deliberately updated with the reason recorded — it drives 500 characters at a cap of 50 and
  is the existing contract for this behaviour.

### Phase 5: Prune short boilerplate blocks
**Risk:** flagged (!#5)
**Test-first:** required
**Goal:** Stop short boilerplate blocks reaching the model, with the threshold chosen against a
real fetched page rather than in the abstract.
**Requirements:** R3
**Files:**
- `harness/tools/fetch.py` — pass `min_word_threshold` to `PruningContentFilter` at line 195,
  with a named constant carrying its rationale.
- `tests/test_fetch.py` — wiring assertion.
- `docs/backlog.md` — update or close the residual-boilerplate entry with what actually
  survived.
**Diff budget:** ~25-45 lines across 3 files

**Reuse:**
- Extend the existing `DefaultMarkdownGenerator(content_filter=PruningContentFilter())` call —
  do NOT introduce a second filter or a custom filter subclass.
- Pattern to mirror: `_MEMORY_THRESHOLD_PERCENT` in `harness/tools/fetch.py:34-37` — a module
  constant whose comment states why the number is not the library default.

**Out of scope:**
- `PruningContentFilter`'s `threshold`, which stays at the library default `0.48`. Raising it
  discards the context and caveats that explain why a source concluded what it did — an
  explicit developer ruling, not an oversight.
- `_EXCLUDED_TAGS`.
- `CrawlerRunConfig.word_count_threshold`, which is inert in 0.9.2 (see `## Background`). Do
  not set it "for completeness" — it would read as a working control.

**Tests (write first, confirm red):**
- [ ] The configured `min_word_threshold` reaches the `PruningContentFilter` handed to the
  markdown generator.

**Steps:**
1. Write the test above; run it; confirm it FAILS (red).
2. Add the constant and wire it through.
3. Run the test; confirm it PASSES (green).

**Acceptance criteria:**
- [ ] A real page is fetched before and after the change and the two outputs compared: the
  "Search / N languages" fragment and category-link stubs are gone, AND no substantive short
  line disappeared — specifically check a one-sentence finding, a table row, and a line of
  code. This is a live network check and cannot be satisfied by a fixture.
- [ ] `docs/backlog.md`'s residual-boilerplate entry records what survived, or is removed if
  nothing did.

## Verification

- [ ] `uv run pytest` — full suite green, and still launching zero browsers.
- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] `grep -ri lightpanda .` returns only the historical records named in Phase 2.
- [ ] A live end-to-end fetch of several real URLs, confirming the truncation notice appears
  on a long page, a short page is untouched, and each page carries its own `[Sn]` marker.

## Notes

- Phase order is deliberate: Phase 2 deletes the most code, so running it early means Phases
  3-5 edit a simpler `fetch.py`. Phase 5 is last because it is the only phase whose acceptance
  needs the network.
- The `_pair` change in Phase 1 and the cap in Phase 3 both alter behaviour the concurrent
  research-loop session may be building against. Neither has a production caller today
  (`build_tools` is invoked only by `tests/test_tools_registry.py`), but that session should be
  told when Phases 1 and 3 land.
- `per_page_char_cap` (12000, ~15k tokens across a full 5-URL call) is a bigger lever on
  context quality than the URL cap. It is deliberately unchanged here; revisit with real pages.

## Risks

#1. **Removing `_pair`'s fallback assumes crawl4ai always keys results by the requested URL.**
    Verified for the redirect case — `AsyncWebCrawler.arun` keeps the requested URL on
    `result.url` and puts the destination on `redirected_url` — but not proven exhaustively for
    every crawl path. If the assumption is ever wrong, the affected URL reports `error` with an
    empty body. That is the correct failure: the fallback's own failure mode was attributing
    one page's body to another page's `[Sn]` marker, which is silent and unfixable downstream.
    A visible `error` is strictly safer than a plausible wrong citation. Confirm during Phase 1
    that the deleted test is the ONLY coverage of the fallback.

#2. **Phase 2 deletes config surface while another session is actively working in this repo.**
    `HarnessConfig.browser` disappears. Nothing in production reads it today, but the
    research-loop session may be writing code against the config object right now. Coordinate
    before landing, and expect a merge conflict in `harness/config.py` and `tests/conftest.py`
    rather than a clean rebase.

#3. **Phase 3 changes the tool's public schema.** `fetch_pages` starts rejecting calls it used
    to accept. Any prompt, fixture, or loop code written against an unlimited `urls` list
    breaks. The rejection is a recoverable tool message rather than an escaping exception, so
    the loop can retry — but a prompt that hands over ten URLs will now fail every time until
    it is updated.

#4. **Boundary truncation can discard far more than the cap requires.** A page whose last
    heading or paragraph break sits well before the cap loses everything after it. The
    "boundary too early" floor in Phase 4 exists to bound this, but the floor is a guess until
    it meets real pages. Watch for a fetched page coming back conspicuously shorter than the
    cap allows.

#5. **`min_word_threshold` is a hard kill, not a weight.** A block under the threshold is
    forced to score `-1.0` and removed regardless of how relevant it is
    (`content_filter_strategy.py:757-764`). Short but meaningful content — a one-sentence
    finding, a table row, a code line, a figure caption — is exactly what this deletes. Keep
    the number small (3-5) and treat Phase 5's live before/after comparison as the real gate,
    not the wiring test.

## Reconciliations

<!-- Drift amendments written by /implement during execution. Append-only. Outdated phase
text above is struck through (~~...~~) but preserved; entries here are the authoritative
correction. Empty at plan creation. -->

### #1 — 2026-08-11 — Phase 1: `RateLimiter.max_retries` does not retry anything

**Contradiction.** R5, the Phase 1 contract, and the constant's own comment all assert that
`RateLimiter(max_retries=N)` bounds re-fetch attempts on a 429/503. It does not. In crawl4ai
0.9.2, `update_delay` (`async_dispatcher.py:65-85`) runs *after* the crawl has already
returned (called at line 328) and its `False` return only writes a monitor error message —
nothing re-crawls the URL. `max_retries` is a **per-domain fail budget bounding how many times
that domain's backoff delay doubles**. The only re-queue in the dispatcher is for critical
memory pressure (lines 289-293), unrelated to rate limiting. Surfaced by the Phase 1 judgment
review and verified against the installed library.

**Amendment.** The value stays `1` — the code needs no change. What it *buys* is restated
honestly: with 2+ URLs from one domain in a batch, a 429 grows that domain's delay at most
once, and the sleep happens while holding one of `max_concurrency`'s permits
(`async_dispatcher.py:283-286`), so a lower cap returns the permit to the batch sooner. With
one URL per domain — the normal case — the setting has no observable effect. R5's surviving
outcome is the amended wording in `## Intent`: shortest available backoff, and a 429 still
classifies `blocked` (via `classify`'s `_BLOCKED_STATUSES`, which is untouched and independently
tested).

**Rejected:** building real retry machinery in `_fetch` — new machinery for an unmeasured
latency win, against the Intent non-goal of adding no new fetch capability. **Rejected:**
dropping `RateLimiter` entirely — it still supplies the inter-request politeness delay and the
429 backoff; only its retry semantics were misdescribed.

## Discoveries

<!-- Non-contradictory findings logged by /implement during execution (act / defer / drop).
Append-only, empty at plan creation. -->

- **2026-08-12 — Phase 3: nothing turned D2's promised "recoverable tool message" into one.**
  A pydantic `max_length` violation raises `ValidationError` straight out of `ainvoke`, so the
  mitigation D2's Consequences and risk #3 both rely on — the loop can retry — did not exist;
  the first implementation's test comment called the conversion the future agent loop's job.
  It is not: `BaseTool.handle_validation_error` is in the installed langchain-core and returns
  an error-status `ToolMessage`. → Surfaced by the Phase 3 judgment review; FIXED in-phase with
  a `_explain_validation_error` callable rather than a fixed string, because the hook swallows
  every validation failure for the tool and a fixed over-limit string would misreport a
  wrong-type call as a too-long list. Two tests cover it (over-limit reports the cap;
  a malformed call reports itself). Also from the same review: the over-limit test gained an
  assertion on the pydantic cap message so an unrelated validation failure cannot satisfy it,
  and R7's "duplicates still count toward the limit" sub-bullet gained the test it lacked.

- **2026-08-12 — Phase 2: `.env.example` still advertises a browser backend key that no longer
  exists.** Its header says non-secret endpoints "(SearXNG URL, browser backend/CDP URL, model
  IDs) live in harness.toml", and `docs/guides/setup.md:30` tells the reader to copy that file —
  so it is a live surface R1 covers, missed by the phase's file list and invisible to the
  `grep -ri lightpanda` criterion because the line says "CDP", not "Lightpanda". → Surfaced by
  the Phase 2 judgment review; suggested action: drop the phrase from the comment as part of
  Phase 2 (one line, same requirement).

- **2026-08-11 — Phase 1 — DEFERRED: the one-page fake-result setup repeats four times in
  `tests/test_fetch.py`.** `make_config()` + `SourceRegistry()` +
  `_FakeResult("https://a.test", markdown=_FakeMarkdown("a", "a"))` now appears at four call
  sites (lines ~222, 241, 260, 275; two of them landed as this phase's pre-existing work),
  which is past CLAUDE.md's "if the same lines are about to appear a third time, factor them
  out" rule. Deferred by the developer rather than fixed in-phase: Phases 3-5 all add tests to
  this file, so the right shape of the fixture is clearer once they land. To address: a module-
  level fixture in `tests/test_fetch.py` (not `conftest.py` — the fakes are fetch-specific).

## Phase Handoff Log

<!-- Written by /implement at each 3G phase gate (Done / Learned / Drift / Watch-next per
phase). Append-only, empty at plan creation. -->

### 2026-08-11 — Phase 1: Baseline — land existing work, retry policy, pairing
- Done: Landed the pre-existing uncommitted hardening (`verbose=False` on both configs,
  `memory_threshold_percent=75.0`, the `RateLimiter`), dropped `_RATE_LIMIT_MAX_RETRIES` to 1,
  and deleted `_pair`'s positional fallback so an unmatched input URL reports `error` instead of
  borrowing an unrelated page's body. `CLAUDE.md`'s `@uv.lock` de-referencing committed first
  and alone. Suite 99 passed (was 98: one test deleted, two added); all four gates green.
- Learned: `RateLimiter.max_retries` does not retry anything in crawl4ai 0.9.2 — see
  Reconciliation #1; real retry-on-429 is now a `docs/backlog.md` entry. Risk #1's audit
  question is answered: the deleted `test_result_whose_url_diff_from_input_paired` was the
  fallback's only coverage, and `arun`/`CrawlResult` do keep the requested URL on `result.url`
  (verified in the installed library, `async_webcrawler.py:475-489,751-757`), so exact-URL
  pairing is sound on the live path.
- Drift: Reconciliation #1 (R5's retry semantics). Approved by the developer with a backlog
  entry for genuine retry.
- Watch-next: Phase 2 deletes `HarnessConfig.browser` — risk #2 says coordinate with the
  concurrent research-loop session first and expect a conflict in `harness/config.py` /
  `tests/conftest.py` rather than a clean rebase. Also note `docs/plans/PLAN-crawler-refinement.html`
  is still untracked and deliberately uncommitted.

### 2026-08-12 — Phase 2: Remove Lightpanda
- Done: Deleted `BrowserSettings` + `HarnessConfig.browser`, the `[browser]` TOML table, and
  `build_browser_config` (its call site now inlines `BrowserConfig(verbose=False)`); dropped the
  dead tests and the `BrowserSettings` fixture wiring; stripped live references from `CLAUDE.md`,
  `docs/INDEX.md`, `docs/guides/setup.md` (env var, `[browser]` bullet, Lightpanda Docker
  section) and deleted `docs/backlog.md`'s Lightpanda entry; appended one `docs/decisions.md`
  entry. 50 insertions / 119 deletions across 12 files. Suite 97 passed; all four gates green.
- Learned: `.env.example` was a live surface the plan's file list missed and its
  `grep -ri lightpanda` criterion structurally could not catch (the line said "CDP") — fixed in
  this phase, logged under `## Discoveries`. Risk #2's blast radius was verified as zero on this
  tree: nothing anywhere reads `config.browser`, and `harness/config.py` / `tests/conftest.py`
  were both clean at phase start, so no live conflict occurred. The old decisions entry's
  `@docs/backlog.md` pointer went dangling when that entry was deleted; the new entry now says
  so explicitly, since the earlier one may not be edited.
- Drift: none.
- Watch-next: **Risk #2 is not fully discharged — coordination is still owed before this branch
  merges.** The commits are local; another session writing against `HarnessConfig` will only
  discover the deletion at merge. Phase 3 next: it adds `FetchSettings.max_urls_per_call` and
  moves `FetchPagesInput` inside `build_fetch_tool`, which changes the tool's public schema
  (risk #3) — the same session should be told when it lands.

### 2026-08-12 — Phase 3: Cap a call at five URLs
- Done: Added `FetchSettings.max_urls_per_call` (default 5, `gt=0`) and its `harness.toml` key;
  moved `FetchPagesInput` inside `build_fetch_tool` so `max_length` — and therefore the schema's
  `maxItems` — comes from config; stated the limit in the `urls` field description and in an
  interpolated sentence appended to `fetch_pages.description`; wired
  `handle_validation_error` so an over-limit call returns an error `ToolMessage` instead of
  raising. 7 new tests in `tests/test_fetch.py`, plus the config load/default/reject cases.
  Suite 104 passed (was 97); all four gates green.
- Learned: a literal docstring cannot interpolate a config value, so the model-facing limit is
  appended to `BaseTool.description` after the decorator — `search.py`'s pattern was mirrored
  for the closure but not for its prose, which deliberately omits its number. `mypy` requires
  the `handle_validation_error` callable to accept pydantic v1 OR v2 errors, hence its `object`
  parameter. Risk #3 is now genuinely mitigated rather than merely asserted, but the mitigation
  is per-tool: any future tool with a bounded schema needs the same hook.
- Drift: none. The judgment review's Major was a gap against D2, not a contradiction of it —
  fixed in-phase and logged under `## Discoveries`.
- Watch-next: **Risk #2 AND #3 coordination is still owed before this branch merges** — the
  concurrent research-loop session needs telling that `HarnessConfig.browser` is gone and that
  `fetch_pages` now rejects more than 5 URLs. Phase 4 next (boundary-aware truncation in
  `_render`); its risk #4 warns the "boundary too early" floor is a guess until it meets real
  pages. Phase 5's acceptance and the plan's final verification both need the homelab box over
  SSH — they cannot be completed on this workstation.

