"""Tool input models for Claude Code tools."""

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class GrepOutputMode(str, Enum):
    CONTENT = "content"
    FILES_WITH_MATCHES = "files_with_matches"
    COUNT = "count"


class EditInput(BaseModel):
    file_path: Path
    old_string: str
    new_string: str
    replace_all: bool = Field(default=False, description="Replace all occurrences of old_string")


class MultiEditInput(BaseModel):
    file_path: Path
    edits: list[dict[str, Any]]


class WriteInput(BaseModel):
    file_path: Path
    content: str


class ReadInput(BaseModel):
    file_path: Path
    limit: int | None = Field(default=None, ge=1, description="Number of lines to read")
    offset: int | None = Field(default=None, ge=0, description="Line number to start reading from")


class BashInput(BaseModel):
    command: str
    description: str | None = Field(default=None, description="Description of the command")
    timeout: int | None = Field(default=None, ge=1, description="Timeout in seconds")


class GlobInput(BaseModel):
    pattern: str
    path: Path | None = None


class GrepInput(BaseModel):
    pattern: str
    path: Path | None = None
    glob: str | None = None
    output_mode: GrepOutputMode = GrepOutputMode.FILES_WITH_MATCHES


class TaskInput(BaseModel):
    description: str
    prompt: str


class LSInput(BaseModel):
    path: Path
    ignore: list[str] | None = Field(default=None, description="List of glob patterns to ignore")


# Simple union of all possible tool inputs
ToolInput = (
    WriteInput
    | EditInput
    | MultiEditInput
    | ReadInput
    | BashInput
    | GlobInput
    | GrepInput
    | TaskInput
    | LSInput
    | dict[str, Any]  # Fallback for unknown tools
)
