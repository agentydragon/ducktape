"""Pydantic models for Claude Code Notification hook."""

from typing import Literal

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase


class NotificationInput(HookInputBase):
    hook_event_name: Literal["Notification"] = "Notification"
    message: str
    title: str | None = None
    notification_type: str  # z.string() upstream — no enum restriction


class NotificationHookSpecificOutput(CamelModel):
    hook_event_name: Literal["Notification"] = "Notification"
    additional_context: str | None = None
