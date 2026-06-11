from __future__ import annotations

import asyncio

import pytest
import pytest_bazel
from fastmcp.mcp_config import MCPConfig
from hamcrest import assert_that, has_item
from hamcrest.core.base_matcher import BaseMatcher
from hamcrest.core.description import Description

from agent_core_testing.responses import DecoratorMock
from agent_server.mcp.approval_policy.engine import SetPolicyTextArgs
from agent_server.runtime.container import build_container
from mcp_infra.constants import APPROVAL_ADMIN_MOUNT_PREFIX
from openai_utils.model import UserMessage


class IsSystemNotification(BaseMatcher):
    """Matcher for UserMessage containing system notification with specified substrings."""

    def __init__(self, *substrings: str):
        self._substrings = substrings

    def _matches(self, item) -> bool:
        if not isinstance(item, UserMessage):
            return False
        for part in item.content:
            text = part.text
            if "<system notification>" in text and all(s in text for s in self._substrings):
                return True
        return False

    def describe_to(self, description: Description) -> None:
        description.append_text(f"UserMessage with system notification containing {self._substrings!r}")


@pytest.mark.requires_docker
@pytest.mark.timeout(35)
async def test_notifications_handler_in_container_inserts_system_message(
    docker_client, async_docker_client, sqlite_persistence, monkeypatch: pytest.MonkeyPatch, policy_allow_all: str
) -> None:
    @DecoratorMock.mock()
    def mock(m: DecoratorMock):
        # First turn: receive request, return admin_set_policy tool call
        _ = yield
        set_policy_call = m.mcp_tool_call(
            APPROVAL_ADMIN_MOUNT_PREFIX, "set_policy", SetPolicyTextArgs(source=policy_allow_all)
        )
        # Second turn: receive request with tool output (notification should be inserted)
        req = yield set_policy_call
        # Assert system notification is present in this request
        assert isinstance(req.input, list)
        assert_that(req.input, has_item(IsSystemNotification("approval-policy")))
        # Return done message
        yield m.assistant_text("done")

    # Build container headless (no UI) with allow-all policy
    container = await build_container(
        agent_id="notif-e2e",
        mcp_config=MCPConfig(),
        persistence=sqlite_persistence,
        model="test-model",
        client_factory=lambda _model: mock,
        with_ui=False,
        async_docker_client=async_docker_client,
        initial_policy=policy_allow_all,
    )

    try:
        # First turn: agent sets policy via MCP tool, triggering notification
        assert container.session is not None
        await asyncio.wait_for(container.session.run("set policy"), timeout=30)

        # Second turn: notification should be inserted (assertion happens inside mock)
        await asyncio.wait_for(container.session.run("check"), timeout=30)
    finally:
        await container.close()


if __name__ == "__main__":
    pytest_bazel.main()
