"""Pydantic models for Claude Code InstructionsLoaded hook."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import HookInputBase


class InstructionsMemoryType(StrEnum):
    USER = "User"
    PROJECT = "Project"
    LOCAL = "Local"
    MANAGED = "Managed"


class InstructionsLoadReason(StrEnum):
    SESSION_START = "session_start"
    NESTED_TRAVERSAL = "nested_traversal"
    PATH_GLOB_MATCH = "path_glob_match"
    INCLUDE = "include"
    COMPACT = "compact"


class InstructionsLoadedInput(HookInputBase):
    hook_event_name: Literal["InstructionsLoaded"] = "InstructionsLoaded"
    file_path: Path
    memory_type: InstructionsMemoryType
    load_reason: InstructionsLoadReason
    globs: list[str] = Field(
        default_factory=list,
        description="Path glob patterns from paths: frontmatter; present for path_glob_match loads",
    )
    trigger_file_path: Path | None = Field(
        default=None, description="File whose access triggered this load (lazy/glob loads)"
    )
    parent_file_path: Path | None = Field(
        default=None, description="Parent instruction file that included this one (include loads)"
    )
