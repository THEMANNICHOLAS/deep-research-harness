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
