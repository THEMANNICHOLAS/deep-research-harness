# Setup

## Install

1. Install [uv](https://docs.astral.sh/uv/).
2. `uv sync` — creates `.venv` and installs dev dependencies (ruff, mypy).
3. Copy `.env.example` to `.env` and fill in:
   - `OPENCODE_API_KEY` — smart-model orchestration (GLM 5.2 / DeepSeek V4 Pro)
   - `CEREBRAS_API_KEY` — Gemma 4 31B worker triage (free tier)
   - `SEARXNG_URL` — existing self-hosted SearXNG instance
   - `LIGHTPANDA_CDP_URL` — self-hosted Lightpanda, reached over CDP via crawl4ai

## Prerequisites not yet covered by this repo

- SearXNG must already be deployed and reachable (Docker, JSON API enabled).
- Lightpanda deployment on the homelab machine is part of this project's
  build, not done yet — set it up before the fetch pipeline can run.

## Commands

- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`

## Planned (not yet usable)

- Typecheck: `uv run mypy .` — works once source files exist; currently
  exits non-zero with "no .py[i] files".
- Test: pytest — not yet added as a dependency; add via `uv add --dev pytest`
  when the first test is written.
