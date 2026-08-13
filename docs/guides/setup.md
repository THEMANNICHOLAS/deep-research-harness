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
   crawl4ai, httpx, deepagents, langchain-openai) and dev deps (ruff, mypy, pytest,
   pytest-asyncio, pytest-cov). Python 3.12 is used, pinned by `.python-version`;
   `requires-python` floor is 3.11. `deepagents` and `langchain-openai` are both pinned
   exactly; the former also pulls in langchain-anthropic, langchain-google-genai and
   langsmith.
3. `uv run playwright install chromium` — the fetch tool is crawl4ai-managed
   Playwright/Chromium, and this is not covered by `uv sync`.
4. Copy `.env.example` to `.env` and fill in:
   - `OPENCODE_API_KEY` — the OpenCode endpoint serving both model roles
   - `SEARXNG_SECRET` — cookie signing for the local SearXNG instance; generate
     with `openssl rand -hex 32`

   Every provider declared in `harness.toml` has its key resolved at load time,
   whether or not a role uses it — so a declared provider with no key set fails
   `load_config()`. Only `[providers.opencode]` is declared today.

   `SEARXNG_URL` is no longer an `.env` variable — it moved into `harness.toml` (see
   below). If you have an existing `.env` with that key, move its value into
   `harness.toml`'s `[search]` table and delete it from `.env`.
5. `harness.toml` ships with real values — no `TODO` placeholders remain. If you
   change the endpoint or a model ID, note that a literal `TODO` still passes
   `load_config()` (it is a well-formed string), but is rejected at startup by
   `build_chat_model`, which raises `ModelError` naming the role, the provider, and
   the offending value. A wrong-but-well-formed endpoint or model ID is caught by
   `preflight` before any research starts, not mid-run.

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
- `[fetch]` — per-page timeout, fetch concurrency, the per-page character cap, and the
  maximum URLs one `fetch_pages` call may request (`max_urls_per_call`; a call carrying
  more is rejected without fetching anything).
- `[search]` — the SearXNG base URL and default result count.
- `[agent]` — `max_rounds` (default 20) and `wall_clock_seconds` (default 1800), the
  run's two ceilings. The wall clock starts at the first `search_web`/`fetch_pages`
  call, not at launch, so an initial clarifying question can be answered at leisure;
  hitting either bound still writes a report naming which one it was. `max_rounds` is
  approximate — it maps onto LangGraph supersteps and buys somewhat fewer rounds than
  its number suggests (see @docs/plans/PLAN-research-loop.md `## Discoveries`).
- `max_rounds` bounds one PASS, not the whole run: every clarification resume grants a
  fresh allowance, so a run that asked two questions may use roughly three times the
  number configured. The wall clock is the only run-level bound once research starts.

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

The head model role needs a real provider, so it isn't exercised by `uv run pytest` either.
This requires `OPENCODE_API_KEY` set. To check it, run:

```
uv run --env-file .env python -c "
from harness.config import load_config
from harness.models import build_chat_model

model = build_chat_model(load_config(), 'head')
print(model.invoke('Say hi in five words or fewer.').content)
"
```

Expect a short sentence back. To check the R6 startup guard — an endpoint or model that is
well-formed but wrong — point `[providers.opencode] base_url` at a dead URL (e.g.
`http://localhost:1/v1`) and run:

```
uv run --env-file .env python -c "
import asyncio
from harness.config import load_config
from harness.models import preflight

asyncio.run(preflight(load_config(), 'head'))
"
```

Expect a `ModelError` naming the role, the provider, the base URL, and the model — never a
raw `openai` or `httpx` traceback. `base_url` is the API **base**, not a full endpoint: the
client appends `/chat/completions` itself, so a value ending in `/chat/completions` produces
a doubled path and fails here.

## End-to-end live check

Runs the whole loop for real — model, SearXNG, and a live browser fetch — and writes a
report. This costs real tokens, so it is not part of `uv run pytest`.

```
uv run --env-file .env python -m harness "What changed in Python 3.14's free-threading support?"
```

Expect the research plan to echo at the terminal as the agent works, and the final line of
stdout to be the path of a timestamped report under `[agent] reports_dir`. Open that file: it
should answer the question, carry `[Sn]` markers on its claims, and list its sources. Every
source consulted also leaves a file under `[agent] workspace_dir` in `sources/`.

**Running from a git worktree:** `.env` is gitignored, so it does not exist inside a worktree.
Point uv at the main checkout's copy — `uv run --env-file ../../../.env python -m harness
"..."` — or run from the main checkout instead. Without it the run fails with "No environment
file found".

Note that `[roles.head]`'s model is a reasoning model, so most of a run's output tokens are
reasoning tokens; the report records the split rather than a single total, because a bare
total would misprice any later delegation work against this baseline.

## Commands

- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Typecheck: `uv run mypy .`
- Test: `uv run pytest`
