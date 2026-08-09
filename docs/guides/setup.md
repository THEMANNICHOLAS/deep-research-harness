# Setup

## Install

1. Install [uv](https://docs.astral.sh/uv/).
2. `uv sync` — creates `.venv` and installs runtime deps (pydantic, langchain-core,
   crawl4ai, httpx) and dev deps (ruff, mypy, pytest, pytest-asyncio).
3. `uv run playwright install chromium` — the default browser backend
   (`browser.backend = "playwright"` in `harness.toml`) needs a Chromium install; this
   is not covered by `uv sync`.
4. Copy `.env.example` to `.env` and fill in:
   - `OPENCODE_API_KEY` — the OpenCode endpoint serving both model roles
   - `SEARXNG_SECRET` — cookie signing for the local SearXNG instance; generate
     with `openssl rand -hex 32`

   Every provider declared in `harness.toml` has its key resolved at load time,
   whether or not a role uses it — so a declared provider with no key set fails
   `load_config()`. Only `[providers.opencode]` is declared today.

   `SEARXNG_URL` and `LIGHTPANDA_CDP_URL` are no longer `.env` variables — they moved
   into `harness.toml` (see below). If you have an existing `.env` with those keys,
   move their values into `harness.toml`'s `[search]` and `[browser]` tables and
   delete them from `.env`.
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
- `[browser]` — the fetch tool's browser backend (`playwright` or `lightpanda`) and,
  for `lightpanda`, the CDP URL.
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

- **Lightpanda** — not currently used (`browser.backend` defaults to `playwright`;
  see the `docs/decisions.md` entry on why). Left here for when the backlog item is
  revisited:

  ```
  docker run -d --name lightpanda -p 9222:9222 lightpanda/browser \
    /bin/lightpanda serve --host 0.0.0.0 --port 9222 --advertise-host 127.0.0.1
  ```

  On Git Bash for Windows, prefix with `MSYS_NO_PATHCONV=1` or the
  `/bin/lightpanda` argument gets rewritten into a Windows path and the container
  exits 127.

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

## Commands

- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Typecheck: `uv run mypy .`
- Test: `uv run pytest`
