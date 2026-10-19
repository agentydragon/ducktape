"""Test helpers for Haku's MCP approval service."""

from __future__ import annotations

import asyncio
import datetime
import secrets

from fastmcp import FastMCP

from haku.console.database_schema import McpToolCall, McpToolCallPrincipal
from haku.console.mcp.approval import PostgresToolCallLedger
from haku.console.mcp.tool_call_service import ToolCallApplicationService
from haku.console.mcp_config import InProcessCredentialKind, InProcessServerRegistration, McpServerEntry
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tool_calls import (
    AgentToolCallCaller,
    OperatorToolCallCaller,
    SubmitToolCallRequest,
    ToolCallCaller,
    ToolCallRecord,
    ToolCallStatus,
)


def const_in_process_server(mcp: FastMCP) -> InProcessServerRegistration:
    """A test registration whose builder always returns the same credential-free server."""
    return InProcessServerRegistration(builder=lambda _token: mcp, credential_kind=InProcessCredentialKind.NONE)


async def seed_pending_tool_call(
    ledger: PostgresToolCallLedger, *, server: McpServerEntry, req: SubmitToolCallRequest, actor: RuntimeActor
) -> ToolCallRecord:
    """Insert a historical pending row for tests of approval and history behavior."""
    async with ledger._sessions.begin() as session:
        tool_call_id = f"tc_{secrets.token_hex(12)}"
        caller: ToolCallCaller
        match actor:
            case AgentActor() as agent:
                display_name, _ = await ledger._require_active_agent_binding(session, agent)
                caller = AgentToolCallCaller(agent_id=agent.agent_id, display_name=display_name, session_id=None)
                principal = McpToolCallPrincipal(
                    tool_call_id=tool_call_id, operator_id=None, binding_id=agent.binding_id
                )
            case OperatorActor() as operator:
                await ledger._require_active_operator(session, operator.operator_id)
                caller = OperatorToolCallCaller()
                principal = McpToolCallPrincipal(
                    tool_call_id=tool_call_id, operator_id=operator.operator_id, binding_id=None
                )
            case _:
                raise TypeError(f"unsupported tool-call actor: {type(actor).__name__}")

        now = datetime.datetime.now(datetime.UTC)
        record = ToolCallRecord(
            tool_call_id=tool_call_id,
            server_id=server.id,
            tool_name=req.tool_name,
            caller=caller,
            status=ToolCallStatus.PENDING_APPROVAL,
            created_at=now,
            updated_at=now,
            arguments=req.arguments,
            rationale=req.rationale,
            title=req.title,
        )
        session.add(
            McpToolCall(
                tool_call_id=record.tool_call_id,
                server_id=record.server_id,
                tool_name=record.tool_name,
                status=record.status,
                created_at=record.created_at,
                updated_at=record.updated_at,
                arguments_json=record.arguments,
                rationale=record.rationale,
                title=record.title,
            )
        )
        await session.flush()
        session.add(principal)
        return record


async def join_executions(service: ToolCallApplicationService) -> None:
    """Wait for approved background executions to settle so tests can inspect terminal rows."""
    while service._execution_tasks:
        await asyncio.gather(*tuple(service._execution_tasks), return_exceptions=True)
