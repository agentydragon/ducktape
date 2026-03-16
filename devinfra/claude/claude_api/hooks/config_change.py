"""Pydantic models for Claude Code ConfigChange hook."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import HookInputBase, HookOutputBase


class ConfigChangeSource(StrEnum):
    USER_SETTINGS = "user_settings"
    PROJECT_SETTINGS = "project_settings"
    LOCAL_SETTINGS = "local_settings"
    POLICY_SETTINGS = "policy_settings"
    SKILLS = "skills"


class ConfigChangeInput(HookInputBase):
    hook_event_name: Literal["ConfigChange"] = "ConfigChange"
    source: ConfigChangeSource
    file_path: Path | None = Field(default=None, description="Path to changed file; absent for some source types")


class ConfigChangeOutput(HookOutputBase):
    pass
