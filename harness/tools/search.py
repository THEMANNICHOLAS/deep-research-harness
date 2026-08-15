"""Query the self-hosted SearXNG JSON API and return normalized results.

An unreachable backend, a non-200, or a malformed body all surface as a typed
`SearchFailure` rather than an exception.
"""

from typing import Literal

import httpx
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.config import HarnessConfig


class SearchResult(BaseModel):
    """One normalized SearXNG result."""

    model_config = ConfigDict(extra="forbid")

    title: str
    url: str
    snippet: str
    engine: str


class SearchFailure(BaseModel):
    """A typed failure to complete a search, naming why."""

    model_config = ConfigDict(extra="forbid")

    reason: Literal["unreachable", "bad_status", "malformed"]
    detail: str


def _parse_results(payload: dict, max_results: int) -> list[SearchResult] | SearchFailure:
    """Extract and normalize the `results` array, slicing to `max_results` after parsing.

    Skips any entry that is not a dict, lacks a truthy `url`, or is wrong-typed: one engine
    emitting a non-string degrades to a skipped entry, not an exception. `raw.get(key) or ""`
    maps `None` and missing keys alike to `""`, matching the frozen `str` fields (SearXNG
    declares `engine` as `str | None`).
    """
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return SearchFailure(reason="malformed", detail="response body has no 'results' list")

    results: list[SearchResult] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        url = raw.get("url") or ""
        if not url:
            continue
        try:
            results.append(
                SearchResult(
                    title=raw.get("title") or "",
                    url=url,
                    snippet=raw.get("content") or "",
                    engine=raw.get("engine") or "",
                )
            )
        except ValidationError:
            continue

    return results[:max_results]


def _render(query: str, outcome: list[SearchResult] | SearchFailure) -> str:
    """Render the model-facing content: a numbered list, a no-results line, or a failure."""
    if isinstance(outcome, SearchFailure):
        return f'Search for "{query}" failed: {outcome.reason} — {outcome.detail}'
    if not outcome:
        return f'Search for "{query}" returned no results.'

    lines = [f'Results for "{query}":']
    for index, result in enumerate(outcome, start=1):
        lines.append(f"{index}. {result.title} — {result.url}\n   {result.snippet}")
    return "\n".join(lines)


def _search_url(config: HarnessConfig) -> str:
    return f"{config.search.base_url.rstrip('/')}/search"


async def _fetch_search_json(query: str, config: HarnessConfig) -> object | SearchFailure:
    """GET the SearXNG JSON endpoint and parse the body, or return the typed failure.

    The single place the request is made: `_search` and `preflight_search` both go through
    it, so a change to how SearXNG is called (timeout, headers, TLS) lands in both at once.
    A bare `httpx.AsyncClient()` so `install_search_transport` swaps it in tests.
    """
    url = _search_url(config)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params={"q": query, "format": "json"})
    except httpx.RequestError as exc:
        return SearchFailure(reason="unreachable", detail=f"{type(exc).__name__}: {exc}")

    if response.status_code != 200:
        return SearchFailure(reason="bad_status", detail=f"HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError as exc:
        return SearchFailure(reason="malformed", detail=f"response body is not JSON: {exc}")


async def _search(
    query: str, max_results: int, config: HarnessConfig
) -> tuple[str, list[SearchResult] | SearchFailure]:
    """Query SearXNG, returning model-facing content and the typed result/failure."""
    payload = await _fetch_search_json(query, config)
    if isinstance(payload, SearchFailure):
        return _render(query, payload), payload

    if not isinstance(payload, dict):
        detail = f"response body is not an object (got {type(payload).__name__})"
        failure = SearchFailure(reason="malformed", detail=detail)
        return _render(query, failure), failure

    outcome = _parse_results(payload, max_results)
    return _render(query, outcome), outcome


class SearchPreflightError(Exception):
    """Raised when the configured SearXNG endpoint fails the startup health check."""


_CONTAINER_HINT = "is the container running? (docker compose up in searxng/)"


async def preflight_search(config: HarnessConfig) -> None:
    """Verify the configured SearXNG endpoint answers a real JSON search before any run starts.

    R1/D4: probes via `_fetch_search_json` — the exact request `_search` makes — asserting 200
    and a parseable JSON body. This catches both the container being down AND the documented
    "stock container is HTML-only" misconfiguration, either of which would otherwise only
    surface mid-run.

    Raises `SearchPreflightError` naming SearXNG, the probed URL, and the container hint.
    """
    payload = await _fetch_search_json("ping", config)
    if not isinstance(payload, SearchFailure):
        return

    url = _search_url(config)
    if payload.reason == "unreachable":
        raise SearchPreflightError(
            f"SearXNG unreachable at {url} — {_CONTAINER_HINT} ({payload.detail})"
        )
    if payload.reason == "bad_status":
        raise SearchPreflightError(
            f"SearXNG at {url} returned {payload.detail} — {_CONTAINER_HINT}"
        )
    raise SearchPreflightError(
        f"SearXNG at {url} did not return JSON (got HTML? the JSON API may not be enabled) "
        f"— {_CONTAINER_HINT} ({payload.detail})"
    )


class SearchUnavailableError(Exception):
    """Raised when SearXNG has failed too many consecutive times in a single run (R2/D3).

    The agent loop never special-cases this — it reaches the generic exception handler like
    any other unexpected error, ending the run as a hard error with no report.
    """


def build_search_tool(config: HarnessConfig) -> BaseTool:
    """Build the `search_web` tool, closing over `config`."""

    class SearchWebInput(BaseModel):
        """Model-facing input schema for the `search_web` tool."""

        model_config = ConfigDict(extra="forbid")

        query: str = Field(description="The search query to send to the search engine.")
        max_results: int = Field(
            default=config.search.default_max_results,
            ge=1,
            description="The maximum number of results to return.",
        )

    # D3: per-tool-instance (i.e. per-run) counter of CONSECUTIVE connection-level failures.
    # Lives in this closure, not in `_search` or at module scope, so it never leaks across
    # runs/tests. `malformed` failures neither increment nor reset it.
    consecutive_failures = 0

    @tool("search_web", args_schema=SearchWebInput, response_format="content_and_artifact")
    async def search_web(
        query: str, max_results: int
    ) -> tuple[str, list[SearchResult] | SearchFailure]:
        """Search the web and return a list of normalized results with titles, URLs, and snippets.

        Failures (unreachable search backend, a non-200 response, or a malformed body) are
        reported as data rather than raising, so a dead search backend never fails the
        whole tool call — except after too many consecutive connection-level failures in a
        row, which raises `SearchUnavailableError` to abort the run.
        """
        nonlocal consecutive_failures
        content, outcome = await _search(query, max_results, config)

        if isinstance(outcome, SearchFailure) and outcome.reason in ("unreachable", "bad_status"):
            consecutive_failures += 1
            if consecutive_failures >= config.search.max_consecutive_failures:
                raise SearchUnavailableError(
                    f"SearXNG search failed {consecutive_failures} times in a row — "
                    "aborting the run (is the container still up?)"
                )
        elif not isinstance(outcome, SearchFailure):
            consecutive_failures = 0
        # reason == "malformed": leave the counter unchanged.

        return content, outcome

    return search_web
