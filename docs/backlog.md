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

- **Residual boilerplate survives the pruning filter.** A tail of category links and a
  "Search / N languages" fragment reaches the model on every fetched page. `min_word_threshold`
  is ruled out as the fix on live measurement — it scores HTML blocks, so it cannot tell a
  one-word heading from a nav stub; full evidence and the numbers are in Reconciliation #2 of
  @docs/plans/PLAN-crawler-refinement.md. To address: a render-side line filter dropping bare
  `* [Text](url)` bullets (the measured front-runner) or an extended `_EXCLUDED_TAGS`, either
  one measured against real pages and mindful of genuine link-only "See also" lists.

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

- **The wall clock has never been cancelled against a real browser teardown.** Both
  offline tests expire the clock inside an `httpx.MockTransport` handler sleeping on
  `asyncio.sleep` — the friendliest possible cancellation target. On a real run the
  in-flight work at the bound is crawl4ai's `arun_many` inside
  `async with AsyncWebCrawler(...)` (@harness/tools/fetch.py), whose `__aexit__` tears
  down a Playwright/Chromium subprocess while the task is being cancelled. Raised by the
  PR #4 review; nothing offline can settle it. To address: during Phase 5's owed live
  check, set `wall_clock_seconds` below `fetch.page_timeout_ms` so the clock fires
  *inside* `arun_many`, then confirm the partial report is still written and no chromium
  process is left behind.

- **`InMemorySaver` checkpoint growth has never been measured.** Deferred at Phase 4
  (a checkpointer is required for `interrupt_on`, so it is not optional), inherited by
  Phase 5, and still untaken after Phase 5's and Phase 6's live checks — the one item
  the research-loop plan's `## Verification` ticks without evidence behind it. Every
  superstep writes a full checkpoint to memory and nothing evicts them, so a run long
  enough to matter is the only thing that can show whether it grows linearly with the
  whole message history. To address: read the process's RSS at the first tool call and
  again at completion during a normal-length run, and compare against the run's total
  input tokens.

- **An agentic verification search tool (greps source text instead of one pooled call per
  paragraph).** Deferred as a non-goal in @docs/plans/PLAN-reader-delegation.md: today's
  `verify_paragraphs` (@harness/verify.py) pools a paragraph's whole cited-source text into
  one model call, which does not scale if a source capture grows large enough that pooling
  several of them stops fitting the model's context. To address: a verifier that can search
  within captured source text on demand rather than having it all pooled up front.
