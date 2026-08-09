# Fast Research Harness

A self-hosted, Perplexity-style research agent that answers questions with cited
sources, built as a reusable agent-loop/tool-registry harness so future toolpacks
can plug in without rework.

## Status

The **substrate** is built and tested: config, citation registry, fetch and search
tools, prompt loader, and the tool list. **There is no agent loop yet** — nothing
here calls a model. Tools are LangChain-native (`BaseTool`, async) so they drop
into an orchestrator later without translation.

## Requirements

- Python >= 3.11, managed with [uv](https://docs.astral.sh/uv/)
- Docker (for the local SearXNG instance)

## Quick start

```
uv sync
docker compose -f searxng/docker-compose.yml up -d
uv run pytest
```

Then fill in `harness.toml`'s remaining `TODO` values and copy `.env.example` to
`.env` for API keys. Full instructions, including the manual live checks for the
network-dependent tools, are in `docs/guides/setup.md`.

## Layout

| Path | Contents |
|---|---|
| `harness/config.py` | TOML + env config loading and validation |
| `harness/sources.py` | Per-run registry assigning `[Sn]` citation IDs |
| `harness/prompts.py` | Prompt loading and `$variable` rendering |
| `harness/prompts/` | Versioned prompt files (`.md` only) |
| `harness/tools/` | `build_tools` plus one module per tool |
| `tests/` | Offline, fixture-based test suite |
| `searxng/` | Local SearXNG instance with the JSON API enabled |
| `docs/` | Architecture, plans, decisions, setup guide |

## Configuration

Endpoints, model IDs, and limits live in `harness.toml`. API keys never do — the
config names the environment variable holding each key, and `.env` supplies it.

## Commands

| Command | Description |
|---|---|
| `uv run pytest` | Run the test suite |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Format check |
| `uv run mypy .` | Typecheck |

## Documentation

See `docs/INDEX.md` for the full documentation map, and `CLAUDE.md` for
agent-facing project facts and conventions.
