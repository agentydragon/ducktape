from __future__ import annotations

from enum import Enum, StrEnum
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field


class ApprovalToolCall(BaseModel):
    name: str
    call_id: str
    args_json: str | None  # JSON string, or None for no arguments


class ApprovalRequest(BaseModel):
    tool_key: str
    tool_call: ApprovalToolCall


class PolicyDecision(str, Enum):
    """Policy decision outcomes for approval evaluation."""

    ALLOW = "allow"
    ASK = "ask"
    DENY_CONTINUE = "deny_continue"
    DENY_ABORT = "deny_abort"


class WellKnownServers(StrEnum):
    """Well-known MCP server names for approval policies."""

    UI = "ui"
    APPROVAL_POLICY = "approval_policy"
    RESOURCES = "resources"
    SEATBELT_EXEC = "seatbelt_exec"


class WellKnownTools(StrEnum):
    SEND_MESSAGE = "send_message"
    END_TURN = "end_turn"
    GET_STATUS = "get_status"
    PROPOSE = "propose"
    WITHDRAW = "withdraw"
    SANDBOX_EXEC = "sandbox_exec"


class ApprovalContext(BaseModel):
    """Context passed to approval policy."""

    server: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    seatbelt_policy: Any = None


def load_default_policy_source() -> str:
    """Load the packaged default approval policy source code as text."""
    return resources.files("agent_server.policies").joinpath("default_policy.py").read_text(encoding="utf-8")
