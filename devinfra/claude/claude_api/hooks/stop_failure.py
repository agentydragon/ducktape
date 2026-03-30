"""Pydantic models for Claude Code StopFailure hook (v2.1.87+)."""

from enum import StrEnum
from typing import Literal

from devinfra.claude.claude_api.hooks.common import HookInputBase, HookOutputBase


class StopFailureError(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    BILLING_ERROR = "billing_error"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    SERVER_ERROR = "server_error"
    UNKNOWN = "unknown"
    MAX_OUTPUT_TOKENS = "max_output_tokens"


class StopFailureInput(HookInputBase):
    hook_event_name: Literal["StopFailure"] = "StopFailure"
    error: StopFailureError
    error_details: str | None = None
    last_assistant_message: str | None = None


class StopFailureOutput(HookOutputBase):
    pass
