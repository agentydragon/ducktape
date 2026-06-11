"""Hook input models for Claude Code events."""

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from claude_hooks.tool_models import (
    BashInput,
    EditInput,
    GlobInput,
    GrepInput,
    LSInput,
    MultiEditInput,
    ReadInput,
    TaskInput,
    WriteInput,
)

# Map tool names to their input classes - shared by both PreToolInput and PostToolInput
TOOL_INPUT_MAP: dict[str, type[BaseModel]] = {
    "Write": WriteInput,
    "Edit": EditInput,
    "MultiEdit": MultiEditInput,
    "Read": ReadInput,
    "Bash": BashInput,
    "Glob": GlobInput,
    "Grep": GrepInput,
    "Task": TaskInput,
    "LS": LSInput,
}


class CompactTrigger(str, Enum):
    """Trigger type for compaction (manual or auto-triggered)."""

    MANUAL = "manual"
    AUTO = "auto"


class BaseHookInput(BaseModel):
    """Base class for all Claude Code hook inputs with common fields."""

    session_id: UUID
    transcript_path: Path = Field(description="Path to conversation JSON")
    cwd: Path = Field(description="Current working directory when hook is invoked")
    hook_event_name: str


class PreToolInput(BaseHookInput):
    hook_event_name: Literal["PreToolUse"] = "PreToolUse"
    tool_name: str
    tool_input: Any  # Will be parsed by field_validator based on tool_name

    @field_validator("tool_input")
    @classmethod
    def parse_tool_input_based_on_tool_name(cls, v: Any, info: ValidationInfo) -> Any:
        """Parse tool_input with the correct class based on tool_name."""
        # Get tool_name from the data being validated
        tool_name = info.data.get("tool_name")
        if not tool_name:
            return v

        if tool_class := TOOL_INPUT_MAP.get(tool_name):
            # Parse directly with the correct class based on tool_name
            try:
                return tool_class.model_validate(v)
            except Exception:
                # If validation fails, leave as dict for graceful error handling
                return v

        # For unknown tools (like MCP), return as dict
        return v


class PostToolInput(BaseHookInput):
    hook_event_name: Literal["PostToolUse"] = "PostToolUse"
    tool_name: str
    tool_input: Any  # Will be parsed by field_validator based on tool_name
    tool_response: dict[str, Any] | list[dict[str, Any]] | str | None = None

    @field_validator("tool_input")
    @classmethod
    def parse_tool_input_based_on_tool_name(cls, v: Any, info: ValidationInfo) -> Any:
        """Parse tool_input with the correct class based on tool_name."""
        # Get tool_name from the data being validated
        tool_name = info.data.get("tool_name")
        if not tool_name:
            return v

        if tool_class := TOOL_INPUT_MAP.get(tool_name):
            # Parse directly with the correct class based on tool_name
            try:
                return tool_class.model_validate(v)
            except Exception:
                # If validation fails, leave as dict for graceful error handling
                return v

        # For unknown tools (like MCP), return as dict
        return v


class UserPromptSubmitInput(BaseHookInput):
    hook_event_name: Literal["UserPromptSubmit"] = "UserPromptSubmit"
    prompt: str


class StopInput(BaseHookInput):
    hook_event_name: Literal["Stop"] = "Stop"
    stop_hook_active: bool


class SubagentStopInput(BaseHookInput):
    hook_event_name: Literal["SubagentStop"] = "SubagentStop"
    stop_hook_active: bool


class NotificationInput(BaseHookInput):
    hook_event_name: Literal["Notification"] = "Notification"
    message: str


class PreCompactInput(BaseHookInput):
    hook_event_name: Literal["PreCompact"] = "PreCompact"
    trigger: CompactTrigger
    custom_instructions: str = ""


def _generate_invocation_id() -> str:
    """Generate hook invocation ID in format HI_<UUID>."""
    return f"HI_{uuid4()}"


class HookContext(BaseModel):
    hook_name: str
    hook_event: str
    execution_start: datetime = Field(default_factory=datetime.now)
    session_id: UUID
    cwd: Path
    environment: dict[str, str] | None = None
    invocation_id: str = Field(default_factory=_generate_invocation_id)
