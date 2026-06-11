from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ApprovalDecision(StrEnum):
    """Policy decision outcomes as string-valued enum.

    Use these values for all policy paths (in-proc and container) to keep
    decisions consistent and type-safe.
    """

    ALLOW = "allow"
    ASK = "ask"
    DENY_CONTINUE = "deny_continue"
    DENY_ABORT = "deny_abort"


class PolicyRequest(BaseModel):
    """Input to approval policy evaluation: tool name + JSON arguments.

    Arguments are JSON-encoded as a string to enable OpenAI strict mode compatibility.
    Policy programs should parse the JSON string when they need to inspect arguments.
    None means no arguments were provided.
    """

    name: str
    arguments_json: str | None  # JSON-encoded tool arguments, or None
    model_config = ConfigDict(extra="forbid")


class PolicyResponse(BaseModel):
    """Structured decision result for approval evaluations."""

    decision: ApprovalDecision
    rationale: str


# Internal package: avoid public barrels; import explicitly where needed
