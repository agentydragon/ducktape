"""Typed models for the CLI's control channel.

<protocol.md> describes the whole wire; this models the half that is request/response, because
that is the half where getting a field name or a nesting wrong fails silently. `initialize`
validates almost nothing and rejects no unknown field, so a misspelled key is answered `success`
and ignored — a model is the only thing that catches it.

The conversation channel is deliberately not modelled. The console's record of a session is the
wire, a model that dropped an unrecognised field would make the record a parse of it, and the
frames it will eventually act on (`command_lifecycle`, `system/task_*`, `result` cost and usage)
get models when the code that reads them exists.

The control channel is camelCase, which is why these carry aliases and the rest of the codebase
does not.
"""

from __future__ import annotations

import uuid as uuid_module
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ControlSubtype(StrEnum):
    SUCCESS = "success"
    ERROR = "error"


class InitializeRequest(BaseModel):
    """The handshake's `request` object — only the fields the console has a use for."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    subtype: Literal["initialize"] = "initialize"
    system_prompt: list[str] | None = Field(default=None, alias="systemPrompt")
    append_system_prompt: str | None = Field(default=None, alias="appendSystemPrompt")
    hooks: dict[str, list[dict[str, Any]]] | None = None
    sdk_mcp_servers: list[str] | None = Field(default=None, alias="sdkMcpServers")
    agents: dict[str, dict[str, Any]] | None = None
    skills: list[str] | None = None
    forward_subagent_text: bool | None = Field(default=None, alias="forwardSubagentText")
    json_schema: dict[str, Any] | None = Field(
        default=None,
        alias="jsonSchema",
        description=(
            "A bare JSON Schema. The `{type: json_schema, schema: ...}` wrapper the SDK's "
            "output-format option takes is accepted here and silently ignored."
        ),
    )


class InterruptRequest(BaseModel):
    """Abort the running turn, and optionally the prompts queued behind it."""

    subtype: Literal["interrupt"] = "interrupt"
    reason: str | None = Field(
        default=None,
        description="Forwarded to the turn's AbortSignal.reason so tools can tell a user cancel from other aborts.",
    )
    cancel_queued: bool | None = Field(
        default=None,
        description=(
            "Without it the CLI starts the next queued prompt as soon as the running one dies. "
            "Reaches only uuid-stamped commands."
        ),
    )


class ControlRequestFrame(BaseModel):
    """The outbound envelope. The CLI's own requests arrive in the same shape."""

    type: Literal["control_request"] = "control_request"
    request_id: str = Field(default_factory=lambda: f"req_{uuid_module.uuid4().hex[:12]}")
    request: dict[str, Any]


class ControlResponse(BaseModel):
    """The inner object of a `control_response` frame.

    `request_id` lives here rather than beside `response` on the frame, and it is the only
    correlation key — a responder that echoes it at the top level goes unmatched.
    """

    subtype: ControlSubtype
    request_id: str
    response: dict[str, Any] | None = None
    error: str | None = None
