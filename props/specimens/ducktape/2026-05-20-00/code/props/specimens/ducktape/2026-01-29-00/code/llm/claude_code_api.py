"""Pydantic models for Claude Code hook API requests and responses per Anthropic spec."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, NewType
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

SessionID = NewType("SessionID", UUID)


class HookDecision(StrEnum):
    """Hook decision values per Claude Code API."""

    APPROVE = "approve"
    BLOCK = "block"


class CamelCaseModel(BaseModel):
    """Base model for Claude Code responses that use camelCase wire format."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class EditOperation(BaseModel):
    """Individual edit operation for MultiEdit tool."""

    old_string: str
    new_string: str
    replace_all: bool = False


# Typed tool call classes with discriminator


class EditToolCall(BaseModel):
    """Edit tool call."""

    tool_name: Literal["Edit"] = "Edit"
    file_path: Path
    old_string: str
    new_string: str
    replace_all: bool = False


class WriteToolCall(BaseModel):
    """Write tool call."""

    tool_name: Literal["Write"] = "Write"
    file_path: Path
    content: str


class ReadToolCall(BaseModel):
    """Read tool call."""

    tool_name: Literal["Read"] = "Read"
    file_path: Path
    offset: int | None = None
    limit: int | None = None


class MultiEditToolCall(BaseModel):
    """MultiEdit tool call."""

    tool_name: Literal["MultiEdit"] = "MultiEdit"
    file_path: Path
    edits: list[EditOperation]


class BashToolCall(BaseModel):
    """Bash tool call."""

    tool_name: Literal["Bash"] = "Bash"
    command: str
    description: str | None = None
    timeout: int | None = None
    run_in_background: bool = False


class GlobToolCall(BaseModel):
    """Glob tool call."""

    tool_name: Literal["Glob"] = "Glob"
    pattern: str
    path: Path | None = None


class GrepToolCall(BaseModel):
    """Grep tool call."""

    tool_name: Literal["Grep"] = "Grep"
    pattern: str
    path: Path | None = None
    output_mode: str | None = None
    glob: str | None = None


class TaskToolCall(BaseModel):
    """Task tool call."""

    tool_name: Literal["Task"] = "Task"
    prompt: str
    description: str
    subagent_type: str
    model: str | None = None
    resume: str | None = None


class MCPToolCall(BaseModel):
    """Catch-all for MCP and unrecognized tools."""

    tool_name: str
    model_config = ConfigDict(extra="allow")


# Known tool names for discriminator
_KNOWN_TOOLS = {"Edit", "Write", "Read", "MultiEdit", "Bash", "Glob", "Grep", "Task"}


def _tool_call_discriminator(v: Any) -> str:
    """Discriminator function for ToolCall union."""
    if isinstance(v, dict):
        tool_name = v.get("tool_name", "")
        return tool_name if tool_name in _KNOWN_TOOLS else "mcp"
    return getattr(v, "tool_name", "mcp") if getattr(v, "tool_name", "") in _KNOWN_TOOLS else "mcp"


# Union of all tool call types with discriminator
ToolCall = Annotated[
    Annotated[EditToolCall, Tag("Edit")]
    | Annotated[WriteToolCall, Tag("Write")]
    | Annotated[ReadToolCall, Tag("Read")]
    | Annotated[MultiEditToolCall, Tag("MultiEdit")]
    | Annotated[BashToolCall, Tag("Bash")]
    | Annotated[GlobToolCall, Tag("Glob")]
    | Annotated[GrepToolCall, Tag("Grep")]
    | Annotated[TaskToolCall, Tag("Task")]
    | Annotated[MCPToolCall, Tag("mcp")],
    Discriminator(_tool_call_discriminator),
]

_ToolCallAdapter: TypeAdapter[ToolCall] = TypeAdapter(ToolCall)


def _parse_tool_call(tool_name: str, tool_input: dict[str, Any]) -> ToolCall:
    """Parse tool_name + tool_input into a typed ToolCall."""
    return _ToolCallAdapter.validate_python({"tool_name": tool_name, **tool_input})


class HookEventName(StrEnum):
    """Valid hook event names in Claude Code."""

    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    NOTIFICATION = "Notification"
    STOP = "Stop"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"


class BaseHookRequest(BaseModel):
    """Base request for all hooks."""

    session_id: SessionID
    transcript_path: Path | None = None
    hook_event_name: str

    model_config = ConfigDict(populate_by_name=True)


class PreToolUseRequest(BaseHookRequest):
    """PreToolUse hook request."""

    hook_event_name: Literal["PreToolUse"] = "PreToolUse"
    tool_call: ToolCall

    @model_validator(mode="before")
    @classmethod
    def restructure_tool_call(cls, data: Any) -> Any:
        """Restructure wire format {tool_name, tool_input} into {tool_call}."""
        if isinstance(data, dict) and "tool_name" in data and "tool_input" in data:
            tool_input = data.get("tool_input", {})
            if isinstance(tool_input, dict):
                data["tool_call"] = _parse_tool_call(data["tool_name"], tool_input)
        return data


class PostToolUseRequest(BaseHookRequest):
    """PostToolUse hook request."""

    hook_event_name: Literal["PostToolUse"] = "PostToolUse"
    tool_call: ToolCall
    tool_response: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def restructure_tool_call(cls, data: Any) -> Any:
        """Restructure wire format {tool_name, tool_input} into {tool_call}."""
        if isinstance(data, dict) and "tool_name" in data and "tool_input" in data:
            tool_input = data.get("tool_input", {})
            if isinstance(tool_input, dict):
                data["tool_call"] = _parse_tool_call(data["tool_name"], tool_input)
        return data


