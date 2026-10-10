"""SEP-2663 MCP task wire adapter; the canonical Action remains the sole execution engine."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.extensions import MethodBinding, ServerExtension, read_client_extension_settings
from mcp.server.context import ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import MCP_NAME_HEADER, decode_header_value
from mcp_types import INVALID_PARAMS, RequestParams
from mcp_types.jsonrpc import HEADER_MISMATCH, MISSING_REQUIRED_CLIENT_CAPABILITY
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer

from agentplane.action_service.db import ActionConflictError, ActionNotFoundError
from agentplane.action_service.models import ActionRequestView, ActionState, CancellationOutcome

IDENTIFIER = "io.modelcontextprotocol/tasks"
_TASK_ERROR = -32000


class TaskFields(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(serialization_alias="taskId")
    status: Literal["working", "completed", "failed", "cancelled"]
    created_at: datetime = Field(serialization_alias="createdAt")
    last_updated_at: datetime = Field(serialization_alias="lastUpdatedAt")
    status_message: str | None = Field(default=None, serialization_alias="statusMessage")
    # Canonical Actions have no expiry; no finite TTL may be advertised.
    ttl_ms: None = Field(default=None, serialization_alias="ttlMs")
    poll_interval_ms: int = Field(default=5000, serialization_alias="pollIntervalMs")

    @model_serializer(mode="wrap")
    def serialize(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        data["ttlMs"] = None  # required-but-nullable even with exclude_none=True
        return data


class CreateTaskResult(TaskFields):
    result_type: Literal["task"] = Field(default="task", serialization_alias="resultType")


class GetTaskResult(TaskFields):
    result_type: Literal["complete"] = Field(default="complete", serialization_alias="resultType")
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class GetTaskParams(RequestParams):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(alias="taskId")


class UpdateTaskParams(GetTaskParams):
    input_responses: dict[str, Any] = Field(alias="inputResponses")


type TaskSnapshot = tuple[ActionRequestView, ActionState | None, datetime | None]


class ActionTasksExtension(ServerExtension):
    identifier = IDENTIFIER

    def __init__(
        self,
        submit: Callable[[dict[str, Any]], Awaitable[ActionRequestView]],
        get: Callable[[UUID], Awaitable[TaskSnapshot]],
        cancel: Callable[[UUID], Awaitable[CancellationOutcome]],
        result: Callable[[ActionRequestView], dict[str, Any]],
    ) -> None:
        self._submit = submit
        self._get = get
        self._cancel = cancel
        self._result = result

    def methods(self) -> Sequence[MethodBinding]:
        versions = frozenset(MODERN_PROTOCOL_VERSIONS)
        return [
            MethodBinding("tasks/get", GetTaskParams, self.get_task, protocol_versions=versions),
            MethodBinding("tasks/cancel", GetTaskParams, self.cancel_task, protocol_versions=versions),
            MethodBinding("tasks/update", UpdateTaskParams, self.update_task, protocol_versions=versions),
        ]

    @staticmethod
    def _check(ctx: ServerRequestContext[Any, Any], task_id: str) -> UUID:
        if read_client_extension_settings(ctx, IDENTIFIER) is None:
            raise MCPError(code=MISSING_REQUIRED_CLIENT_CAPABILITY, message="Tasks extension not negotiated")
        # Modern HTTP routers can route by Mcp-Name; a disagreeing header must not
        # authorize access to the ID in a different request body.
        try:
            header = get_http_request().headers.get(MCP_NAME_HEADER)
        except RuntimeError:
            header = None
        if header is not None and decode_header_value(header) != task_id:
            raise MCPError(code=HEADER_MISMATCH, message="Mcp-Name does not match taskId")
        try:
            return UUID(task_id)
        except ValueError:
            raise MCPError(code=INVALID_PARAMS, message="Task not found") from None

    async def intercept_tool_call(self, params: Any, context: Context, call_next: Callable[[], Awaitable[Any]]) -> Any:
        rc = context.request_context
        if (
            params.name != "start_action_task"
            or rc is None
            or rc.protocol_version not in MODERN_PROTOCOL_VERSIONS
            or context.client_extension_settings(IDENTIFIER) is None
        ):
            return await call_next()
        try:
            view = await self._submit(params.arguments or {})
        except ActionConflictError:
            raise MCPError(
                code=INVALID_PARAMS, message="Idempotency key already used; recover the existing Action request by key"
            ) from None
        except ValidationError, ValueError:
            raise MCPError(code=INVALID_PARAMS, message="Invalid task-augmented Action request") from None
        return CreateTaskResult(**self._fields(view, None, None))

    async def _snapshot(self, request_id: UUID) -> TaskSnapshot:
        try:
            return await self._get(request_id)
        except ActionNotFoundError:
            raise MCPError(code=INVALID_PARAMS, message="Task not found") from None

    @staticmethod
    def _fields(view: ActionRequestView, terminal: ActionState | None, at: datetime | None) -> dict[str, Any]:
        state = terminal or view.state
        status: Literal["working", "completed", "failed", "cancelled"]
        if state is ActionState.SUCCEEDED:
            status = "completed"
        elif state is ActionState.CANCELLED:
            status = "cancelled"
        elif state in (ActionState.FAILED, ActionState.DENIED, ActionState.EXECUTION_UNKNOWN):
            status = "failed"
        else:
            status = "working"
        message = {
            ActionState.DECISION_PENDING: "Awaiting operator approval; nothing has run.",
            ActionState.ALLOWED: "Approved; awaiting dispatch.",
            ActionState.DISPATCHING: "Dispatching.",
            ActionState.RUNNING: "Running.",
            ActionState.EXECUTION_UNKNOWN: "Execution outcome unknown; may have run. Do not retry automatically.",
        }.get(state)
        return {
            "task_id": str(view.id),
            "status": status,
            "created_at": view.created_at,
            "last_updated_at": at or view.updated_at,
            "status_message": message,
        }

    async def get_task(self, ctx: ServerRequestContext[Any, Any], params: GetTaskParams) -> GetTaskResult:
        request_id = self._check(ctx, params.task_id)
        view, terminal, at = await self._snapshot(request_id)
        fields = self._fields(view, terminal, at)
        if fields["status"] == "completed":
            return GetTaskResult(**fields, result=self._result(view))
        if fields["status"] == "failed":
            state = terminal or view.state
            return GetTaskResult(
                **fields, error={"code": _TASK_ERROR, "message": f"Action {state.value}; read its receipt for details."}
            )
        return GetTaskResult(**fields)

    async def cancel_task(self, ctx: ServerRequestContext[Any, Any], params: GetTaskParams) -> dict[str, str]:
        request_id = self._check(ctx, params.task_id)
        await self._snapshot(request_id)  # authorize this Action before cancellation
        outcome = await self._cancel(request_id)
        if outcome in (CancellationOutcome.TOO_LATE, CancellationOutcome.ALREADY_FINISHED):
            raise MCPError(code=INVALID_PARAMS, message=f"Task could not be cancelled: {outcome.value}")
        return {"resultType": "complete"}

    async def update_task(self, ctx: ServerRequestContext[Any, Any], params: UpdateTaskParams) -> None:
        await self._snapshot(self._check(ctx, params.task_id))
        raise MCPError(code=INVALID_PARAMS, message="Action tasks do not request client input")
