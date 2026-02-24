"""Parse diff information from Claude tool responses."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from llm.claude_code_api import EditToolCall, MultiEditToolCall


@dataclass
class DiffLine:
    """A single line in a diff."""

    line_number: int  # Line number in final file
    content: str
    change_type: Literal["added", "removed", "context"]
    hunk_index: int  # Which hunk this belongs to


@dataclass
class DiffHunk:
    """A contiguous section of changes in a diff."""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: list[DiffLine]


@dataclass
class ParsedDiff:
    """Parsed diff information from a tool response."""

    file_path: Path
    hunks: list[DiffHunk]
    added_lines: set[int]  # Line numbers in final file
    removed_lines: set[int]  # Line numbers in original file
    context_lines: set[int]  # Unchanged lines near changes


class HunkData(BaseModel):
    """A hunk in a structured patch from Edit/MultiEdit tool response."""

    old_start: int = Field(alias="oldStart")
    old_lines: int = Field(alias="oldLines")
    new_start: int = Field(alias="newStart")
    new_lines: int = Field(alias="newLines")
    lines: list[str]


class StructuredPatchResponse(BaseModel):
    """Response from Edit or MultiEdit tool containing the structured patch."""

    structured_patch: list[HunkData] = Field(alias="structuredPatch")


def _parse_hunk(hunk_data: HunkData, hunk_idx: int) -> DiffHunk:
    """Parse a single hunk from structured patch."""
    old_start = hunk_data.old_start
    old_lines = hunk_data.old_lines
    new_start = hunk_data.new_start
    new_lines = hunk_data.new_lines
    raw_lines = hunk_data.lines

    parsed_lines = []
    current_new_line = new_start

    for raw_line in raw_lines:
        if raw_line.startswith("\\"):  # Special marker
            continue

        if raw_line.startswith("-"):
            change_type: Literal["added", "removed", "context"] = "removed"
            content = raw_line[1:]
            line_number = -1  # Not in final file
        elif raw_line.startswith("+"):
            change_type = "added"
            content = raw_line[1:]
            line_number = current_new_line
            current_new_line += 1
        else:
            # Context line (may start with space or nothing)
            change_type = "context"
            content = raw_line.removeprefix(" ")
            line_number = current_new_line
            current_new_line += 1

        parsed_lines.append(
            DiffLine(line_number=line_number, content=content, change_type=change_type, hunk_index=hunk_idx)
        )

    return DiffHunk(
        old_start=old_start, old_lines=old_lines, new_start=new_start, new_lines=new_lines, lines=parsed_lines
    )


def _parse_structured_patch(file_path: Path, structured_patch: list[HunkData]) -> ParsedDiff:
    """Parse structured patch format from Claude."""
    hunks = []
    added_lines: set[int] = set()
    removed_lines: set[int] = set()
    context_lines: set[int] = set()

    # Handle empty patch list
    if not structured_patch:
        return ParsedDiff(file_path=file_path, hunks=[], added_lines=set(), removed_lines=set(), context_lines=set())

    for hunk_idx, hunk_data in enumerate(structured_patch):
        hunk = _parse_hunk(hunk_data, hunk_idx)
        hunks.append(hunk)

        # Track line numbers
        current_new_line = hunk.new_start
        current_old_line = hunk.old_start

        for line in hunk.lines:
            if line.change_type == "added":
                added_lines.add(line.line_number)
                current_new_line += 1
            elif line.change_type == "removed":
                removed_lines.add(current_old_line)
                current_old_line += 1
            else:  # context
                context_lines.add(line.line_number)
                current_new_line += 1
                current_old_line += 1

    return ParsedDiff(
        file_path=file_path,
        hunks=hunks,
        added_lines=added_lines,
        removed_lines=removed_lines,
        context_lines=context_lines,
    )


def parse_tool_response(
    tool_call: EditToolCall | MultiEditToolCall, tool_response: dict[str, Any]
) -> ParsedDiff | None:
    """Parse Edit/MultiEdit tool response into ParsedDiff.

    Args:
        tool_call: The typed Edit or MultiEdit tool call
        tool_response: The tool response dict containing structuredPatch
    """
    try:
        response = StructuredPatchResponse.model_validate(tool_response)
    except ValidationError:
        return None  # Missing or invalid structuredPatch field

    return _parse_structured_patch(tool_call.file_path, response.structured_patch)
