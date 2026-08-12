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
  the only path, and no config key selects a browser.** The declared `lightpanda`
  `BrowserSettings.backend` value and the whole `BrowserSettings` model are now gone,
  superseding the earlier entry's statement that it stays declared. The smoke-test
  failure that entry recorded was never resolved, and the branch was dead code besides —
  `BrowserConfig(browser_mode="cdp")` never even matched crawl4ai's own check, which
  tests for `"custom"` (`async_configs.py:920`), so the CDP attach only worked
  incidentally through `cdp_url`. The earlier entry's @docs/backlog.md pointer is now
  dangling: that entry — including the `--advertise-host` startup trap — was retired with the
  backend, so anyone reviving Lightpanda starts from its own docs, not ours. Re-adding a second
  backend means re-adding the config surface. See @harness/config.py and
  @harness/tools/fetch.py.
