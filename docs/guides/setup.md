# Setup

## Install

1. Install [uv](https://docs.astral.sh/uv/) — **exactly `0.12.3`**. `pyproject.toml`'s
   `[tool.uv] required-version = "==0.12.3"` is a hard constraint: any other uv refuses
   to run *every* uv command, including step 2, with a version-mismatch error rather than
   a dependency error. A fresh installer gives you a newer uv, so pin it explicitly:

   ```
   curl -LsSf https://astral.sh/uv/0.12.3/install.sh | sh
   ```

   On Windows PowerShell:

   ```
   powershell -c "irm https://astral.sh/uv/0.12.3/install.ps1 | iex"
   ```

   Confirm with `uv --version` before step 2.

   Bumping uv is a deliberate edit to that one line in `pyproject.toml`, which CI, the
   workstation, and a rebuilt runner VM all read — CI installs its uv from the same key
   (see `.github/workflows/ci.yml` and `docs/plans/PLAN-ci-pipeline.md` `## Discoveries`).
2. `uv sync` — creates `.venv` and installs runtime deps (pydantic, langchain-core,
   crawl4ai, httpx) and dev deps (ruff, mypy, pytest, pytest-asyncio, pytest-cov).
   Python 3.12 is used, pinned by `.python-version`; `requires-python` floor is 3.11.
3. `uv run playwright install chromium` — the fetch tool is crawl4ai-managed
   Playwright/Chromium, and this is not covered by `uv sync`.
4. Copy `.env.example` to `.env` and fill in:
   - `OPENCODE_API_KEY` — smart-model orchestration (GLM 5.2 / DeepSeek V4 Pro)
   - `CEREBRAS_API_KEY` — Gemma 4 31B worker triage (free tier)
   - `SEARXNG_SECRET` — cookie signing for the local SearXNG instance; generate
     with `openssl rand -hex 32`

   `SEARXNG_URL` is no longer an `.env` variable — it moved into `harness.toml` (see
   below). If you have an existing `.env` with that key, move its value into
   `harness.toml`'s `[search]` table and delete it from `.env`.
5. Replace `harness.toml`'s remaining `TODO` placeholders with real values: the
   OpenCode base URL and the head/subagent model IDs. (`[search] base_url` is
   already set to the local SearXNG below.) These are **not** validated — `TODO`
   is a well-formed string, so `load_config()` accepts it and the mistake surfaces
   later as a connection or model error. Check them by eye. Nothing reads the model
   roles yet — they are the loop plan's concern, so the fetch and search live
   checks below work with them still unset.

## Running with `.env`

Nothing in the harness reads `.env` — `harness/config.py` resolves `api_key_env` from
the **process environment**, and `uv run` does not load `.env` on its own. Either pass
it explicitly:

```
uv run --env-file .env python -c "from harness.config import load_config; print(load_config())"
```

or set `UV_ENV_FILE=.env` once in your shell profile. Without one of these,
`load_config()` fails with `ConfigError: providers.opencode: Value error, environment
variable 'OPENCODE_API_KEY' is not set` even though `.env` is filled in correctly.

## `harness.toml`

The checked-in config surface (D3 in `docs/plans/PLAN-harness-substrate.md`). Secrets
are never stored here — each provider names an environment variable
(`api_key_env`), resolved from the process environment at load time (see
"Running with `.env`" above).

- `[providers.<name>]` — a model provider's `base_url` and the env var holding its key.
- `[roles.head]` / `[roles.subagent]` — which provider + model ID each role resolves
  to. Both keys are required.
- `[fetch]` — per-page timeout, fetch concurrency, and the per-page character cap.
- `[search]` — the SearXNG base URL and default result count.

## Prerequisites

- **SearXNG** must be reachable with its JSON API enabled. A local instance is
  checked in — start it from the repo root with:

  ```
  docker compose --env-file .env -f searxng/docker-compose.yml up -d
  ```

  `--env-file .env` supplies `SEARXNG_SECRET` (see step 4); compose refuses to
  start without it. The container binds to `127.0.0.1` only, since the limiter
  is off and the JSON API is unauthenticated.

  Do **not** use a bare `docker run ... searxng/searxng`. The stock image ships
  `formats: [html]` and serves HTML even when asked for `format=json`, which the
  search tool cannot parse; its bot limiter also answers unauthenticated API calls
  with HTTP 403. @searxng/settings.yml overrides both. Verify with:

  ```
  curl -s -o /dev/null -w "%{http_code} %{content_type}\n" \
    "http://localhost:8080/search?q=test&format=json"
  ```

  Expect `200 application/json`. `200 text/html` means the settings mount did not
  take effect. Some engines (e.g. `wikidata`) may log a 403 init failure on
  start-up; that is one upstream engine refusing this instance, not a broken
  install — the others still return results.

## Manual live check

The fetch tool needs a real browser and network, so it isn't exercised by `uv run
pytest`. To check it against live URLs, run (Windows needs `PYTHONIOENCODING=utf-8` —
crawl4ai prints box-drawing characters that crash the default `cp1252` console):

The three URLs are chosen to exercise one outcome each: an ordinary article, a URL that
returns 403, and a PDF. The tool is driven with `ainvoke` in its tool-call form, which is
how the agent loop will call it (D1) and what surfaces the artifact alongside the
model-facing content.

```
PYTHONIOENCODING=utf-8 uv run --env-file .env python -c "
import asyncio
from harness.config import load_config
from harness.sources import SourceRegistry
from harness.tools.fetch import build_fetch_tool

async def main():
    registry = SourceRegistry()
    fetch_pages = build_fetch_tool(load_config(), registry)
    message = await fetch_pages.ainvoke({
        'name': 'fetch_pages',
        'args': {'urls': [
            'https://en.wikipedia.org/wiki/Web_scraping',
            'https://httpbin.org/status/403',
            'https://www.africau.edu/images/default/sample.pdf',
        ]},
        'id': 'live-check',
        'type': 'tool_call',
    })
    for page in message.artifact:
        print(page.source_id, page.outcome, page.status_code, page.url)
    print('--- first 800 chars of model-facing content ---')
    print(message.content[:800])

asyncio.run(main())
"
```

Expect `fetched 200` for the article and `blocked 403` for the httpbin URL. The PDF lands
in `fetched` (crawl4ai extracts its text) or `error` (the browser starts a download
instead of navigating) depending on how the server serves it — **not** `non_html`. That is
a known limitation, not a regression: see the PDF entry in `docs/backlog.md`.

The printed content should open with the article's prose under its `## [S1] <url>` heading,
with no Wikipedia sidebar, personal tools, navigation menu, or license footer. A tail of
category links and a "Search / N languages" fragment does survive the pruning filter —
also a known, logged limitation.

The first run downloads a Chromium build if crawl4ai has never launched one on this
machine (`uv run crawl4ai-setup` does it ahead of time).

The search tool needs a real SearXNG instance, so it isn't exercised by `uv run pytest`
either. `harness.toml`'s `[search] base_url` points at the local instance above; start
it first. To check it, run:

```
PYTHONIOENCODING=utf-8 uv run --env-file .env python -c "
import asyncio
from harness.config import load_config
from harness.tools.search import build_search_tool

async def main():
    search_web = build_search_tool(load_config())
    message = await search_web.ainvoke({
        'name': 'search_web',
        'args': {'query': 'solar panel efficiency', 'max_results': 5},
        'id': 'live-check',
        'type': 'tool_call',
    })
    print(message.content)

asyncio.run(main())
"
```

Print `message.content`, not the artifact: on the failure half the artifact is a single
`SearchFailure`, and iterating a pydantic model yields `(key, value)` tuples rather than
results — the rendered content is the one form that reads correctly either way.

Expect real results back from the configured SearXNG. Then point `[search] base_url` at a
dead URL (e.g. `http://localhost:1`) and re-run — expect a `SearchFailure` with reason
`unreachable` printed as plain text, never a traceback.

## Commands

- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Typecheck: `uv run mypy .`
- Test: `uv run pytest`
