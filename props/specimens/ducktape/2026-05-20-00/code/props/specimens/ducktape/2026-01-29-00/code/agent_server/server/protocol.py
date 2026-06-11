from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from mcp import types as mcp_types
from pydantic import BaseModel, ConfigDict, Field

from agent_core.events import ToolCall, UserText
from agent_server.models.policy_error import PolicyTestsSummary
from agent_server.models.proposal_status import ProposalStatus
from agent_server.server.bus import MimeType

# --------------------------
# Core state
# --------------------------


class SessionState(BaseModel):
    session_id: str
    version: str
    capabilities: list[str] = []
    last_event_id: int | None = None

    model_config = ConfigDict(extra="forbid")


class ApprovalBrief(BaseModel):
    call_id: str
    tool_key: str
    args: dict = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ProposalInfo(BaseModel):
    """Policy proposal information for UI (no policy content included)."""

    id: str
    status: ProposalStatus
    # Optional docstring extracted from class ApprovalPolicy
    docstring: str | None = None
    # Optional structured test results summary
    tests: PolicyTestsSummary | None = None

    model_config = ConfigDict(extra="forbid")


class ApprovalPolicyInfo(BaseModel):
    """Current approval policy state."""

    content: str
    version: int
    proposals: list[ProposalInfo] = []

    model_config = ConfigDict(extra="forbid")


class AgentStatus(StrEnum):
    """Agent execution status."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    ABORTING = "aborting"
    FINISHED = "finished"
    ERROR = "error"


# --------------------------
# Transcript items
# --------------------------


class FunctionCallOutput(BaseModel):
    type: Literal["function_call_output"] = "function_call_output"
    call_id: str
    result: mcp_types.CallToolResult

    model_config = ConfigDict(extra="forbid")


class UiMessagePayload(BaseModel):
    mime: MimeType = MimeType.MARKDOWN
    content: str
    model_config = ConfigDict(extra="forbid")


class UiMessageEvt(BaseModel):
    type: Literal["ui_message"] = "ui_message"
    message: UiMessagePayload
    model_config = ConfigDict(extra="forbid")


class UiEndTurnEvt(BaseModel):
    type: Literal["ui_end_turn"] = "ui_end_turn"
    model_config = ConfigDict(extra="forbid")


## Client -> Server message types are no longer used; REST handles mutations.

# --------------------------
# Server -> Client messages
# --------------------------
# NOTE: WebSocket has been replaced by MCP. These event types are used
# for internal state management (reducer).


# ServerMessage union - Used for reducer
ServerMessage = Annotated[
    UserText  # Reducer
    | ToolCall  # Reducer
    | FunctionCallOutput  # Reducer
    | UiMessageEvt  # Reducer
    | UiEndTurnEvt,  # Reducer
    Field(discriminator="type"),
]
