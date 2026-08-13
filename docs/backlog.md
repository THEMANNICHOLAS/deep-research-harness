# Backlog

Deferred work and predicted issues that aren't on the critical path right
now — features without the framework or time yet, medium-sized bugs we're
choosing not to chase while focus is elsewhere, design decisions parked
until more is known.

Each entry: name the problem, where it bites, and roughly what it'd take
to address.

## Entries

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
  markdown. Costs tokens on every fetched page. **`min_word_threshold` is ruled out as the
  fix** — measured live, not inferred: on the RAG article, `1` is a byte-identical no-op, `2`
  removes "Search" but not "23 languages" while costing 54% of headings and a third of the
  page's inline links, and `3` clears the target set only at the price of 85% of headings and
  63% of inline links — including links embedded in prose, whose anchor text vanishes with them
  ("Libraries such as [spaCy] or [NLTK] can also help" becomes "Libraries such as or can also
  help"). The filter scores HTML blocks, so a one-word `<h2>` and a nav stub are
  indistinguishable to it, and pruning headings would also strip the boundaries Phase 4's
  truncation cuts on. Full evidence in Reconciliation #2 of
  @docs/plans/PLAN-crawler-refinement.md. To address: a render-side line filter that drops
  bare `* [Text](url)` bullets (measured at -84 lines / -30% chars on that page with all 13
  headings and all 161 inline links kept), or extend `_EXCLUDED_TAGS` — both measured against
  real fetched pages rather than in the abstract, and both must keep a genuine link-only
  "See also" list in mind.

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

- **The CI runner's configuration is not recorded anywhere.** CI depends on a self-hosted
  GitHub Actions runner (`CI-Runner`, default tags `self-hosted`/`Linux`/`X64`) on a Proxmox
  VM, but its systemd unit name, work directory, runner version, and OS version are not
  written down, and `systemctl is-enabled` was never run — so nothing confirms the runner
  comes back unattended after a reboot. This bites if the VM is lost or rebuilt: the runner
  must be re-registered from GitHub's own documentation, and until it is, every pull request
  queues forever with no verdict. Deliberately descoped by the developer on 2026-08-09 (see
  the Phase 4 entry in @docs/plans/PLAN-ci-pipeline.md `## Reconciliations`), which drops
  requirement R5. The project-side facts a rebuild needs — the uv pin and the setup-uv SHA —
  are both already in @pyproject.toml and @.github/workflows/ci.yml. To address: capture the
  unit name and `systemctl is-enabled` output, either over SSH or via a temporary read-only
  step in the workflow, since the job runs on the VM itself.

- **Most dependencies are `>=` floors, not exact pins.** `pyproject.toml` declares
  `pydantic>=2.9`, `langchain-core>=0.3`, `httpx>=0.27`, and a `dev` group
  (`ruff`, `mypy`, `pytest`, `pytest-asyncio`) with no constraints at all; only
  `crawl4ai==0.9.2` is pinned. A `>=` floor blocks older releases but lets the resolved
  version float, so the workstation, the CI runner, and a rebuilt VM can each land on
  something different — `uv.lock` holds this steady in practice, but the declared intent
  doesn't. This bites reproducibility (R5's rebuild story in
  @docs/plans/PLAN-ci-pipeline.md) and makes an unplanned tool upgrade look like a code
  regression. Developer instruction (2026-08-09): **all requirements in this project should
  be pinned exactly (`==`), never `>=`.** Only `[tool.uv] required-version` was pinned in
  that session — converting the rest is a separate change, each pin chosen against what
  `uv.lock` already resolves, then re-locked and pushed through CI.

- **Nothing retries a rate-limited page.** `RateLimiter(max_retries=...)` in
  `harness/tools/fetch.py` does not re-fetch on a 429/503 — crawl4ai 0.9.2 calls
  `update_delay` after the crawl has returned and only grows that domain's backoff delay
  (`async_dispatcher.py:65-85`, verified 2026-08-11). So a source that rate-limits us is
  reported `blocked` on a single attempt, and a transient 429 costs the whole page. This bites
  research coverage against APIs and doc sites that throttle bursts. To address: a retry pass
  in `_fetch` over the `blocked` outcomes, which is genuinely new machinery (attempt budget,
  backoff, and a rule for how a retried page reports) — deliberately deferred in Phase 1 of
  @docs/plans/PLAN-crawler-refinement.md, see its Reconciliation #1.

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
