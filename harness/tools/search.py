"""Query the self-hosted SearXNG JSON API and return normalized results.

Any failure to reach SearXNG, a non-200 response, or a malformed body is surfaced as a
typed `SearchFailure` rather than an exception, so a dead search backend shows up as data
for the model to reason about instead of an exception that would sink the whole tool call.
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

    Skips any entry that is not a dict, has no truthy `url`, or carries a wrong-typed
    field — one engine emitting a non-string value must degrade to a skipped entry, not
    an exception out of the tool call. `raw.get(key) or ""` maps both `None` and missing
    keys to `""`, matching the frozen `str` fields (`engine` is declared `str | None`
    upstream in SearXNG).
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


async def _search(
    query: str, max_results: int, config: HarnessConfig
) -> tuple[str, list[SearchResult] | SearchFailure]:
    """Query SearXNG, returning model-facing content and the typed result/failure."""
    url = f"{config.search.base_url.rstrip('/')}/search"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params={"q": query, "format": "json"})
    except httpx.RequestError as exc:
        outcome: list[SearchResult] | SearchFailure = SearchFailure(
            reason="unreachable", detail=f"{type(exc).__name__}: {exc}"
        )
        return _render(query, outcome), outcome

    if response.status_code != 200:
        outcome = SearchFailure(reason="bad_status", detail=f"HTTP {response.status_code}")
        return _render(query, outcome), outcome

    try:
        payload = response.json()
    except ValueError as exc:
        outcome = SearchFailure(reason="malformed", detail=f"response body is not JSON: {exc}")
        return _render(query, outcome), outcome

    if not isinstance(payload, dict):
        detail = f"response body is not an object (got {type(payload).__name__})"
        outcome = SearchFailure(reason="malformed", detail=detail)
        return _render(query, outcome), outcome

    outcome = _parse_results(payload, max_results)
    return _render(query, outcome), outcome


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

    @tool("search_web", args_schema=SearchWebInput, response_format="content_and_artifact")
    async def search_web(
        query: str, max_results: int
    ) -> tuple[str, list[SearchResult] | SearchFailure]:
        """Search the web and return a list of normalized results with titles, URLs, and snippets.

        Failures (unreachable search backend, a non-200 response, or a malformed body) are
        reported as data rather than raising, so a dead search backend never fails the
        whole tool call.
        """
        return await _search(query, max_results, config)

    return search_web
