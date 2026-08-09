# Setup

## Install

1. Install [uv](https://docs.astral.sh/uv/).
2. `uv sync` — creates `.venv` and installs runtime deps (pydantic, langchain-core,
   crawl4ai, httpx) and dev deps (ruff, mypy, pytest, pytest-asyncio).
3. `uv run playwright install chromium` — the default browser backend
   (`browser.backend = "playwright"` in `harness.toml`) needs a Chromium install; this
   is not covered by `uv sync`.
4. Copy `.env.example` to `.env` and fill in:
   - `OPENCODE_API_KEY` — smart-model orchestration (GLM 5.2 / DeepSeek V4 Pro)
   - `CEREBRAS_API_KEY` — Gemma 4 31B worker triage (free tier)

   `SEARXNG_URL` and `LIGHTPANDA_CDP_URL` are no longer `.env` variables — they moved
   into `harness.toml` (see below). If you have an existing `.env` with those keys,
   move their values into `harness.toml`'s `[search]` and `[browser]` tables and
   delete them from `.env`.
5. Replace `harness.toml`'s `TODO` placeholders with real values (OpenCode base URL,
   head and subagent model IDs, SearXNG base URL). These are **not** validated —
   `TODO` is a well-formed string, so `load_config()` accepts it and the mistake
   surfaces later as a connection or model error. Check them by eye.

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

- **SearXNG** must be reachable with its JSON API enabled
  (`formats: [html, json]` in its `settings.yml`) — the Phase 4 search tool cannot
  parse the HTML-only response otherwise:

  ```
  docker run -d --name searxng -p 8080:8080 searxng/searxng
  ```

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

## Commands

- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Typecheck: `uv run mypy .`
- Test: `uv run pytest`
