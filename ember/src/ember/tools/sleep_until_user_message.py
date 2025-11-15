from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from ..config import EnforcedSleepUntilUserMessagePolicy, LegacySleepUntilUserMessagePolicy, SleepUntilUserMessagePolicy
from ..matrix_client import ConversationStatus
from ..tool_execution import ToolPayload, ToolSpec


class ConversationStatusProvider(Protocol):
    async def get_conversation_status(self) -> ConversationStatus: ...


class SleepUntilUserMessageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SleepUntilUserMessageResult(BaseModel):
    status: Literal["waiting_for_matrix", "rejected"]
    reason: str | None = None
    model_config = ConfigDict(extra="forbid")


def build_spec(
    on_sleep: Callable[[], None], provider: ConversationStatusProvider, policy: SleepUntilUserMessagePolicy
) -> ToolSpec:
    async def handler(_: SleepUntilUserMessageArgs) -> ToolPayload:
        if isinstance(policy, EnforcedSleepUntilUserMessagePolicy):
            status = await provider.get_conversation_status()
            violation = _evaluate_enforced_policy(status, policy)
            if violation is not None:
                return SleepUntilUserMessageResult(status="rejected", reason=violation)

        on_sleep()
        return SleepUntilUserMessageResult(status="waiting_for_matrix")

    description = _build_description(policy)

    return ToolSpec(name="sleep_until_user_message", description=description, handler=handler)


def _evaluate_enforced_policy(status: ConversationStatus, policy: EnforcedSleepUntilUserMessagePolicy) -> str | None:
    now = datetime.now(timezone.utc)

    user_ts = status.last_user_message_at
    agent_ts = status.last_agent_message_at

    if user_ts is not None and (agent_ts is None or agent_ts < user_ts):
        return "You must respond to the user before sleeping."

    if agent_ts is None:
        return "Send an update to the user before sleeping."

    if (now - agent_ts) > policy.timeout:
        return "Your last update is too old. Provide a fresh update before sleeping."

    return None


def _build_description(policy: SleepUntilUserMessagePolicy) -> str:
    base = "Suspend yourself until a new user Matrix message arrives. Use this when all tasks are complete or blocked."
    if isinstance(policy, LegacySleepUntilUserMessagePolicy):
        return base

    assert isinstance(policy, EnforcedSleepUntilUserMessagePolicy)
    window_seconds = int(policy.timeout.total_seconds())
    return (
        f"{base} Calls are rejected unless you have already replied to the last user "
        f"message and your most recent reply is no older than {window_seconds} seconds."
    )
