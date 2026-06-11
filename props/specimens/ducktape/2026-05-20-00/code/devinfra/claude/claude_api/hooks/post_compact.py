"""Pydantic models for Claude Code PostCompact hook."""

from typing import Literal

from devinfra.claude.claude_api.hooks.common import HookInputBase
from devinfra.claude.claude_api.hooks.pre_compact import CompactTrigger


class PostCompactInput(HookInputBase):
    hook_event_name: Literal["PostCompact"] = "PostCompact"
    trigger: CompactTrigger
    compact_summary: str
