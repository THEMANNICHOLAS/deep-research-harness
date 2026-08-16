"""The harness's tool registry package."""

from typing import NamedTuple

from langchain_core.tools import BaseTool

from harness.config import HarnessConfig
from harness.runlog import RunLog, or_default
from harness.sources import SourceRegistry
from harness.tools.ask_user import build_ask_user_tool
from harness.tools.fallback import build_fallback_tool
from harness.tools.fetch import build_fetch_tool
from harness.tools.search import build_search_tool


class ToolSets(NamedTuple):
    """The harness's tools, split by which stack gets to call them (Phase 1 delegation)."""

    lead: list[BaseTool]
    reader: list[BaseTool]


def build_tools(
    config: HarnessConfig, registry: SourceRegistry, run_log: RunLog | None = None
) -> ToolSets:
    """Build every tool the harness exposes, bound to this run's config, registry and run log.

    `fetch_pages` is built exactly once and routed to the reader set only — the lead
    delegates to the reader subagent (`task`) rather than fetching directly (R1). `fetch_raw`
    (Phase 2, D2) is the lead's fallback when digestion fails or returns empty.

    ONE `run_log` is shared across every tool — per-tool logs would fragment the incidents
    the report and terminal disclose. Defaulted only for callers that assert nothing about
    incidents; the real entrypoint always passes the run's shared instance.
    """
    log = or_default(run_log)
    return ToolSets(
        lead=[
            build_search_tool(config, log),
            build_ask_user_tool(config),
            build_fallback_tool(config, registry, log),
        ],
        reader=[build_fetch_tool(config, registry, log)],
    )
