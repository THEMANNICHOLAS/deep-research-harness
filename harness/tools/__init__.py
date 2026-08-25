"""The harness's tool registry package."""

from typing import TYPE_CHECKING, NamedTuple

from langchain_core.tools import BaseTool

from harness.blocklist import load_blocklist
from harness.config import HarnessConfig
from harness.runlog import RunLog, or_default
from harness.sources import SourceRegistry
from harness.tools.ask_user import build_ask_user_tool
from harness.tools.fallback import build_fallback_tool
from harness.tools.fetch import build_fetch_tool
from harness.tools.search import build_search_tool

if TYPE_CHECKING:
    from harness.browser import BrowserSession


class ToolSets(NamedTuple):
    """The harness's tools, split by which tier gets to call them (Step 3's 3-tier hierarchy)."""

    lead: list[BaseTool]
    researcher: list[BaseTool]
    reader: list[BaseTool]


def build_tools(
    config: HarnessConfig,
    registry: SourceRegistry,
    run_log: RunLog | None = None,
    browser: "BrowserSession | None" = None,
) -> ToolSets:
    """Build every tool the harness exposes, bound to this run's config, registry and run log.

    `fetch_pages` is built exactly once and routed to the reader set only — the researcher
    delegates to the reader subagent (`task`) rather than fetching directly (R1). `search_web`
    and `fetch_raw` (Phase 2, D2) both route to the researcher: the digest-recovery loop
    belongs to whoever dispatches readers, not to the lead, which keeps only `ask_user` (its
    planning/workspace tools come from middleware/backend, not this registry).

    ONE `run_log` is shared across every tool — per-tool logs would fragment the incidents
    the report and terminal disclose. Defaulted only for callers that assert nothing about
    incidents; the real entrypoint always passes the run's shared instance.

    The persistent domain blocklist (Phase 3, R3/R4) is loaded ONCE here and shared the same
    way, and for the same reason: a hostname walled mid-run by one fetch must start filtering
    `search_web`'s results immediately, which three independently-loaded copies would not do.

    ONE `browser` session (Phase 1, R2) is shared for the same reason as the `run_log` and the
    blocklist above: a fresh crawler per call would defeat the whole point of a session browser.
    It goes to BOTH `fetch_pages` and `fetch_raw` — `search_web` never fetches a page itself, so
    it does not take one — or R2 would be false for the fallback path the moment it fired.
    """
    log = or_default(run_log)
    blocklist = load_blocklist(config.blocklist.path)
    return ToolSets(
        lead=[build_ask_user_tool(config)],
        researcher=[
            build_search_tool(config, registry, log, blocklist),
            build_fallback_tool(config, registry, log, blocklist, browser),
        ],
        reader=[build_fetch_tool(config, registry, log, blocklist, browser)],
    )
