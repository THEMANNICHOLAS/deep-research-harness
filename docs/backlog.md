# Backlog

Deferred work and predicted issues that aren't on the critical path right
now — features without the framework or time yet, medium-sized bugs we're
choosing not to chase while focus is elsewhere, design decisions parked
until more is known.

Each entry: name the problem, where it bites, and roughly what it'd take
to address.

## Entries

- **Lightpanda cannot currently drive crawl4ai's `goto`.** No page lifecycle event
  ever arrives over CDP, so `Page.goto` times out even though the CDP connection
  attaches successfully (see @docs/decisions.md). This bites the browser backend
  selection in `harness/tools/fetch.py` (Phase 3), which is why `browser.backend`
  defaults to `playwright` there instead. To revisit: retest against a later
  Lightpanda release, or drive navigation with a strategy that does not wait on a
  lifecycle event crawl4ai currently blocks on. Separately, `--advertise-host` must be
  set when starting Lightpanda — without it the server advertises
  `webSocketDebuggerUrl: ws://0.0.0.0:9222/`, which no CDP client can dial; this is
  independent of the lifecycle-event problem and needed regardless of which fix lands.
