"""The lead's two session-control tools: start a researcher, and end research with a report.

Both are thin fronts over `harness.session.Session` — the loop owns the researcher tasks, the
roster and the report gate, and these tools exist only to give the model a name, description
and schema for reaching them (the same shape `ask_user` uses).

`dispatch_researcher` replaces deepagents' `task` on the LEAD tier alone (D1): it starts the
compiled researcher graph as a background `asyncio.Task` and returns at once, so one lead turn
may fire several researchers and the lead keeps its turn instead of blocking inside the tool
node until they all finish. The researcher's own dispatch to a reader is still `task` and is
untouched.

`submit_report` is the ONLY way a run produces a report (D3): the final answer is the tool
ARGUMENT, never whatever prose happened to come last, so a narration turn can never be
mistaken for an answer.
"""

from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    # Annotation-only: `harness.session` imports `harness.agent`, which imports this module
    # through `harness.tools`. A runtime import here would close that cycle.
    from harness.session import Session

DISPATCH_RESEARCHER_TOOL_NAME = "dispatch_researcher"
SUBMIT_REPORT_TOOL_NAME = "submit_report"


def build_dispatch_researcher_tool(session: "Session | None") -> BaseTool:
    """Build the `dispatch_researcher` tool, bound to this run's session."""

    class DispatchResearcherInput(BaseModel):
        """Model-facing input schema for `dispatch_researcher`. One researcher per call."""

        model_config = ConfigDict(extra="forbid")

        label: str = Field(
            description="A short human label for this researcher, shown in the roster (2-5 words)."
        )
        objective: str = Field(
            description="What this researcher must find out. It does not see the wider "
            "research question, so state the angle in enough detail to work from."
        )
        output_format: str = Field(
            description="The shape of the findings you want back — the sections, the level "
            "of detail, and what a complete answer on this angle looks like."
        )
        boundaries: str = Field(
            description="What this researcher must NOT research, so its angle does not "
            "overlap the others you dispatched."
        )

    @tool(
        DISPATCH_RESEARCHER_TOOL_NAME,
        args_schema=DispatchResearcherInput,
        response_format="content_and_artifact",
    )
    async def dispatch_researcher(
        label: str, objective: str, output_format: str, boundaries: str
    ) -> tuple[str, str]:
        """Start one researcher on one angle, in the background.

        Returns immediately with `researcher/N (label) started` — the findings arrive later,
        as their own message headed `[researcher/N - label] returned:`. Dispatch one call per
        angle; you may fire several in a single turn and more after a return lands.
        """
        # `session` is `None` only in registry tests that inspect names and schemas; a real
        # run always builds the lead toolset from inside `Session.run`.
        assert session is not None
        result = session.dispatch(label, objective, output_format, boundaries)
        return result, result

    return dispatch_researcher


def build_submit_report_tool(session: "Session | None") -> BaseTool:
    """Build the `submit_report` tool, bound to this run's session."""

    class SubmitReportInput(BaseModel):
        """Model-facing input schema for `submit_report`. One complete answer per run."""

        model_config = ConfigDict(extra="forbid")

        answer: str = Field(
            description="Your complete final answer in markdown, with `[Sn]` citation "
            "markers inline, following the output rules in your instructions."
        )

    @tool(
        SUBMIT_REPORT_TOOL_NAME,
        args_schema=SubmitReportInput,
        response_format="content_and_artifact",
    )
    async def submit_report(answer: str) -> tuple[str, str]:
        """Deliver your complete final answer and end research.

        Call this only when no researcher is still running and you are satisfied with the
        coverage. The answer you pass here IS the report — nothing you say in chat is.
        """
        assert session is not None
        result = session.submit(answer)
        return result, result

    return submit_report
