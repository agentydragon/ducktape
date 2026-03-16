"""Pydantic models for Claude Code PermissionRequest hook."""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Discriminator, Field, Tag

from devinfra.claude.claude_api.hooks.common import CamelModel, HookInputBase, HookOutputBase

# --- PermissionSuggestion types (discriminated union on "type") ---


class PermissionBehavior(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionDestination(StrEnum):
    USER_SETTINGS = "userSettings"
    PROJECT_SETTINGS = "projectSettings"
    LOCAL_SETTINGS = "localSettings"
    SESSION = "session"
    CLI_ARG = "cliArg"


class PermissionRule(CamelModel):
    tool_name: str
    rule_content: str | None = None


class AddRulesSuggestion(CamelModel):
    type: Literal["addRules"] = "addRules"
    rules: list[PermissionRule]
    behavior: PermissionBehavior
    destination: PermissionDestination


class ReplaceRulesSuggestion(CamelModel):
    type: Literal["replaceRules"] = "replaceRules"
    rules: list[PermissionRule]
    behavior: PermissionBehavior
    destination: PermissionDestination


class RemoveRulesSuggestion(CamelModel):
    type: Literal["removeRules"] = "removeRules"
    rules: list[PermissionRule]
    behavior: PermissionBehavior
    destination: PermissionDestination


class PermissionModeValue(StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"
    PLAN = "plan"
    DONT_ASK = "dontAsk"


class SetModeSuggestion(CamelModel):
    type: Literal["setMode"] = "setMode"
    mode: PermissionModeValue
    destination: PermissionDestination


class AddDirectoriesSuggestion(CamelModel):
    type: Literal["addDirectories"] = "addDirectories"
    directories: list[str]
    destination: PermissionDestination


class RemoveDirectoriesSuggestion(CamelModel):
    type: Literal["removeDirectories"] = "removeDirectories"
    directories: list[str]
    destination: PermissionDestination


PermissionSuggestion = Annotated[
    Annotated[AddRulesSuggestion, Tag("addRules")]
    | Annotated[ReplaceRulesSuggestion, Tag("replaceRules")]
    | Annotated[RemoveRulesSuggestion, Tag("removeRules")]
    | Annotated[SetModeSuggestion, Tag("setMode")]
    | Annotated[AddDirectoriesSuggestion, Tag("addDirectories")]
    | Annotated[RemoveDirectoriesSuggestion, Tag("removeDirectories")],
    Discriminator("type"),
]


# --- PermissionRequest decision (discriminated union on "behavior") ---


class AllowDecision(CamelModel):
    behavior: Literal["allow"] = "allow"
    updated_input: dict[str, Any] | None = None
    updated_permissions: list[PermissionSuggestion] | None = None


class DenyDecision(CamelModel):
    behavior: Literal["deny"] = "deny"
    message: str | None = None
    interrupt: bool | None = None


PermissionRequestDecision = Annotated[
    Annotated[AllowDecision, Tag("allow")] | Annotated[DenyDecision, Tag("deny")], Discriminator("behavior")
]


# --- Hook input/output ---


class PermissionRequestInput(HookInputBase):
    hook_event_name: Literal["PermissionRequest"] = "PermissionRequest"
    tool_name: str
    tool_input: dict[str, Any]
    permission_suggestions: list[PermissionSuggestion] = Field(default_factory=list)


class PermissionRequestHookSpecificOutput(CamelModel):
    hook_event_name: Literal["PermissionRequest"] = "PermissionRequest"
    decision: PermissionRequestDecision


class PermissionRequestOutput(HookOutputBase):
    hook_specific_output: PermissionRequestHookSpecificOutput | None = None