class NotificationRequest(BaseHookRequest):
    """Notification hook request."""

    hook_event_name: Literal["Notification"] = "Notification"
    message: str = ""
    title: str | None = None


class StopRequest(BaseHookRequest):
    """Stop hook request."""

    hook_event_name: Literal["Stop"] = "Stop"
    stop_hook_active: bool = True


class SubagentStopRequest(BaseHookRequest):
    """SubagentStop hook request."""

    hook_event_name: Literal["SubagentStop"] = "SubagentStop"
    stop_hook_active: bool = True


class PreCompactRequest(BaseHookRequest):
    """PreCompact hook request."""

    hook_event_name: Literal["PreCompact"]
    trigger: Literal["manual", "auto"]
    custom_instructions: str


class BaseResponse(CamelCaseModel):
    """
    Base response for all hooks.

    Per Anthropic docs section "Common JSON Fields":
    - continue: Whether Claude should continue (default: true)
    - stopReason: Message shown when continue is false (shown to user, NOT Claude)
    - suppressOutput: Hide stdout from transcript mode
    """

    # continue_ needs explicit alias since to_camel("continue_") -> "continue_" not "continue"
    continue_: bool = Field(True, alias="continue")
    stop_reason: str | None = Field(None, description="Message shown to USER when continue is false")
    suppress_output: bool | None = None

    @field_validator("stop_reason")
    @classmethod
    def validate_stop_reason(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v and info.data and info.data.get("continue_", True):
            raise ValueError("stopReason only valid when continue=False")
        return v


class PreToolResponse(BaseResponse):
    """
    PreToolUse hook response.

    Per docs:
    - decision="approve": Bypasses permission system
    - decision="block": Prevents tool call execution
    - undefined: Uses default permission flow
    """

    decision: HookDecision | None = Field(None)
    reason: str | None = Field(None, description="Explanation for decision")

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str | None, info: ValidationInfo) -> str | None:
        if info.data and info.data.get("decision") == HookDecision.BLOCK and not v:
            raise ValueError("reason required when decision=block")
        return v


class PostToolResponse(BaseResponse):
    """
    PostToolUse hook response.

    Per docs:
    - decision="block": Automatically prompts Claude with reason
    - undefined: No action taken
    """

    decision: Literal[HookDecision.BLOCK] | None = Field(None)
    reason: str | None = Field(
        None, description="Explanation for decision - automatically prompts Claude if decision=block"
    )


class StopResponse(BaseResponse):
    """
    Stop/SubagentStop hook response.

    Per docs:
    - decision="block": Prevents Claude from stopping
    - undefined: Allows Claude to stop
    """

    decision: Literal[HookDecision.BLOCK] | None = Field(None)
    reason: str | None = Field(None, description="Must provide reason if blocking Claude from stopping")

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str | None, info: ValidationInfo) -> str | None:
        if info.data and info.data.get("decision") == "block" and not v:
            raise ValueError("reason required when decision=block")
        return v


# Hook-specific output types for the new unified response model


class PreToolHookOutput(CamelCaseModel):
    """PreToolUse hook-specific output."""

    permission_decision: Literal["allow", "deny", "ask"] | None = None
    permission_decision_reason: str | None = None
    updated_input: dict[str, Any] | None = None


class PostToolHookOutput(CamelCaseModel):
    """PostToolUse hook-specific output."""

    decision: Literal["block"] | None = None
    reason: str | None = None
    additional_context: str | None = None


class StopHookOutput(CamelCaseModel):
    """Stop hook-specific output."""

    decision: Literal["block"] | None = None
    reason: str | None = None


class SubagentStopHookOutput(CamelCaseModel):
    """SubagentStop hook-specific output."""

    decision: Literal["block"] | None = None
    reason: str | None = None


class UserPromptHookOutput(CamelCaseModel):
    """UserPromptSubmit hook-specific output."""

    decision: Literal["block"] | None = None
    reason: str | None = None
    additional_context: str | None = None


class PermissionDecisionPayload(CamelCaseModel):
    """Permission decision payload for PermissionRequest hooks."""

    behavior: Literal["allow", "deny"]
    message: str | None = None
    updated_input: dict[str, Any] | None = None


class PermissionHookOutput(CamelCaseModel):
    """PermissionRequest hook-specific output."""

    decision: PermissionDecisionPayload | None = None


class SessionStartHookOutput(CamelCaseModel):
    """SessionStart hook-specific output."""

    additional_context: str | None = None


# Union of all hook-specific outputs
HookSpecificOutput = (
    PreToolHookOutput
    | PostToolHookOutput
    | StopHookOutput
    | SubagentStopHookOutput
    | UserPromptHookOutput
    | PermissionHookOutput
    | SessionStartHookOutput
    | None
)


class HookResponse(CamelCaseModel):
    """Unified hook response model.

    This is the new-style response that uses hook_specific_output instead of
    the legacy decision/reason fields at the top level.
    """

    continue_: bool = Field(default=True, serialization_alias="continue")
    stop_reason: str | None = None
    hook_specific_output: HookSpecificOutput = None


HookRequest = (
    PreToolUseRequest | PostToolUseRequest | NotificationRequest | StopRequest | SubagentStopRequest | PreCompactRequest
)
