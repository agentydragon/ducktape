"""Shared base classes and enums for Claude Code hook models.

Upstream hook semantics: ``devinfra/claude/docs/hook_semantics.md``.
"""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class PermissionMode(StrEnum):
    """Known permission_mode values (upstream field uses z.string() — may receive new values).

    The base hook input uses an arbitrary string for permission_mode, so HookInputBase
    annotates the field as str | None rather than PermissionMode | None. These constants
    exist for reference and for code that needs to compare against known values.
    """

    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"
    DONT_ASK = "dontAsk"
    AUTO = "auto"


class CamelModel(BaseModel):
    """Base for hook output models — serializes fields as camelCase, rejects extra fields."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class HookInputBase(BaseModel):
    """Common fields present in all hook inputs."""

    session_id: str
    transcript_path: Path
    cwd: Path
    # Upstream uses z.string().optional() — accept any future permission mode values.
    permission_mode: str | None = Field(
        default=None, description="Not sent by Claude Code Web for some SessionStart events"
    )
    # agent_id and agent_type are not in the base schema (v2.1.105); they appear only
    # in SubagentStart and SubagentStop inputs. We keep them here as optional so that
    # subclasses (SubagentStartInput, SubagentStopInput) can narrow them to required str.
    agent_id: str | None = Field(
        default=None, description="Subagent identifier. Present only when the hook fires from within a subagent."
    )
    agent_type: str | None = Field(
        default=None, description="Agent type name. Present in subagent context (SubagentStart/SubagentStop)."
    )
