from __future__ import annotations

from typing import Any, Final

from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.flat_tool import FlatTool
from mcp_infra.mcp_types import SimpleOk
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Mount prefix constant (used in compositor mount configuration)
LOOP_MOUNT_PREFIX: Final[str] = "loop"


class YieldTurnArgs(OpenAIStrictModeBaseModel):
    pass


class LoopServer(EnhancedFastMCP):
    """Loop control MCP server with typed tool access.

    Subclasses EnhancedFastMCP and adds typed tool attributes for accessing
    tool names. This is the single source of truth - no string literals elsewhere.
    """

    # Tool references (assigned in __init__ after tool registration)
    yield_turn_tool: FlatTool[Any, Any]

    def __init__(self):
        """Create a minimal loop-control MCP server.

        Tools are self-describing via MCP; avoid duplicating per-tool instructions here.
        """
        super().__init__(
            "Loop Control Server", instructions=("Loop control tools for orchestrator/agent turn coordination.")
        )

        # Register tools using clean pattern: tool name derived from function name
        async def yield_turn(_: YieldTurnArgs) -> SimpleOk:
            # Orchestration semantics are owned by the runtime/handlers. The tool is a
            # neutral signal; the agent loop interprets it as yield/end-turn.
            return SimpleOk(ok=True)

        self.yield_turn_tool = self.flat_model()(yield_turn)
