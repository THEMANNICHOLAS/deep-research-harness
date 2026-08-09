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

- **PDFs never classify as `non_html`.** The `non_html` outcome in
  `harness/tools/fetch.py` assumes crawl4ai returns a successful crawl with empty markdown
  for a PDF. Over crawl4ai-managed Playwright it does neither: a PDF that triggers a
  browser download surfaces as `Page.goto: Download is starting` and classifies `error`,
  while a PDF served inline gets its text extracted and classifies `fetched` (4307
  characters, in the Phase 3 live check). This bites any caller that wants to tell "we
  couldn't read this format" from "we failed" — right now the two are indistinguishable
  for PDFs. Confirmed by live check, not inferred; full evidence in the Phase 3 PDF entry
  of @docs/plans/PLAN-harness-substrate.md `## Reconciliations`. To address: dispatch on
  the `content-type` response header before classifying, or adopt crawl4ai's
  `PDFCrawlerStrategy` (deliberately unused in the substrate plan). Deferred rather than
  fixed mid-phase per that plan's risk #2, which forbids widening the classifier during
  Phase 3.

- **Residual boilerplate survives the pruning filter.** `PruningContentFilter` strips
  Wikipedia's sidebar, personal tools, navigation menu, privacy policy and license footer,
  but a tail of category links and a "Search / N languages" fragment remains in the fetched
  markdown. Costs tokens on every fetched page. The substrate plan's Preferences place
  stripping quality outside the acceptance gate ("tuning quality is iterative"), so this is
  tuning work: adjust `PruningContentFilter`'s threshold or extend `_EXCLUDED_TAGS` in
  `harness/tools/fetch.py`, measured against real fetched pages rather than in the abstract.

- **`harness.toml`'s `TODO` placeholders load as valid config.** The literal `"TODO"`
  strings shipped for the OpenCode `base_url` and both role model IDs pass `load_config()`
  — they are well-formed strings and nothing in the substrate reads them. This bites at the
  future agent loop's first model call, which would fail with an HTTP error instead of the
  startup `ConfigError` R7 intends. Deliberately kept (per review decision 2026-08-09):
  validating at load would reject the checked-in file and block the fetch/search tools,
  which never read provider values. Pinned visible by
  `test_shipped_harness_toml_loads_with_its_todo_placeholders` in @tests/test_config.py.
  To address when the loop lands: validate `base_url` shape and non-`TODO` model IDs
  wherever roles are first consumed.

- **An HTTP 404 that serves a real HTML body classifies as `fetched`.** Observed in the
  final end-to-end sanity check: a Wikipedia URL returning 404 came back
  `outcome=fetched, status_code=404` with the "page does not exist" body as its markdown,
  so the model would receive an error page as if it were a source. This follows the frozen
  classification rules in @docs/plans/PLAN-harness-substrate.md — D7 concludes `blocked`
  from 403/429/503 alone and nothing else inspects status — so the classifier was NOT
  widened, consistent with that plan's risk #2 stance. To address: decide whether a 4xx/5xx
  other than the three `blocked` codes deserves its own outcome, or whether the caller
  should read `FetchedPage.status_code` (already carried in the artifact) and judge for
  itself. The information is not lost, only unclassified.
