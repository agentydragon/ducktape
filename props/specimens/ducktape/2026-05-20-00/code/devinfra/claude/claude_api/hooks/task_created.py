"""Pydantic models for Claude Code TaskCreated hook."""

from typing import Literal

from devinfra.claude.claude_api.hooks.common import HookInputBase


class TaskCreatedInput(HookInputBase):
    hook_event_name: Literal["TaskCreated"] = "TaskCreated"
    task_id: str
    task_subject: str
    task_description: str | None = None
    teammate_name: str | None = None
    team_name: str | None = None
