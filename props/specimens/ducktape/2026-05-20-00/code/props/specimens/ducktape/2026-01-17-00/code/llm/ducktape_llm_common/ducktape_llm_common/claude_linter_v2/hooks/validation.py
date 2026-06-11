"""Validate hook responses."""

import logging
from typing import Any

from ...claude_code_api import HookEventName
from ...claude_outcomes import (
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
from .exceptions import HookBugError

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
        raise HookBugError(f"Unknown hook type: {hook_type}")

    valid_types = VALID_OUTCOMES[hook_type]
    if not isinstance(outcome, valid_types):
        raise HookBugError(
            f"Invalid outcome type for {hook_type.value}: "
            f"got {type(outcome).__name__}, "
            f"expected one of {[t.__name__ for t in valid_types]}"
        )

    # Semantic checks
    _validate_outcome_semantics(hook_type, outcome)

    logger.debug(f"✓ Valid {hook_type} outcome: {type(outcome).__name__}")


def _validate_outcome_semantics(hook_type: HookEventName, outcome: HookOutcome) -> None:
    """Validate semantic correctness of outcomes."""
    # PreTool validations
    if isinstance(outcome, PreToolDeny):
        if not outcome.llm_message:
            raise HookBugError("PreToolDeny must have llm_message")
        if len(outcome.llm_message) < 10:
            raise HookBugError(f"PreToolDeny message too short: '{outcome.llm_message}'")

    # PostTool validations
    if isinstance(outcome, PostToolNotifyLLM) and not outcome.llm_message:
        raise HookBugError("PostToolNotifyLLM must have llm_message")

    # Stop validations
    if isinstance(outcome, StopPrevent) and not outcome.llm_message:
        raise HookBugError("StopPrevent must explain what Claude needs to do")

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
        raise HookBugError(f"{hook_type.value}: decision=block requires reason")

    if response_data.get("stopReason") and response_data.get("continue", True):
        raise HookBugError(f"{hook_type.value}: stopReason only valid when continue=False")

    # Hook-specific validation
    if hook_type == HookEventName.PRE_TOOL_USE and response_data.get("decision") not in [None, "approve", "block"]:
        raise HookBugError(f"Invalid PreToolUse decision: {response_data.get('decision')}")

    logger.debug(f"✓ Final response valid for {hook_type.value}")
