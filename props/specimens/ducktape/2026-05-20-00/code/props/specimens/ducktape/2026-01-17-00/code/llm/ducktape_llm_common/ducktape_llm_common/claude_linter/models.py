"""Pydantic models for Claude Code hook request/response structures.

These models represent the JSON data exchanged between Claude Code and hook commands.
"""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Import shared types from claude_code_api
from ..claude_code_api import CamelCaseModel, SessionID, ToolCall, _parse_tool_call


class LinterHookResponse(CamelCaseModel):
    """Response sent back to Claude Code from the linter hook command.

    Hook can communicate decisions via:
    1. Exit codes: 0=success, 2=blocking error, other=non-blocking error
    2. JSON output (this model) for fine-grained control

    When using JSON output, exit code should be 0.
    """

    decision: Literal["approve", "block"] | None = None
    reason: str | None = None
    continue_: bool = Field(default=True, serialization_alias="continue")
    stop_reason: str | None = None
    suppress_output: bool = False

    model_config = ConfigDict(use_enum_values=True, arbitrary_types_allowed=True)


class PatchLine(CamelCaseModel):
    """Represents a hunk in a structured patch (unified diff format).

    This appears in tool responses to show exactly what changed in a file.
    """

    old_start: int = Field(description="Starting line number in the original file.")
    old_lines: int = Field(description="Number of lines in the original file covered by this hunk.")
    new_start: int = Field(description="Starting line number in the modified file.")
    new_lines: int = Field(description="Number of lines in the modified file covered by this hunk.")
    lines: list[str] = Field(description="The actual diff lines, including context and changes with +/- prefixes.")


class ToolResponse(CamelCaseModel):
    """Response from tool execution (post-hook only).

    Contains information about what actually happened during tool execution,
    including any modifications made by the tool or by auto-fixes.
    """

    file_path: str | None = Field(None, description="Path to the file that was operated on.")
    old_string: str | None = Field(None, description="Original text that was replaced (for Edit operations).")
    new_string: str | None = Field(None, description="New text that replaced the old (for Edit operations).")
    original_file: str | None = Field(None, description="Original file content before any modifications.")
    structured_patch: list[PatchLine] | None = Field(
        None, description="Detailed patch information showing all changes made."
    )
    user_modified: bool = Field(False, description="Tracks if file was externally modified after tool execution.")
    replace_all: bool = Field(False, description="Whether all occurrences were replaced (for Edit operations).")


class HookRequest(BaseModel):
    """Request sent from Claude Code to a hook command."""

    session_id: SessionID | None = None
    transcript_path: Path | None = None
    hook_event_name: Literal["PreToolUse", "PostToolUse"] | None = None
    tool_call: ToolCall
    tool_response: ToolResponse | dict[str, Any] | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @model_validator(mode="before")
    @classmethod
    def restructure_tool_call(cls, data: Any) -> Any:
        """Restructure wire format {tool_name, tool_input} into {tool_call}."""
        if isinstance(data, dict) and "tool_name" in data and "tool_input" in data:
            tool_input = data.get("tool_input", {})
            if isinstance(tool_input, dict):
                data["tool_call"] = _parse_tool_call(data["tool_name"], tool_input)
        return data
