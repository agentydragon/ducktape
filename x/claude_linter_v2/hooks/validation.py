"""Validate hook responses."""

import logging
from typing import Any

from llm.claude_code_api import HookEventName
from llm.claude_outcomes import (
    HookError,
    HookOutcome,
    NotificationAcknowledge,
    PostToolNotifyLLM,
    PostToolSuccess,
    PostToolSuccessWithInfo,
    PreToolApprove,
    PreToolDeny,
    StopAllow,
    StopAllowWithInfo,
    StopPrevent,
    SubagentStopAllow,
)
from x.claude_linter_v2.hooks.exceptions import HookBugError

logger = logging.getLogger(__name__)

# Valid outcome types per hook
VALID_OUTCOMES: dict[HookEventName, tuple[type[HookOutcome], ...]] = {
    HookEventName.PRE_TOOL_USE: (PreToolApprove, PreToolDeny, HookError),
    HookEventName.POST_TOOL_USE: (PostToolSuccess, PostToolSuccessWithInfo, PostToolNotifyLLM, HookError),
    HookEventName.STOP: (StopAllow, StopPrevent, StopAllowWithInfo, HookError),
    HookEventName.SUBAGENT_STOP: (SubagentStopAllow, HookError),
    HookEventName.NOTIFICATION: (NotificationAcknowledge, HookError),
}


def validate_hook_outcome(hook_type: HookEventName, outcome: HookOutcome) -> None:
    """Validate outcome is appropriate for hook type."""
    if hook_type not in VALID_OUTCOMES:
        msg = f"Unknown hook type: {hook_type}"
        raise HookBugError(msg)

    valid_types = VALID_OUTCOMES[hook_type]
    if not isinstance(outcome, valid_types):
        msg = (
            f"Invalid outcome type for {hook_type.value}: "
            f"got {type(outcome).__name__}, "
            f"expected one of {[t.__name__ for t in valid_types]}"
        )
        raise HookBugError(msg)

    # Semantic checks
    _validate_outcome_semantics(hook_type, outcome)

    logger.debug("✓ Valid %s outcome: %s", hook_type, type(outcome).__name__)


def _validate_outcome_semantics(hook_type: HookEventName, outcome: HookOutcome) -> None:
    """Validate semantic correctness of outcomes."""
    # PreTool validations
    if isinstance(outcome, PreToolDeny):
        if not outcome.llm_message:
            msg = "PreToolDeny must have llm_message"
            raise HookBugError(msg)
        if len(outcome.llm_message) < 10:
            msg = f"PreToolDeny message too short: '{outcome.llm_message}'"
            raise HookBugError(msg)

    # PostTool validations
    if isinstance(outcome, PostToolNotifyLLM) and not outcome.llm_message:
        msg = "PostToolNotifyLLM must have llm_message"
        raise HookBugError(msg)

    # Stop validations
    if isinstance(outcome, StopPrevent) and not outcome.llm_message:
        msg = "StopPrevent must explain what Claude needs to do"
        raise HookBugError(msg)

    # Check for old terminology
    if (
        hook_type == HookEventName.STOP
        and isinstance(outcome, StopPrevent | StopAllowWithInfo)
        and "session" in outcome.llm_message.lower()
        and "ending" in outcome.llm_message.lower()
    ):
        logger.warning(
            "Stop hook message mentions 'session ending' - Stop is about ending Claude's turn, not sessions!"
        )


def validate_final_response(hook_type: HookEventName, response_data: dict[str, Any]) -> None:
    """Final validation before sending to Claude."""
    # Check required fields based on hook type and response
    if response_data.get("decision") == "block" and not response_data.get("reason"):
        msg = f"{hook_type.value}: decision=block requires reason"
        raise HookBugError(msg)

    if response_data.get("stopReason") and response_data.get("continue", True):
        msg = f"{hook_type.value}: stopReason only valid when continue=False"
        raise HookBugError(msg)

    # Hook-specific validation
    if hook_type == HookEventName.PRE_TOOL_USE and response_data.get("decision") not in [None, "approve", "block"]:
        msg = f"Invalid PreToolUse decision: {response_data.get('decision')}"
        raise HookBugError(msg)

    logger.debug("✓ Final response valid for %s", hook_type.value)
