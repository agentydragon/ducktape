"""UI and approval policy step classes for agent_server tests.

These steps depend on agent_server MCP servers and should only be used
by tests in agent_server and adgn (not props - see constraint in plan).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_core_testing.steps import EmptyArgs
from agent_server.mcp.approval_policy.engine import SetPolicyTextArgs
from agent_server.mcp.ui.server import SendMessageInput
from mcp_infra.constants import APPROVAL_ADMIN_MOUNT_PREFIX, UI_MOUNT_PREFIX

if TYPE_CHECKING:
    from agent_core_testing.responses import ResponsesFactory
    from openai_utils.model import ResponsesRequest, ResponsesResult


@dataclass
class UiEndTurnCall:
    """End turn via UI."""

    tool_name: str = "end_turn"

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        return factory.make_mcp_tool_call(UI_MOUNT_PREFIX, self.tool_name, EmptyArgs())


@dataclass
class UiSendMessageCall:
    """Send message via UI."""

    content: str
    tool_name: str = "send_message"

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        return factory.make_mcp_tool_call(UI_MOUNT_PREFIX, self.tool_name, SendMessageInput(content=self.content))


@dataclass
class ApprovalPolicyAdminSetPolicyCall:
    """Set policy via approval_policy_admin."""

    source: str

    def execute(self, req: ResponsesRequest, factory: ResponsesFactory) -> ResponsesResult:
        return factory.make_mcp_tool_call(
            APPROVAL_ADMIN_MOUNT_PREFIX, "set_policy", SetPolicyTextArgs(source=self.source)
        )
