from __future__ import annotations

from typing import Literal

from fastmcp.tools import FunctionTool
from pydantic import ConfigDict, Field

from agent_server.server.bus import MimeType, ServerBus, UiEndTurn, UiMessage
from mcp_infra.enhanced.server import EnhancedFastMCP
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# UI MCP server: lightweight tools to instruct the HTML UI rendering layer.
# Tools are declarative; the agent can call them to emit UI messages and to
# explicitly end a turn (as a bus message).


class SendMessageInput(OpenAIStrictModeBaseModel):
    """Input for send_message tool.

    Fields:
    - mime: MIME type for the message content. Currently only 'text/markdown' is supported.
      Use markdown formatting for rich text (headers, lists, code blocks, etc.).
    - content: The message content to display in the UI. Supports full markdown syntax including
      code blocks, lists, tables, and inline formatting.
    """

    # Use Literal instead of MimeType enum to avoid $ref with default value
    # OpenAI strict mode doesn't allow $ref with additional keywords (including default)
    mime: Literal["text/markdown"] = "text/markdown"
    content: str = Field(
        description="The message content to display in the UI. Supports full markdown syntax including "
        "code blocks, lists, tables, and inline formatting."
    )
    model_config = ConfigDict(extra="forbid")


class EndTurnInput(OpenAIStrictModeBaseModel):
    """Empty input for end_turn tool."""

    model_config = ConfigDict(extra="forbid")


class UiServer(EnhancedFastMCP):
    """UI MCP server with typed tool access.

    Subclasses EnhancedFastMCP and adds typed tool attributes for accessing
    tool names. This is the single source of truth - no string literals elsewhere.
    """

    # Tool references (assigned in __init__ after tool registration)
    send_message_tool: FunctionTool
    end_turn_tool: FunctionTool

    def __init__(self, bus: ServerBus):
        """Create a UI MCP server bound to a ServerBus.

        Args:
            bus: ServerBus for pushing UI messages and end-turn signals
        """
        super().__init__(
            "UI Server",
            instructions=(
                "UI helper: send formatted messages and end your turn via tools.\n"
                "Do not emit plain text in this UI; always use the UI tools."
            ),
        )

        # Register tools using clean pattern: tool name derived from function name
        def send_message(input: SendMessageInput) -> UiMessage:
            """Send a formatted message to the UI (markdown recommended)."""
            # Convert Literal string back to MimeType enum for the bus message
            msg = UiMessage(mime=MimeType.MARKDOWN, content=input.content)
            bus.push_message(msg)
            return msg

        self.send_message_tool = self.flat_model()(send_message)

        def end_turn(input: EndTurnInput) -> UiEndTurn:
            """Tell the UI to end the current turn."""
            bus.push_end_turn()
            return UiEndTurn()

        self.end_turn_tool = self.flat_model()(end_turn)
