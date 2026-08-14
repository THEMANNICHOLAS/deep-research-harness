"""`fetch_raw`: the lead's recovery path when reader digestion failed or returned empty (D2).

Reuses `harness.tools.fetch`'s `_fetch`/`_render` internals rather than re-implementing
fetching or capture writing (same package; fetch.py's own tests already import these
cross-module). Content is wrapped in an explicit `<undigested>` marker so the run's report
and orchestrator prompt can disclose that a source was never actually digested by the
reader — it is the lead reading raw page text directly, not the reader's [Sn]-cited summary.
"""

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig
from harness.sources import SourceRegistry
from harness.tools.fetch import FetchedPage, _fetch, _render, _sources_dir


async def _fetch_raw(
    urls: list[str], reason: str, config: HarnessConfig, registry: SourceRegistry
) -> tuple[str, list[FetchedPage]]:
    """Fetch every URL via the shared `_fetch`, wrapping each successful page in the marker.

    Only a successful (`fetched`) page is wrapped and marked `"fallback"` — a failed fetch's
    stub is rendered exactly like `fetch_pages` renders it and stays `"unread"`, since nothing
    was actually captured for the lead to read raw.
    """
    _, pages = await _fetch(urls, config, registry)

    # `"` is escaped rather than stripped: the reason is model-supplied prose that may
    # legitimately quote something, and dropping the quote marks would lose that context.
    escaped_reason = reason.replace('"', "&quot;")

    blocks: list[str] = []
    for page in pages:
        rendered = _render(page, config.fetch.per_page_char_cap)
        if page.outcome == "fetched":
            registry.mark_read(page.source_id, "fallback")
            rendered = (
                f'<undigested source="{page.source_id}" reason="{escaped_reason}">\n'
                f"{rendered}\n"
                f"</undigested>"
            )
        blocks.append(rendered)

    return "\n\n".join(blocks), pages


def build_fallback_tool(config: HarnessConfig, registry: SourceRegistry) -> BaseTool:
    """Build the `fetch_raw` tool, closing over `config` and the shared `registry`.

    Mirrors `build_fetch_tool`'s own `mkdir`: `fetch_raw` can be exercised (and, in a live
    run, called) before `fetch_pages` ever has been, so it cannot rely on the reader's tool
    having already created `<workspace_dir>/<run_id>/sources`.
    """
    _sources_dir(config, registry).mkdir(parents=True, exist_ok=True)

    max_urls = config.fetch.max_urls_per_call

    class FetchRawInput(BaseModel):
        """Model-facing input schema for the `fetch_raw` tool."""

        model_config = ConfigDict(extra="forbid")

        urls: list[str] = Field(
            max_length=max_urls,
            description=(
                "The URLs to fetch raw (undigested), in the order they should be reported. "
                f"At most {max_urls} per call."
            ),
        )
        reason: str = Field(description="Why digestion failed or was skipped for these URLs.")

    @tool("fetch_raw", args_schema=FetchRawInput, response_format="content_and_artifact")
    async def fetch_raw(urls: list[str], reason: str) -> tuple[str, list[FetchedPage]]:
        """Fetch the given URLs directly, bypassing reader digestion.

        Recovery path ONLY: call this after `task(subagent_type="reader")` failed after retry
        or returned an empty digest. Each successfully fetched page is wrapped in an
        `<undigested>` marker so the run's report can disclose it as raw, undigested content.
        """
        return await _fetch_raw(urls, reason, config, registry)

    # Appended rather than written into the docstring, matching fetch_pages: the limit is
    # config, and a literal would go stale the moment an operator changed it.
    fetch_raw.description = (
        f"{fetch_raw.description}\n\nAt most {max_urls} URLs may be requested per call; "
        "a call carrying more is rejected without fetching anything."
    )

    # `exc` is `object`: langchain may hand over a pydantic v1 or v2 `ValidationError`.
    def _explain_validation_error(exc: object) -> str:
        """Turn a rejected call into a message the model can act on and retry."""
        return (
            f"fetch_raw rejected this call without fetching anything: {exc}. "
            f"At most {max_urls} URLs may be requested per call."
        )

    fetch_raw.handle_validation_error = _explain_validation_error

    return fetch_raw
