from __future__ import annotations

import asyncio

import docker
from fastmcp.mcp_config import MCPConfig
import pytest
from tests.fixtures.responses import ResponsesFactory
from tests.llm.support.openai_mock import make_mock

from adgn.agent.runtime.container import build_container
from adgn.openai_utils.model import InputTextPart, ResponsesRequest, ResponsesResult


@pytest.mark.requires_docker
async def test_notifications_handler_in_container_inserts_system_message(
    sqlite_persistence, monkeypatch: pytest.MonkeyPatch, policy_allow_all: str
) -> None:
    # Persistence and container
    # Capture OpenAI requests
    captured: list[ResponsesRequest] = []

    async def _create(req: ResponsesRequest) -> ResponsesResult:
        captured.append(req)
        # Always return a simple assistant message; notifications come from admin set_policy
        result: ResponsesResult = ResponsesFactory("test-model").make_assistant_message("done")
        return result

    client = make_mock(_create)
    # Build container headless (no UI) with allow-all policy
    container = await build_container(
        agent_id="notif-e2e",
        mcp_config=MCPConfig(),
        persistence=sqlite_persistence,
        model="test-model",
        client_factory=lambda _model: client,
        with_ui=False,
        docker_client=docker.from_env(),
        initial_policy=policy_allow_all,
    )

    try:
        # Trigger a policy update via admin MCP client (out-of-band notification)
        await container.policy_approver.set_policy_text(policy_allow_all)

        # Run one turn; first sampling triggers notifier tool; second should include notification insert
        assert container.session is not None
        await asyncio.wait_for(container.session.run("go"), timeout=30)

        # Look for the system notification in the request input
        found = False
        for req in captured:
            inp = req.input or []
            for msg in inp:
                # UserMessage with inserted system notification block
                for c in getattr(msg, "content", []) or []:
                    if (
                        isinstance(c, InputTextPart)
                        and "<system notification>" in c.text
                        and ("approval-policy" in c.text or "policy.py" in c.text)
                    ):
                        found = True
                        break
                if found:
                    break
            if found:
                break
        assert found, "expected system notification inserted by NotificationsHandler"
    finally:
        await container.close()
