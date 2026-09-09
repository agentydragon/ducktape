"""Compact generic Action tools on the canonical service's authenticated HTTP frontend."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from functools import wraps
from typing import Annotated, cast
from uuid import UUID

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools import ToolResult
from pydantic import BaseModel, Field, JsonValue
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from x.agentplane.action_service.auth import workload_principal
from x.agentplane.action_service.catalog import ActionCatalog, ActionIdentity, Key, UnknownActionError
from x.agentplane.action_service.db import ActionConflictError, ActionNotFoundError
from x.agentplane.action_service.models import ActionEventView, ActionRequestInput, ActionRequestView, Principal
from x.agentplane.action_service.service import ActionService, InvalidActionArgumentsError, UnsupportedActionError
from x.agentplane.action_service.updates import ActionUpdates, UpdatesUnavailableError
from x.agentplane.action_service.waits import ActionWaiter, WaitOptions, WaitUntil
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator

PageSize = Annotated[int, Field(ge=1, le=100, description="Maximum entries in this page (1-100).")]
WaitSeconds = Annotated[
    float,
    Field(ge=0, le=30, allow_inf_nan=False, description="Wait at most this many seconds; zero returns immediately."),
]


class IncludeField(StrEnum):
    INPUT_SCHEMA = "input_schema"
    DESCRIPTION = "description"


class ActionSummary(BaseModel):
    group: str
    name: str
    available: bool
    input_schema: dict[str, JsonValue] | None = None
    description: str | None = None


class ActionPage(BaseModel):
    actions: list[ActionSummary]
    next_after: ActionIdentity | None = None


class EventPage(BaseModel):
    events: list[ActionEventView]
    next_after_sequence: int | None = None


class ActionsMcp:
    """Authenticate every transport request, never just MCP initialization or a session id."""

    def __init__(self, app: ASGIApp, authenticator: SandboxPrincipalAuthenticator) -> None:
        self._app = app
        self._authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        try:
            request.state.action_principal = workload_principal(await self._authenticator(request))
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, status_code=error.status_code, headers=error.headers)(
                scope, receive, send
            )
            return
        disconnected = asyncio.Event()
        request.state.action_disconnected = disconnected

        async def observe_disconnect() -> Message:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected.set()
            return message

        try:
            await self._app(scope, observe_disconnect, send)
        finally:
            # The MCP SDK's stateless server task can outlive its HTTP transport. End only
            # this request's bounded read, without cancelling the canonical Action.
            disconnected.set()


def _principal() -> Principal:
    return cast(Principal, get_http_request().state.action_principal)


def _tool_errors[**P, R](tool: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(tool)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await tool(*args, **kwargs)
        except ActionNotFoundError:
            raise ToolError(
                "Action request not found for this caller; use a request ID returned to this connection."
            ) from None
        except (ActionConflictError, UnknownActionError, InvalidActionArgumentsError, UpdatesUnavailableError) as error:
            raise ToolError(str(error)) from None
        except UnsupportedActionError:
            raise ToolError(
                "Action is unknown or unavailable; use list_actions/get_action to check the catalog."
            ) from None

    return wrapped


def _summary(catalog: ActionCatalog, identity: ActionIdentity, fields: set[IncludeField]) -> ActionSummary:
    group, action = catalog.resolve(identity.group, identity.name)
    return ActionSummary(
        group=identity.group,
        name=identity.name,
        available=group.available,
        input_schema=action.input_schema if IncludeField.INPUT_SCHEMA in fields else None,
        description=action.description if IncludeField.DESCRIPTION in fields else None,
    )


def _result(model: BaseModel, *, exclude_none: bool = False) -> ToolResult:
    return ToolResult(structured_content=model.model_dump(mode="json", exclude_none=exclude_none))


def create_server(
    service: ActionService, catalog: ActionCatalog, updates: ActionUpdates, authenticator: SandboxPrincipalAuthenticator
) -> FastMCP:
    waiter = ActionWaiter(service, updates)
    server = FastMCP(
        "Agentplane Actions",
        instructions="Discover Action identifiers, fetch details only when needed, then submit with a stable idempotency key. "
        "A pending receipt is not execution success. Recover with get_action_request; do not create a replacement key.",
        mask_error_details=True,
        strict_input_validation=True,
        tasks=False,
    )

    async def revalidate(principal: Principal) -> None:
        try:
            current = workload_principal(await authenticator(get_http_request()))
        except HTTPException:
            raise ToolError(
                "Workload authorization expired during the wait; reconnect with a valid workload bearer."
            ) from None
        if current != principal:
            raise ToolError("Workload identity changed during the wait; recover the request as its original caller.")

    async def wait_for_receipt(request_id: UUID, principal: Principal, options: WaitOptions) -> ActionRequestView:
        if options.wait_seconds == 0:
            return await waiter.get(request_id, principal, options)
        disconnected = cast(asyncio.Event, get_http_request().state.action_disconnected)
        receipt = asyncio.create_task(waiter.get(request_id, principal, options))
        disconnect = asyncio.create_task(disconnected.wait())
        try:
            await asyncio.wait((receipt, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnected.is_set():
                raise asyncio.CancelledError
            return await receipt
        finally:
            receipt.cancel()
            disconnect.cancel()
            await asyncio.gather(receipt, disconnect, return_exceptions=True)

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def list_actions(
        group: Key | None = None,
        after: ActionIdentity | None = None,
        limit: PageSize = 30,
        include_fields: set[IncludeField] | None = None,
    ) -> ToolResult:
        """Discover available Action identifiers without loading their full schemas or descriptions.
        Use this before get_action when the group/name is unknown; this never submits an Action.
        Optionally filter by group, select input_schema/description, and resume with next_after.
        Pages contain at most limit entries; backend addresses, credentials, and bindings are never returned.
        """
        if group is not None and group not in catalog.groups:
            raise ToolError("Unknown group; omit group to discover configured Action identifiers.")
        if after is not None and group is not None and after.group != group:
            raise ToolError("after.group must match the selected group; use the previous page's next_after.")
        identities = (
            ActionIdentity(group=group_key, name=name)
            for group_key in sorted(catalog.groups)
            if group is None or group == group_key
            for name in sorted(catalog.groups[group_key].actions)
            if after is None or (group_key, name) > (after.group, after.name)
        )
        page: list[ActionSummary] = []
        next_after = None
        for identity in identities:
            if len(page) == limit:
                last = page[-1]
                next_after = ActionIdentity(group=last.group, name=last.name)
                break
            page.append(_summary(catalog, identity, include_fields or set()))
        return _result(ActionPage(actions=page, next_after=next_after), exclude_none=True)

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def get_action(group: Key, name: Key, include_fields: set[IncludeField] | None = None) -> ToolResult:
        """Read one Action definition, not a submitted request or its execution status.
        Provide group/name from list_actions; request input_schema before constructing unfamiliar arguments.
        Full description and input_schema appear only when named in include_fields; defaults are compact.
        Unknown names fail clearly; use get_action_request instead when you have a durable request ID.
        """
        return _result(
            _summary(catalog, ActionIdentity(group=group, name=name), include_fields or set()), exclude_none=True
        )

    @server.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    @_tool_errors
    async def request_action(
        request: ActionRequestInput, wait_seconds: WaitSeconds = 0, wait_until: WaitUntil = WaitUntil.TERMINAL
    ) -> ToolResult:
        """Submit one Action for policy evaluation, human decision if needed, and single-shot execution.
        Supply a stable idempotency_key with structured action group/name and validated arguments.
        Returns the durable receipt immediately by default; optionally wait up to 30 seconds for decision or terminal state.
        Pending is not success. After response loss reuse the identical request/key or read its ID, never submit a new key.
        """
        principal = _principal()
        view = await service.submit(request, principal)
        if wait_seconds:
            view = await wait_for_receipt(
                view.id, principal, WaitOptions(wait_seconds=wait_seconds, wait_until=wait_until)
            )
            await revalidate(principal)
        return _result(view)

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def get_action_request(
        request_id: UUID, wait_seconds: WaitSeconds = 0, wait_until: WaitUntil = WaitUntil.TERMINAL
    ) -> ToolResult:
        """Read your submitted Action's current receipt, Decision, and safe execution result/error.
        Use the durable request ID returned by request_action, not a catalog group/name.
        Optionally wait up to 30 seconds for decision or terminal state; a deadline returns the current pending receipt.
        This never submits, retries, or cancels execution, and other callers' request IDs are not readable.
        """
        principal = _principal()
        view = await wait_for_receipt(
            request_id, principal, WaitOptions(wait_seconds=wait_seconds, wait_until=wait_until)
        )
        if wait_seconds:
            await revalidate(principal)
        return _result(view)

    @server.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    @_tool_errors
    async def cancel_action_request(request_id: UUID) -> ToolResult:
        """Withdraw your Action request only before its execution has been claimed for dispatch.
        Provide the durable request ID; no version is required, and another caller's requests are inaccessible.
        Returns cancelled, already_cancelled, already_finished, or too_late together with the current receipt.
        Dispatching/running or unknown executions cannot be stopped; retrying the original submission key retains its receipt.
        """
        return _result(await service.cancel(request_id, _principal()))

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def list_action_request_events(
        request_id: UUID, after_sequence: Annotated[int, Field(ge=0)] = 0, limit: PageSize = 30
    ) -> ToolResult:
        """Read an ordered page of canonical state transitions for your Action request.
        Start after_sequence at zero or at the last sequence already received; use next_after_sequence for more pages.
        Each event carries its sequence, state, and timestamp; get_action_request provides the current receipt and result.
        This read never submits or retries execution and cannot reveal another caller's events.
        """
        events = await service.events(request_id, _principal(), after_sequence=after_sequence, limit=limit + 1)
        return _result(
            EventPage(
                events=events[:limit], next_after_sequence=events[limit - 1].sequence if len(events) > limit else None
            ),
            exclude_none=True,
        )

    return server
