# Decisions

Project decision log — the durable "why" behind non-obvious choices. One
entry per decision: what was decided, why, and what was rejected. 1-3
sentences each, append-only, newest last.

## Entries

- **Browser backend defaults to `playwright`, not `lightpanda`.** Smoke-tested crawl4ai
  `0.9.2` against `lightpanda/browser` `1.0.0-nightly.8578` over CDP: attachment
  succeeded, but `Page.goto` timed out after 60s waiting on `domcontentloaded`, while a
  Playwright control run on the same URL returned 200 — Lightpanda accepts the CDP
  connection and never emits the lifecycle event, consistent with its documented
  Page/Network/Runtime/DOM subset. `lightpanda` stays a declared
  `BrowserSettings.backend` value in @harness.toml; the gap and the `--advertise-host`
  trap are tracked in @docs/backlog.md.

- **mypy targets Python 3.12 although `requires-python` allows 3.11.** numpy's stubs
  (transitive via crawl4ai, reached through `pytest.approx`) use a PEP 695 `type`
  statement that is a syntax error under `python_version = "3.11"` and aborts the entire
  run. Rejected the narrower fix (a `numpy.*` `follow_imports = skip` override); the
  accepted cost is that 3.11-incompatible syntax in @harness/ is not caught by the type
  checker. See @pyproject.toml.

- **The Lightpanda/CDP backend is removed; Chromium via crawl4ai-managed Playwright is
  the only path, and no config key selects a browser.** `BrowserSettings` and the earlier
  entry's declared `lightpanda` value are gone — its smoke-test failure was never resolved
  and the branch was dead code besides. Its @docs/backlog.md pointer is now dangling, and
  re-adding a second backend means re-adding the config surface. See @harness/config.py and
  @harness/tools/fetch.py.

- **Truncation always cuts at the latest structural boundary, with no minimum-yield floor.**
  The `_MIN_BOUNDARY_FRACTION` guard (fall back to the hard cut when the boundary sat below
  60% of the cap) was removed as unmotivated complexity — it never fired in live use. A page
  whose only boundary is near the top now returns that much and says it was truncated; the
  no-boundary and boundary-at-0 cases still take the whole allowance. See
  @docs/plans/PLAN-crawler-refinement.md Reconciliation #3.

- **Fetch is HTTP-first; Chromium is an escalation, and config keys now select the path.**
  This reverses the previous entry above: crawl4ai's `AsyncHTTPCrawlerStrategy` is the
  primary extraction path (no browser launched at all for an ordinary page), and a browser
  crawler is constructed lazily only to re-fetch a page whose generated markdown reads like
  a JS shell. `arun_many` and `MemoryAdaptiveDispatcher` are gone in favour of one deadlined
  `arun()` per URL under our own semaphore and retry budget, which is what makes a hard
  per-URL deadline possible — crawl4ai exposes none on the batch API. The cost is losing the
  dispatcher's system-wide memory backpressure, so `fetch.browser_concurrency` is the only
  thing bounding browser memory. See @docs/plans/PLAN-http-first-fetch.md D1-D3 and
  @harness/tools/fetch.py.

- **A 403/401 blocklists the whole domain for 30 days, persisted as hand-editable JSON.**
  No database (the no-DB constraint), no file locking: a single small map written via
  temp-file + `os.replace`, pruned on load, last-write-wins across concurrent writers because
  a lost entry is simply re-learned on the next block. Both `load` and `record` degrade
  instead of raising — the file is regenerable, so a malformed or unwritable one must never
  fail a batch of fetches. The accepted risk is that a transient 403 locks a domain out for
  the full TTL; the cheap fix if that bites is two strikes before recording, not a shorter
  TTL. See @harness/blocklist.py and @docs/plans/PLAN-http-first-fetch.md D4.

- **`max_subagents` is a declared bound with no runtime enforcement.** There is no agent
  loop yet, so nothing counts or schedules subagents; the key exists so the loop, when
  built, has a single validated place to honor. Rejected building enforcement now — no
  caller, nothing to test against. See @docs/architecture.md `## Concurrency Bounds`.
