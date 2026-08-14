"""The pre-research clarification tool, gated by the agent's `interrupt_on` middleware.

`ask_user` never executes on the real path: `harness/agent.py` registers it in `interrupt_on`
with `allowed_decisions=["respond"]`, so `HumanInTheLoopMiddleware` intercepts the call,
collects the developer's answer, and synthesizes the `ToolMessage` itself. The function exists
only to give the model a name, description and schema to call.

Its body therefore raises rather than returning a stand-in answer. Reaching it means the
`interrupt_on` registration is gone, and a run that silently answered its own clarifying
question would research the wrong thing without ever saying so.
"""

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig

ASK_USER_TOOL_NAME = "ask_user"


def build_ask_user_tool(config: HarnessConfig) -> BaseTool:
    """Build the `ask_user` tool.

    `config` is unused today but kept: every sibling tool builder takes it, and this signature
    is frozen.
    """

    class AskUserInput(BaseModel):
        """Model-facing input schema for the `ask_user` tool. One question per call."""

        model_config = ConfigDict(extra="forbid")

        question: str = Field(
            description="One clarifying question to ask the developer about the research "
            "question, before researching begins."
        )

    @tool(ASK_USER_TOOL_NAME, args_schema=AskUserInput, response_format="content_and_artifact")
    async def ask_user(question: str) -> tuple[str, str]:
        """Ask the developer one clarifying question about the research question.

        Use this only before you begin researching, to resolve a genuine ambiguity that
        would change what you research. Once you have started searching, do not call this
        tool — make your best judgment call instead.
        """
        raise RuntimeError(
            f"{ASK_USER_TOOL_NAME} was executed instead of interrupting the run — "
            "harness/agent.py's interrupt_on registration is missing."
        )

    return ask_user
