"""The pre-research clarification tool, gated by the agent's `interrupt_on` middleware.

`ask_user` is never actually executed on the real path (Phase 4 plan, settled fact 3):
`harness/agent.py` registers it in `interrupt_on` with `allowed_decisions=["respond"]`, so
`HumanInTheLoopMiddleware` intercepts the call, prints/collects the developer's answer, and
synthesizes the tool's `ToolMessage` result itself before the function body ever runs. The
function below exists only to give the model a name, description, and schema to call — its
body is dead code on that path. If the `interrupt_on` entry for this tool were ever removed,
the string it returns is the honest in-band answer: no developer input was captured.
"""

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from harness.config import HarnessConfig

ASK_USER_TOOL_NAME = "ask_user"


def build_ask_user_tool(config: HarnessConfig) -> BaseTool:
    """Build the `ask_user` tool.

    `config` is unused today but kept — every sibling tool builder takes it, and the
    plan freezes this signature.
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
        content = "No answer was captured: the developer was never asked."
        return content, question

    return ask_user
