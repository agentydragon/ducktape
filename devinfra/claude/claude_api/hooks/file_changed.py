"""Pydantic models for Claude Code FileChanged hook (v2.1.87+)."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


class FileChangeEvent(StrEnum):
    CHANGE = "change"
    ADD = "add"
    UNLINK = "unlink"


class FileChangedInput(HookInputBase):
    hook_event_name: Literal["FileChanged"] = "FileChanged"
    file_path: str
    event: FileChangeEvent


class FileChangedHookSpecificOutput(CamelModel):
    hook_event_name: Literal["FileChanged"] = "FileChanged"
    watch_paths: list[str] | None = Field(default=None, description="Update watched paths")


class FileChangedOutput(HookOutputBase):
    hook_specific_output: FileChangedHookSpecificOutput | None = None
