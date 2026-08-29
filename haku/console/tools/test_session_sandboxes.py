"""Contract tests for the approval-gated active session sandbox MCP server."""

from __future__ import annotations

import datetime
from uuid import UUID

import pytest_bazel
from fastmcp import Client

from haku.console.grants.principal import RequestPrincipal
from haku.console.harnesses.kind import HarnessKind
from haku.console.mcp.execution import (
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
    mcp_execution_request_meta,
)
from haku.console.mcp.in_process_server_access import InProcessServerAccessPolicy
from haku.console.mcp_config import AccessProfile
from haku.console.session.runtime import ActiveSandboxRecord
from haku.console.session.sandbox_claims import ProvisioningStep, provisioning_view
from haku.console.session.status import SessionStatus
from haku.console.tools.session_sandboxes import HAKU_SESSION_SANDBOXES_SERVER_ID, build_mcp

NOW = datetime.datetime(2026, 8, 29, 12, 0, tzinfo=datetime.UTC)
OPERATOR = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OTHER_OPERATOR = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
AGENT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SESSION_1 = UUID("11111111-1111-1111-1111-111111111111")
SESSION_2 = UUID("22222222-2222-2222-2222-222222222222")
SESSION_3 = UUID("33333333-3333-3333-3333-333333333333")

ACCESS = InProcessServerAccessPolicy(
    (AccessProfile(id="haku", auto_approval_policy="manual", in_process_server_ids={HAKU_SESSION_SANDBOXES_SERVER_ID}),)
)


class _Service:
    def __init__(self, records: list[ActiveSandboxRecord]) -> None:
        self.records = records
        self.disposed: list[tuple[UUID, UUID]] = []

    async def list_active_sandboxes(self, operator_id: UUID, **kwargs: object) -> list[ActiveSandboxRecord]:
        del kwargs
        return self.records if operator_id == OPERATOR else []

    async def dispose(self, operator_id: UUID, session_id: UUID) -> None:
        if operator_id != OPERATOR or session_id not in {record.session_id for record in self.records}:
            raise KeyError(session_id)
        self.disposed.append((operator_id, session_id))


def _records() -> list[ActiveSandboxRecord]:
    return [
        ActiveSandboxRecord(
            session_id=session_id,
            runtime_kind=HarnessKind.CLAUDE_CODE,
            status=status,
            created_at=NOW - datetime.timedelta(minutes=index),
            updated_at=NOW,
            sandbox=provisioning_view(
                f"claude-{session_id.hex}",
                step=ProvisioningStep.WAITING_FOR_RUNNER,
                claim_ready=True,
                runner_ready=True,
            ),
        )
        for index, (session_id, status) in enumerate(
            (
                (SESSION_1, SessionStatus.PROVISIONING),
                (SESSION_2, SessionStatus.RESPONDING),
                (SESSION_3, SessionStatus.CLOSING),
            )
        )
    ]


def _meta(*, agent: bool = True, operator_id: UUID = OPERATOR) -> dict[str, object]:
    caller = (
        AgentMcpExecutionCaller(
            principal=RequestPrincipal(agent_id=AGENT, session_id=None, access_profile_id="haku"),
            operator_id=operator_id,
        )
        if agent
        else OperatorMcpExecutionCaller(operator_id=operator_id)
    )
    return mcp_execution_request_meta(
        McpExecutionContext(caller=caller, tool_call_id="tc_test", approving_operator_id=None, approval_policy_id=None)
    )


def _mcp(service: _Service):
    return build_mcp(service, access=ACCESS)  # type: ignore[arg-type]


async def test_surface_annotations_and_bounded_arguments() -> None:
    async with Client(_mcp(_Service(_records()))) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == {"list_active", "terminate"}
    assert set(tools["terminate"].inputSchema["properties"]) == {"session_id"}
    assert tools["list_active"].inputSchema["properties"]["limit"]["maximum"] == 100
    assert tools["list_active"].annotations is not None
    assert tools["list_active"].annotations.readOnlyHint
    assert tools["terminate"].annotations is not None
    assert tools["terminate"].annotations.destructiveHint
    assert tools["terminate"].annotations.idempotentHint


async def test_list_is_operator_scoped_and_paginated() -> None:
    service = _Service(_records())
    async with Client(_mcp(service)) as client:
        result = await client.call_tool("list_active", {"limit": 2}, meta=_meta())

    assert not result.is_error
    assert [UUID(item.session_id) for item in result.data.items] == [SESSION_1, SESSION_2]
    assert UUID(result.data.next_cursor.session_id) == SESSION_3

    async with Client(_mcp(service)) as client:
        denied = await client.call_tool("list_active", {}, meta=_meta(operator_id=OTHER_OPERATOR), raise_on_error=False)
    assert not denied.is_error
    assert denied.data.items == []


async def test_agent_without_server_access_is_refused() -> None:
    service = _Service(_records())
    async with Client(_mcp(service)) as client:
        result = await client.call_tool(
            "list_active",
            {},
            meta=mcp_execution_request_meta(
                McpExecutionContext(
                    caller=AgentMcpExecutionCaller(
                        principal=RequestPrincipal(agent_id=AGENT, session_id=None, access_profile_id="other"),
                        operator_id=OPERATOR,
                    ),
                    tool_call_id="tc_test",
                    approving_operator_id=None,
                    approval_policy_id=None,
                )
            ),
            raise_on_error=False,
        )
    assert result.is_error
    assert "access denied" in str(result.content)


async def test_terminate_uses_trusted_operator_and_preserves_history_path() -> None:
    service = _Service(_records())
    async with Client(_mcp(service)) as client:
        result = await client.call_tool("terminate", {"session_id": str(SESSION_2)}, meta=_meta())

    assert not result.is_error
    assert service.disposed == [(OPERATOR, SESSION_2)]
    assert UUID(result.data.session_id) == SESSION_2
    assert result.data.status == "terminated"


async def test_operator_direct_termination_failure_is_reported() -> None:
    service = _Service([])
    async with Client(_mcp(service)) as client:
        result = await client.call_tool(
            "terminate", {"session_id": str(SESSION_1)}, meta=_meta(agent=False), raise_on_error=False
        )
    assert result.is_error
    assert "not found" in str(result.content)


if __name__ == "__main__":
    pytest_bazel.main()
