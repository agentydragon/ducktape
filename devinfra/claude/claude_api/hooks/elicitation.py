"""Pydantic models for Claude Code Elicitation and ElicitationResult hooks."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase


class ElicitationAction(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


class ElicitationMode(StrEnum):
    FORM = "form"
    URL = "url"


class ElicitationInput(HookInputBase):
    hook_event_name: Literal["Elicitation"] = "Elicitation"
    mcp_server_name: str
    message: str
    mode: ElicitationMode | None = Field(default=None, description="'form' or 'url'; absent when neither applies")
    url: str | None = Field(default=None, description="Present when mode='url'")
    requested_schema: dict[str, Any] | None = Field(
        default=None, description="Form field schema; present when mode='form'"
    )


class ElicitationHookSpecificOutput(CamelModel):
    hook_event_name: Literal["Elicitation"] = "Elicitation"
    action: ElicitationAction | None = None
    content: dict[str, Any] | None = None


class ElicitationOutput(HookOutputBase):
    hook_specific_output: ElicitationHookSpecificOutput | None = None


class ElicitationResultInput(HookInputBase):
    hook_event_name: Literal["ElicitationResult"] = "ElicitationResult"
    mcp_server_name: str
    action: ElicitationAction
    content: dict[str, Any] | None = Field(default=None, description="Form field values; present when action='accept'")
    mode: ElicitationMode | None = Field(default=None, description="'form' or 'url'")
    elicitation_id: str | None = None


class ElicitationResultHookSpecificOutput(CamelModel):
    hook_event_name: Literal["ElicitationResult"] = "ElicitationResult"
    action: ElicitationAction | None = None
    content: dict[str, Any] | None = None


class ElicitationResultOutput(HookOutputBase):
    hook_specific_output: ElicitationResultHookSpecificOutput | None = None
