"""`fetch_raw`: the researcher's recovery path when reader digestion failed or returned empty (D2).

Reuses `harness.tools.fetch`'s `_fetch`/`_render` internals rather than re-implementing
fetching or capture writing (same package; fetch.py's own tests already import these
cross-module). Content is wrapped in an explicit `<undigested>` marker so the run's report
and orchestrator prompt can disclose that a source was never actually digested by the
reader — it is the lead reading raw page text directly, not the reader's [Sn]-cited summary.
"""

from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from harness.blocklist import Blocklist, resolve_blocklist
from harness.config import HarnessConfig
from harness.runlog import RunLog, or_default
from harness.sources import SourceRegistry, normalize_url, sources_dir
from harness.tools.fetch import (
    FetchedPage,
    _fetch,
    _install_fetch_contract,
    _render,
)

if TYPE_CHECKING:
    from harness.browser import BrowserSession


async def _fetch_raw(
    urls: list[str],
    reason: str,
    config: HarnessConfig,
    registry: SourceRegistry,
    run_log: RunLog,
    blocklist: Blocklist | None = None,
    browser: "BrowserSession | None" = None,
) -> tuple[str, list[FetchedPage]]:
    """Fetch every URL via the shared `_fetch`, wrapping each successful page in the marker.

    Only a successful (`fetched`) page is wrapped and marked `"fallback"` — a failed fetch's
    stub is rendered exactly like `fetch_pages` renders it and stays `"unread"`, since nothing
    was actually captured for the lead to read raw. `_fetch` is shared, so a blocklisted URL
    hits the same pre-crawl backstop here as through `fetch_pages` (R4), and `browser`, when
    given, is the same shared session — or R2 would be false for this fallback path.
    """
    _, pages = await _fetch(urls, config, registry, run_log, blocklist, browser)

    # `"` is escaped rather than stripped: the reason is model-supplied prose that may
    # legitimately quote something, and dropping the quote marks would lose that context.
    escaped_reason = reason.replace('"', "&quot;")

    blocks: list[str] = []
    for page in pages:
        rendered = _render(page, config.fetch.per_page_char_cap)
        if page.outcome == "fetched":
            assert page.source_id is not None  # every `fetched` page was minted an id
            # Never downgrade: a source an earlier delegation already digested keeps its
            # "digested" mode even if the lead re-fetches it raw (e.g. for a second facet).
            # The <undigested> wrapper still applies — it describes THIS payload being raw —
            # but the report's disclosure reflects the strongest coverage the run achieved.
            current = registry.get(page.source_id)
            if current is not None and current.read_mode != "digested":
                registry.mark_read(page.source_id, "fallback")
            rendered = (
                f'<undigested source="{page.source_id}" reason="{escaped_reason}">\n'
                f"{rendered}\n"
                f"</undigested>"
            )
        blocks.append(rendered)

    # A URL that produced no page was rejected by policy or replayed from an earlier failure
    # (D1/D2). `_fetch` recorded its verdict; fetch_raw shows it rather than returning a batch
    # with silent holes (R1). Grouped after the pages rather than interleaved: a fetch_raw call
    # is a one- or two-URL recovery batch, so ordering carries no information here.
    seen = {normalize_url(page.url) for page in pages}
    for url in urls:
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        block = registry.failed_block(url)
        if block is not None:
            blocks.append(block)

    return "\n\n".join(blocks), pages


def build_fallback_tool(
    config: HarnessConfig,
    registry: SourceRegistry,
    run_log: RunLog | None = None,
    blocklist: Blocklist | None = None,
    browser: "BrowserSession | None" = None,
) -> BaseTool:
    """Build the `fetch_raw` tool, closing over `config`, the shared `registry` and `run_log`.

    Mirrors `build_fetch_tool`'s own `mkdir`: `fetch_raw` can be exercised (and, in a live
    run, called) before `fetch_pages` ever has been, so it cannot rely on the reader's tool
    having already created `<workspace_dir>/<run_id>/sources`. `browser` (Phase 1, R2) threads
    the same way, to the same shared session.
    """
    sources_dir(config, registry).mkdir(parents=True, exist_ok=True)

    log = or_default(run_log)
    domain_blocklist = resolve_blocklist(blocklist, config.blocklist.path)
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
        return await _fetch_raw(urls, reason, config, registry, log, domain_blocklist, browser)

    return _install_fetch_contract(fetch_raw, max_urls)
