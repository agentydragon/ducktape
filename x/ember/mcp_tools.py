"""FastMCP clients providing Ember's tools.

This module creates a ClientGroup containing:
- ember-tools: sleep_until_user_message tool
- exec: DirectExecServer (exec, read_image tools)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from fastmcp import ClientGroup
from fastmcp.client import Client
from fastmcp.server import FastMCP

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from x.ember.config import (
    EnforcedSleepUntilUserMessagePolicy,
    LegacySleepUntilUserMessagePolicy,
    SleepUntilUserMessagePolicy,
)
from x.ember.direct_exec import DirectExecServer
from x.ember.matrix_client import ConversationStatus


class ConversationStatusProvider(Protocol):
    """Protocol for getting conversation status from Matrix client."""

    async def get_conversation_status(self) -> ConversationStatus: ...


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


def make_ember_client_group(
    *,
    workspace_path: Path,
    sleep_callback: Callable[[], None],
    status_provider: ConversationStatusProvider | None,
    sleep_policy: SleepUntilUserMessagePolicy,
) -> ClientGroup:
    """Create namespaced clients for Ember's sleep and exec tools.

    The group keeps one direct FastMCP client per server and prefixes tool names
    with ``ember_`` and ``exec_``.
    """
    ember_server = _create_ember_sleep_server(
        sleep_callback=sleep_callback, status_provider=status_provider, sleep_policy=sleep_policy
    )
    exec_server = DirectExecServer(default_cwd=workspace_path)
    return ClientGroup({"ember": Client(ember_server), "exec": Client(exec_server)})


def _create_ember_sleep_server(
    *,
    sleep_callback: Callable[[], None],
    status_provider: ConversationStatusProvider | None,
    sleep_policy: SleepUntilUserMessagePolicy,
) -> FastMCP:
    """Create MCP server with ember's sleep tool."""
    server = FastMCP("ember-tools")

    sleep_description = _build_sleep_description(sleep_policy)

    @server.tool(name="sleep_until_user_message", description=sleep_description, output_schema=None)
    async def sleep_until_user_message() -> SleepUntilUserMessageResult:
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
