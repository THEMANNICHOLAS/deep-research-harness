"""The harness's tool registry package."""

from typing import NamedTuple

from langchain_core.tools import BaseTool

from harness.config import HarnessConfig
from harness.sources import SourceRegistry
from harness.tools.ask_user import build_ask_user_tool
from harness.tools.fallback import build_fallback_tool
from harness.tools.fetch import build_fetch_tool
from harness.tools.search import build_search_tool


class ToolSets(NamedTuple):
    """The harness's tools, split by which stack gets to call them (Phase 1 delegation)."""

    lead: list[BaseTool]
    reader: list[BaseTool]


def build_tools(config: HarnessConfig, registry: SourceRegistry) -> ToolSets:
    """Build every tool the harness exposes, bound to this run's config and registry.

    `fetch_pages` is built exactly once and routed to the reader set only — the lead
    delegates to the reader subagent (`task`) rather than fetching directly (R1). `fetch_raw`
    (Phase 2, D2) is the lead's fallback when digestion fails or returns empty.
    """
    return ToolSets(
        lead=[
            build_search_tool(config),
            build_ask_user_tool(config),
            build_fallback_tool(config, registry),
        ],
        reader=[build_fetch_tool(config, registry)],
    )
