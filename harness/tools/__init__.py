"""The harness's tool registry package."""

from langchain_core.tools import BaseTool

from harness.config import HarnessConfig
from harness.sources import SourceRegistry
from harness.tools.ask_user import build_ask_user_tool
from harness.tools.fetch import build_fetch_tool
from harness.tools.search import build_search_tool


def build_tools(config: HarnessConfig, registry: SourceRegistry) -> list[BaseTool]:
    """Build every tool the harness exposes, bound to this run's config and registry."""
    return [
        build_fetch_tool(config, registry),
        build_search_tool(config),
        build_ask_user_tool(config),
    ]
