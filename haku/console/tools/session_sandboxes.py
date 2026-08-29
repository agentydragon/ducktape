"""Approval-gated MCP access to the Console's active session sandbox claims."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from haku.console.harnesses.kind import HarnessKind
from haku.console.mcp.execution import (
    EXECUTION_CONTEXT_DEPENDENCY,
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
)
from haku.console.mcp.in_process_server_access import InProcessServerAccessPolicy
from haku.console.session.runtime import ActiveSandboxRecord, SessionService
from haku.console.session.sandbox_claims import SandboxProvisioningView
from haku.console.session.status import SessionStatus

HAKU_SESSION_SANDBOXES_SERVER_ID = "haku_session_sandboxes"
MAX_PAGE = 100
DEFAULT_PAGE = 25

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_TERMINATE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


class ActiveSandboxCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_at: datetime = Field(
        description="The created_at value from the first item not returned by the previous page."
    )
    session_id: UUID


class ActiveSandbox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    harness_kind: HarnessKind
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    sandbox: SandboxProvisioningView


class ActiveSandboxPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ActiveSandbox]
    next_cursor: ActiveSandboxCursor | None = Field(
        description="The first active session this page did not return; pass it back as cursor to continue."
    )


class SessionSandboxTerminationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: Literal["terminated"] = "terminated"


def _operator_id(execution: McpExecutionContext, access: InProcessServerAccessPolicy) -> UUID:
    """Resolve the operator from trusted execution metadata, never from tool arguments."""
    if isinstance(execution.caller, OperatorMcpExecutionCaller):
        return execution.caller.operator_id
    if not isinstance(execution.caller, AgentMcpExecutionCaller):
        raise ToolError("an authenticated Console caller is required")
    if not access.allows(execution.caller, HAKU_SESSION_SANDBOXES_SERVER_ID):
        raise ToolError("in-process server access denied")
    try:
        return execution.operator_id
    except PermissionError as error:
        raise ToolError(str(error)) from error


def _view(record: ActiveSandboxRecord) -> ActiveSandbox:
    return ActiveSandbox(
        session_id=record.session_id,
        harness_kind=record.runtime_kind,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        sandbox=record.sandbox,
    )


def build_mcp(service: SessionService, *, access: InProcessServerAccessPolicy) -> FastMCP:
    mcp = FastMCP(
        name=HAKU_SESSION_SANDBOXES_SERVER_ID,
        strict_input_validation=True,
        instructions=(
            "Inspect and terminate active Haku Console sandbox sessions. list_active returns a bounded "
            "paged inventory including provisioning, ready, responding, and closing resources. "
            "terminate permanently deletes the selected claim; session history remains available."
        ),
    )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_active(
        cursor: Annotated[
            ActiveSandboxCursor | None,
            Field(default=None, description="From a previous page's next_cursor; omit for the newest sessions."),
        ] = None,
        limit: Annotated[
            int, Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE, description="Maximum active sessions in this page.")
        ] = DEFAULT_PAGE,
        execution: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> ActiveSandboxPage:
        """List this Operator's active Console-launched sandbox sessions, newest first."""
        operator_id = _operator_id(execution, access)
        records = await service.list_active_sandboxes(
            operator_id,
            before_created_at=cursor.created_at if cursor is not None else None,
            before_session_id=cursor.session_id if cursor is not None else None,
            limit=limit + 1,
        )
        page = records[:limit]
        more = records[limit] if len(records) > limit else None
        return ActiveSandboxPage(
            items=[_view(record) for record in page],
            next_cursor=(
                ActiveSandboxCursor(created_at=more.created_at, session_id=more.session_id)
                if more is not None
                else None
            ),
        )

    @mcp.tool(annotations=_TERMINATE)
    async def terminate(
        session_id: Annotated[UUID, Field(description="The session_id from list_active.")],
        execution: McpExecutionContext = EXECUTION_CONTEXT_DEPENDENCY,
    ) -> SessionSandboxTerminationResult:
        """Permanently terminate one active sandbox claim while preserving its session history."""
        operator_id = _operator_id(execution, access)
        try:
            await service.dispose(operator_id, session_id)
        except KeyError as error:
            raise ToolError("active sandbox session not found or not owned by this Operator") from error
        return SessionSandboxTerminationResult(session_id=session_id)

    return mcp
