"""Compact generic Action tools on the canonical service's authenticated HTTP frontend."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import wraps
from typing import Annotated, Any, Final, cast
from uuid import UUID, uuid4

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import CurrentAccessToken, get_access_token, get_http_request
from fastmcp.tools import ToolResult
from more_itertools import one
from pydantic import BaseModel, Field, JsonValue
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentplane.action_service.caller_auth import CallerToken, CallerTokenVerifier
from agentplane.action_service.catalog import (
    ActionCatalog,
    ActionIdentity,
    ActionUnavailableError,
    Key,
    UnknownActionError,
)
from agentplane.action_service.db import ActionConflictError, ActionNotFoundError
from agentplane.action_service.direct_tools import DIRECT_CALL_TITLE, DIRECT_WAIT_SECONDS, DirectToolProvider, refusal
from agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CallerPrincipal,
    CancellationOutcome,
    DecisionView,
    ExecutionState,
    ExternalGrantProvenance,
)
from agentplane.action_service.policy_view import SELF, PolicyTarget
from agentplane.action_service.service import (
    ActionService,
    InvalidActionArgumentsError,
    UndecidedRequestError,
    UnsupportedActionError,
)
from agentplane.action_service.tool_results import tool_result
from agentplane.action_service.updates import ActionUpdates, UpdatesUnavailableError
from agentplane.action_service.waits import ActionWaiter, WaitOptions, WaitSeconds
from agentplane.subjects import ServiceAccountRef

PageSize = Annotated[int, Field(ge=1, le=100, description="Maximum entries in this page (1-100).")]
IdempotencyKey = Annotated[
    str, Field(min_length=1, max_length=200, description="The idempotency_key this caller submitted the request under.")
]


class ActionSchemaField(StrEnum):
    """Optional fields of one Action's own definition/schema; include_fields on list_actions and
    get_action is a pure allowlist over these."""

    INPUT_SCHEMA = "input_schema"
    DESCRIPTION = "description"


class RequestField(StrEnum):
    """Every top-level Receipt field; include_fields is a pure allowlist over these."""

    ID = "id"
    STATE = "state"
    VERSION = "version"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    INPUT = "input"
    ORIGIN = "origin"
    CORRELATION = "correlation"
    CALLER = "caller"
    EXTERNAL_GRANT = "external_grant"
    DECISION = "decision"
    EXECUTION = "execution"


DEFAULT_RECEIPT_FIELDS: Final[list[RequestField]] = [
    RequestField.ID,
    RequestField.STATE,
    RequestField.VERSION,
    RequestField.CREATED_AT,
    RequestField.UPDATED_AT,
]


class PolicyField(StrEnum):
    """Every top-level get_action_policy field; include_fields is a pure allowlist over these."""

    SUBJECT = "subject"
    SYNCED = "synced"
    BINDINGS = "bindings"
    AUTO_APPROVE_IF = "auto_approve_if"
    AUTO_DENY_IF = "auto_deny_if"
    AUTO_DENY_UNLESS = "auto_deny_unless"


DEFAULT_POLICY_FIELDS: Final[list[PolicyField]] = [PolicyField.SUBJECT, PolicyField.SYNCED, PolicyField.BINDINGS]

# FastMCP resolves a parameter by its dependency default and strips it from a tool's input schema;
# module-level because a call in a default is what ruff's B008 refuses (also below, for CURRENT_ACCESS_TOKEN).
DEFAULT_WAIT: Final = WaitOptions()


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


class RequestInput(BaseModel):
    """The caller-authored submission envelope, echoed back as one unit."""

    idempotency_key: str
    action: ActionIdentity
    arguments: dict[str, JsonValue]
    title: str
    description: str | None


class ExecutionReceipt(BaseModel):
    """An Execution as a receipt reports it: state, error and timing. Its result is read only through
    get_action_result, as the tool answered, never as JSON nested in a receipt."""

    id: UUID
    state: ExecutionState
    error: dict[str, JsonValue] | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    reconciled_at: datetime | None


class Receipt(BaseModel):
    """The MCP-facing projection of an ActionRequestView; every field is gated by include_fields."""

    id: UUID | None = None
    state: ActionState | None = None
    version: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    input: RequestInput | None = None
    origin: dict[str, JsonValue] | None = None
    correlation: dict[str, JsonValue] | None = None
    caller: ServiceAccountRef | None = None
    external_grant: ExternalGrantProvenance | None = None
    decision: DecisionView | None = None
    execution: ExecutionReceipt | None = None


class CancellationView(BaseModel):
    outcome: CancellationOutcome
    request: Receipt


class TransportDisconnects:
    """Expose the transport's `http.disconnect` to tools as `request.state.action_disconnected`.

    Only a wrapper around the transport's `receive` sees the disconnect, and the MCP SDK's stateless
    server task can outlive the transport, so a bounded wait watches this event instead."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        disconnected = asyncio.Event()
        Request(scope).state.action_disconnected = disconnected

        async def observe_disconnect() -> Message:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected.set()
            return message

        try:
            await self._app(scope, observe_disconnect, send)
        finally:
            # End only this request's bounded read, without cancelling the canonical Action.
            disconnected.set()


