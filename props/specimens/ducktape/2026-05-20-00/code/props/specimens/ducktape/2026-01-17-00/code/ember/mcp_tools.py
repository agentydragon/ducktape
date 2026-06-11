"""MCP server providing ember's tools via compositor.

This module creates an EmberCompositor that mounts:
- ember-tools: sleep_until_user_message tool
- exec: DirectExecServer (exec, read_image tools)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ConfigDict

from ember.config import (
    EnforcedSleepUntilUserMessagePolicy,
    LegacySleepUntilUserMessagePolicy,
    SleepUntilUserMessagePolicy,
)
from ember.matrix_client import ConversationStatus
from mcp_infra.compositor.server import Compositor, Mounted
from mcp_infra.enhanced.flat_mixin import FlatModelMixin
from mcp_infra.exec.direct import DirectExecServer
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel


class ConversationStatusProvider(Protocol):
    """Protocol for getting conversation status from Matrix client."""

    async def get_conversation_status(self) -> ConversationStatus: ...


class SleepUntilUserMessageInput(OpenAIStrictModeBaseModel):
    """Input for sleep_until_user_message tool (no arguments)."""

    model_config = ConfigDict(extra="forbid")


class SleepUntilUserMessageResult(OpenAIStrictModeBaseModel):
    """Result of sleep_until_user_message tool."""

    status: Literal["waiting_for_matrix", "rejected"]
    reason: str | None = None


def _evaluate_enforced_policy(status: ConversationStatus, policy: EnforcedSleepUntilUserMessagePolicy) -> str | None:
    """Check if sleep is allowed under enforced policy."""
    now = datetime.now(UTC)
    user_ts = status.last_user_message_at
    agent_ts = status.last_agent_message_at

    if user_ts is not None and (agent_ts is None or agent_ts < user_ts):
        return "You must respond to the user before sleeping."
    if agent_ts is None:
        return "Send an update to the user before sleeping."
    if (now - agent_ts) > policy.timeout:
        return "Your last update is too old. Provide a fresh update before sleeping."
    return None


def _build_sleep_description(policy: SleepUntilUserMessagePolicy) -> str:
    """Build description for sleep tool based on policy."""
    base = "Suspend yourself until a new user Matrix message arrives. Use this when all tasks are complete or blocked."
    if isinstance(policy, LegacySleepUntilUserMessagePolicy):
        return base
    assert isinstance(policy, EnforcedSleepUntilUserMessagePolicy)
    window_seconds = int(policy.timeout.total_seconds())
    return (
        f"{base} Calls are rejected unless you have already replied to the last user "
        f"message and your most recent reply is no older than {window_seconds} seconds."
    )


class EmberCompositor(Compositor):
    """Compositor with ember's MCP servers pre-mounted.

    Mounts:
    - ember: sleep_until_user_message tool
    - exec: DirectExecServer (exec, read_image tools)
    """

    ember: Mounted[FlatModelMixin]
    exec: Mounted[DirectExecServer]

    def __init__(
        self,
        *,
        workspace_path: Path,
        sleep_callback: Callable[[], None],
        status_provider: ConversationStatusProvider | None,
        sleep_policy: SleepUntilUserMessagePolicy,
    ) -> None:
        super().__init__()
        self._workspace_path = workspace_path
        self._sleep_callback = sleep_callback
        self._status_provider = status_provider
        self._sleep_policy = sleep_policy

    async def __aenter__(self) -> EmberCompositor:
        await super().__aenter__()
        ember_server = _create_ember_sleep_server(
            sleep_callback=self._sleep_callback, status_provider=self._status_provider, sleep_policy=self._sleep_policy
        )
        self.ember = await self.mount_inproc(MCPMountPrefix("ember"), ember_server)
        self.exec = await self.mount_inproc(MCPMountPrefix("exec"), DirectExecServer(default_cwd=self._workspace_path))
        return self


def _create_ember_sleep_server(
    *,
    sleep_callback: Callable[[], None],
    status_provider: ConversationStatusProvider | None,
    sleep_policy: SleepUntilUserMessagePolicy,
) -> FlatModelMixin:
    """Create MCP server with ember's sleep tool."""
    server = FlatModelMixin("ember-tools")

    sleep_description = _build_sleep_description(sleep_policy)

    @server.flat_model(name="sleep_until_user_message", description=sleep_description)
    async def sleep_until_user_message(input: SleepUntilUserMessageInput) -> SleepUntilUserMessageResult:
        if isinstance(sleep_policy, EnforcedSleepUntilUserMessagePolicy):
            if status_provider is None:
                return SleepUntilUserMessageResult(
                    status="rejected", reason="Conversation status provider not available"
                )
            status = await status_provider.get_conversation_status()
            violation = _evaluate_enforced_policy(status, sleep_policy)
            if violation is not None:
                return SleepUntilUserMessageResult(status="rejected", reason=violation)

        sleep_callback()
        return SleepUntilUserMessageResult(status="waiting_for_matrix")

    return server
