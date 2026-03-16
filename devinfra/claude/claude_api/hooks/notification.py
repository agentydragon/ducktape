"""Pydantic models for Claude Code Notification hook."""

from enum import StrEnum
from typing import Literal

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


class NotificationType(StrEnum):
    PERMISSION_PROMPT = "permission_prompt"
    IDLE_PROMPT = "idle_prompt"
    AUTH_SUCCESS = "auth_success"
    ELICITATION_DIALOG = "elicitation_dialog"


class NotificationInput(HookInputBase):
    hook_event_name: Literal["Notification"] = "Notification"
    message: str
    title: str | None = None
    notification_type: NotificationType


class NotificationHookSpecificOutput(CamelModel):
    hook_event_name: Literal["Notification"] = "Notification"
    additional_context: str | None = None


class NotificationOutput(HookOutputBase):
    hook_specific_output: NotificationHookSpecificOutput | None = None