@dataclass(frozen=True, slots=True)
class Caller:
    """Who this transport request authenticated as, injected into every tool that acts for a caller
    and never a tool argument.

    The same two fields as `CallerToken`, deliberately: that one extends FastMCP's `AccessToken` and
    so carries the bearer itself, which tool code has no business holding.
    """

    principal: CallerPrincipal
    external_grant: ExternalGrantProvenance | None


# FastMCP resolves a parameter by its dependency default and strips it from a tool's input schema;
# the markers are module-level because a call in a default is what ruff's B008 refuses.
CURRENT_ACCESS_TOKEN = CurrentAccessToken()


def _caller_token(token: AccessToken | None) -> CallerToken:
    if not isinstance(token, CallerToken):
        raise RuntimeError(f"the MCP transport was not authenticated by CallerTokenVerifier: {type(token).__name__}")
    return token


def _caller(token: AccessToken = CURRENT_ACCESS_TOKEN) -> Caller:
    verified = _caller_token(token)
    return Caller(principal=verified.principal, external_grant=verified.external_grant)


CALLER = Depends(_caller)


def _tool_errors[**P, R](tool: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(tool)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await tool(*args, **kwargs)
        except ActionNotFoundError:
            raise ToolError(
                "Action request not found for this caller; use a request ID or idempotency key this caller submitted."
            ) from None
        except (
            ActionUnavailableError,
            ActionConflictError,
            UnknownActionError,
            InvalidActionArgumentsError,
            UpdatesUnavailableError,
        ) as error:
            # Re-raised as ToolError (a FastMCPError) so mask_error_details=True still lets this
            # message through: FastMCP only preserves FastMCPError text, masking any other exception.
            raise ToolError(str(error)) from None
        except UnsupportedActionError:
            raise ToolError(
                "Action is unknown or unavailable; use list_actions/get_action to check the catalog."
            ) from None

    return wrapped


def _summary(catalog: ActionCatalog, identity: ActionIdentity, fields: set[ActionSchemaField]) -> ActionSummary:
    group, action = catalog.resolve(identity.group, identity.name)
    return ActionSummary(
        group=identity.group,
        name=identity.name,
        available=group.available,
        input_schema=action.input_schema if ActionSchemaField.INPUT_SCHEMA in fields else None,
        description=action.description if ActionSchemaField.DESCRIPTION in fields else None,
    )


def _receipt(view: ActionRequestView, fields: set[RequestField]) -> Receipt:
    """Set only the requested fields, via model_construct, so a field that is requested but
    genuinely null (e.g. execution before dispatch) still dumps as null rather than being
    indistinguishable from one that was never requested -- exclude_none can't tell those apart,
    exclude_unset (below) can, since only explicitly-set fields survive it regardless of value."""
    values: dict[str, Any] = {}
    if RequestField.ID in fields:
        values["id"] = view.id
    if RequestField.STATE in fields:
        values["state"] = view.state
    if RequestField.VERSION in fields:
        values["version"] = view.version
    if RequestField.CREATED_AT in fields:
        values["created_at"] = view.created_at
    if RequestField.UPDATED_AT in fields:
        values["updated_at"] = view.updated_at
    if RequestField.INPUT in fields:
        values["input"] = RequestInput(
            idempotency_key=view.idempotency_key,
            action=view.action,
            arguments=view.arguments,
            title=view.title,
            description=view.description,
        )
    if RequestField.ORIGIN in fields:
        values["origin"] = view.origin
    if RequestField.CORRELATION in fields:
        values["correlation"] = view.correlation
    if RequestField.CALLER in fields:
        values["caller"] = view.caller
    if RequestField.EXTERNAL_GRANT in fields:
        values["external_grant"] = view.external_grant
    if RequestField.DECISION in fields:
        values["decision"] = view.decision
    if RequestField.EXECUTION in fields:
        values["execution"] = (
            None if view.execution is None else ExecutionReceipt.model_validate(view.execution, from_attributes=True)
        )
    return Receipt.model_construct(**values)


def _result(model: BaseModel, *, exclude_none: bool = False, exclude_unset: bool = False) -> ToolResult:
    return ToolResult(
        structured_content=model.model_dump(mode="json", exclude_none=exclude_none, exclude_unset=exclude_unset)
    )


def create_server(
    service: ActionService,
    catalog: ActionCatalog,
    updates: ActionUpdates,
    verifier: CallerTokenVerifier,
    *,
    direct_wait_seconds: float = DIRECT_WAIT_SECONDS,
) -> FastMCP:
    waiter = ActionWaiter(service, updates)
    # strict_input_validation is left at FastMCP's own default (False): its own tool dispatch
    # validates arguments via TypeAdapter.validate_python on the already-JSON-decoded arguments
    # dict, never validate_json on the raw request bytes, and pydantic's "a JSON string coerces to
    # UUID/Enum" leniency is specifically a validate_json behavior -- verified against pydantic
    # 2.12.5, validate_python(dict, strict=True) rejects a plain string for a UUID or Enum field
    # ("Input should be an instance of X") where validate_json(text, strict=True) accepts the
    # identical value, and no field- or model-level strict override can claw that back once the
    # outer call passes strict=True. So strict_input_validation=True would reject every UUID- and
    # enum-shaped argument (request_id, include_fields, wait.wait_until) an ordinary MCP client
    # sends, for a benefit (rejecting a numeric-looking string like "5" for an int field) that
    # doesn't apply to them. Lax mode still rejects a value that isn't one of an enum's members --
    # it relaxes the input's Python type, not the value check.
    server = FastMCP(
        "Agentplane Actions",
        instructions="Discover Action identifiers, fetch details only when needed, then submit each request once under "
        "a fresh idempotency key. A pending receipt is not execution success. A repeated key is refused; recover a "
        "lost response with get_action_request(idempotency_key=...), never with a replacement key.",
        auth=verifier,
        mask_error_details=True,
        tasks=False,
    )

    async def revalidate(principal: CallerPrincipal) -> None:
        current = await verifier.verify_token(_caller_token(get_access_token()).token)
        if current is None:
            raise ToolError("Caller authorization expired during the wait; reconnect with a valid caller bearer.")
        if current.principal != principal:
            raise ToolError("Caller identity changed during the wait; recover the request as its original caller.")

    async def named_request(request_id: UUID | None, idempotency_key: str | None, principal: CallerPrincipal) -> UUID:
        if (request_id is None) == (idempotency_key is None):
            raise ToolError("Name the request by exactly one of request_id or idempotency_key.")
        if request_id is not None:
            return request_id
        return one(
            await service.list_requests(principal, idempotency_key=idempotency_key),
            too_short=ActionNotFoundError(idempotency_key),
        ).id

    async def wait_for_receipt(request_id: UUID, principal: CallerPrincipal, options: WaitOptions) -> ActionRequestView:
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

    def external_caller() -> CallerPrincipal | None:
        # Outside an HTTP request there is no bearer, and so no caller to list for.
        token = get_access_token()
        if token is None:
            return None
        verified = _caller_token(token)
        return verified.principal if verified.external_grant is not None else None

    @_tool_errors
    async def call_direct(action: ActionIdentity, arguments: dict[str, JsonValue]) -> ToolResult:
        verified = _caller_token(get_access_token())
        principal = verified.principal
        try:
            view = await service.submit_decided(
                ActionRequestInput(
                    idempotency_key=f"direct-{uuid4()}", title=DIRECT_CALL_TITLE, action=action, arguments=arguments
                ),
                principal,
                external_grant=verified.external_grant,
            )
        except UndecidedRequestError as undecided:
            return refusal(action, str(undecided))
        view = await wait_for_receipt(view.id, principal, WaitOptions(wait_seconds=direct_wait_seconds))
        await revalidate(principal)
        return tool_result(view, catalog.groups[action.group].executor)

    server.add_provider(DirectToolProvider(catalog, service, external_caller, call_direct, direct_wait_seconds))

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def list_actions(
        group: Key | None = None,
        after: ActionIdentity | None = None,
        limit: PageSize = 30,
        include_fields: list[ActionSchemaField] | None = None,
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
            page.append(_summary(catalog, identity, set(include_fields or ())))
        return _result(ActionPage(actions=page, next_after=next_after), exclude_none=True)

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def get_action(group: Key, name: Key, include_fields: list[ActionSchemaField] | None = None) -> ToolResult:
        """Read one Action definition, not a submitted request or its execution status.
        Provide group/name from list_actions; request input_schema before constructing unfamiliar arguments.
        Full description and input_schema appear only when named in include_fields; defaults are compact.
        Unknown names fail clearly; use get_action_request instead when you have a durable request ID.
        """
        return _result(
            _summary(catalog, ActionIdentity(group=group, name=name), set(include_fields or ())), exclude_none=True
        )

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def get_action_policy(
        target: PolicyTarget = SELF, include_fields: list[PolicyField] = DEFAULT_POLICY_FIELDS, caller: Caller = CALLER
    ) -> ToolResult:
        """Read what bindings auto-decide for a target: your own ("self", the default), or a named
        ServiceAccount. include_fields is a pure allowlist over subject (self-target only), synced,
        bindings, auto_approve_if, auto_deny_if, and auto_deny_unless, defaulting to
        subject/synced/bindings. Name auto_approve_if before request_action to learn which Actions and
        arguments are approved without an operator -- each entry names the binding, set and index a
        Decision's policy_evidence names; a request matching nothing waits for one, and until synced is
        true nothing auto-decides. A target the service does not watch reads as no bindings. This never
        submits an Action and says nothing about past Decisions; read those with get_action_request.
        """
        view = (
            service.caller_action_policy(caller.principal, caller.external_grant)
            if target == SELF
            else service.target_action_policy(target.service_account)
        )
        requested = set(include_fields)
        data = view.model_dump(mode="json")
        return ToolResult(structured_content={key: value for key, value in data.items() if key in requested})

    @server.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    @_tool_errors
    async def request_action(
        request: ActionRequestInput,
        wait: WaitOptions = DEFAULT_WAIT,
        include_fields: list[RequestField] = DEFAULT_RECEIPT_FIELDS,
        caller: Caller = CALLER,
    ) -> ToolResult:
        """Submit one Action for policy evaluation, human decision if needed, and single-shot execution.
        Supply a stable idempotency_key with structured action group/name, validated arguments, and a title the deciding operator reads.
        Returns a compact receipt (id, state, version, created_at, updated_at) immediately by default; wait.wait_seconds (0-30) optionally waits for wait.wait_until ("decision" or "terminal", default terminal).
        include_fields is a pure allowlist: input (the submitted idempotency_key/action/arguments/title/description as one unit), origin, correlation, caller, external_grant, decision, and execution (state, error and timing; the result itself is get_action_result's) widen it.
        Pending is not success. A key this caller already used is refused; after response loss read the request with get_action_request(idempotency_key=...), never submit a new key.
        """
        principal = caller.principal
        view = await service.submit(request, principal, external_grant=caller.external_grant)
        if wait.wait_seconds:
            view = await wait_for_receipt(view.id, principal, wait)
            await revalidate(principal)
        return _result(_receipt(view, set(include_fields)), exclude_unset=True)

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def get_action_request(
        request_id: UUID | None = None,
        idempotency_key: IdempotencyKey | None = None,
        wait: WaitOptions = DEFAULT_WAIT,
        include_fields: list[RequestField] = DEFAULT_RECEIPT_FIELDS,
        caller: Caller = CALLER,
    ) -> ToolResult:
        """Read your submitted Action's current receipt: its Decision and its execution's state and error; read the result with get_action_result.
        Name the request by exactly one of the request ID returned by request_action or the idempotency_key you submitted it under; the key recovers a submission whose response was lost.
        wait.wait_seconds (0-30) optionally waits for wait.wait_until ("decision" or "terminal", default terminal); a deadline returns the current pending receipt.
        Returns a compact receipt by default -- see request_action for what include_fields widens; request decision/execution once state is terminal to see how it ended.
        This never submits, retries, or cancels execution, and other callers' requests are not readable.
        """
        principal = caller.principal
        view = await wait_for_receipt(await named_request(request_id, idempotency_key, principal), principal, wait)
        if wait.wait_seconds:
            await revalidate(principal)
        return _result(_receipt(view, set(include_fields)), exclude_unset=True)

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def get_action_result(
        request_id: UUID | None = None,
        idempotency_key: IdempotencyKey | None = None,
        wait_seconds: WaitSeconds = 0,
        caller: Caller = CALLER,
    ) -> ToolResult:
        """Read your Action's outcome as the tool it ran answered: its own content blocks, images included, and
        structured content. No receipt carries the result; this is where it is read.
        Name the request by exactly one of request_id or idempotency_key, as for get_action_request.
        wait_seconds (0-30) waits for it to finish; until then the result says what it waits on and is not an error.
        Denied, cancelled, failed and unknown outcomes are error results; unknown means it may have run.
        This never submits, retries, or cancels execution, and other callers' requests are not readable.
        """
        principal = caller.principal
        view = await wait_for_receipt(
            await named_request(request_id, idempotency_key, principal),
            principal,
            WaitOptions(wait_seconds=wait_seconds),
        )
        if wait_seconds:
            await revalidate(principal)
        group = catalog.groups.get(view.action.group)
        if group is None:
            raise ToolError("This Action's group is no longer configured; read the request with get_action_request.")
        return tool_result(view, group.executor)

    @server.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    @_tool_errors
    async def cancel_action_request(
        request_id: UUID, include_fields: list[RequestField] = DEFAULT_RECEIPT_FIELDS, caller: Caller = CALLER
    ) -> ToolResult:
        """Withdraw your Action request only before its execution has been claimed for dispatch.
        Provide the durable request ID; no version is required, and another caller's requests are inaccessible.
        Returns cancelled, already_cancelled, already_finished, or too_late together with the current receipt, compact by default -- see request_action for include_fields.
        Dispatching/running or unknown executions cannot be stopped; the cancelled receipt stays readable by request ID or idempotency key.
        """
        result = await service.cancel(request_id, caller.principal)
        return _result(
            CancellationView(outcome=result.outcome, request=_receipt(result.request, set(include_fields))),
            exclude_unset=True,
        )

    @server.tool(annotations={"readOnlyHint": True})
    @_tool_errors
    async def list_action_request_events(
        request_id: UUID, after_sequence: Annotated[int, Field(ge=0)] = 0, limit: PageSize = 30, caller: Caller = CALLER
    ) -> ToolResult:
        """Read an ordered page of canonical state transitions for your Action request.
        Start after_sequence at zero or at the last sequence already received; use next_after_sequence for more pages.
        Each event carries its sequence, state, and timestamp; get_action_request provides the current receipt and result.
        This read never submits or retries execution and cannot reveal another caller's events.
        """
        events = await service.events(request_id, caller.principal, after_sequence=after_sequence, limit=limit + 1)
        return _result(
            EventPage(
                events=events[:limit], next_after_sequence=events[limit - 1].sequence if len(events) > limit else None
            ),
            exclude_none=True,
        )

    return server
