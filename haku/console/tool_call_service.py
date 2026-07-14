"""Application service for Haku's actor-scoped tool-call lifecycle.

FastAPI and FastMCP are transport adapters. They resolve one request actor and delegate here;
Postgres, backend MCP execution, operator OAuth, and event delivery implement the narrow ports
below. Keeping orchestration independent of those adapters makes this the one place where actor
scope is carried through policy, persistence, execution, publication, and polling.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Iterable
from typing import Any, Protocol
from uuid import UUID

from haku.console.auto_approval import auto_approve_tool_call
from haku.console.config import Settings
from haku.console.mcp_config import (
    InProcessServers,
    McpServerEntry,
    _credential_token,
    _operator_oauth_enabled,
    _server_entry,
)
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tool_calls import (
    ApprovalDecision,
    ApprovalDecisionRequest,
    SubmitToolCallRequest,
    ToolCallEvent,
    ToolCallRecord,
    ToolCallStatus,
)
from haku.console.tools.gmail_client import GmailToolsClient

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {ToolCallStatus.OK, ToolCallStatus.ERROR, ToolCallStatus.DENIED}


class ToolCallRepository(Protocol):
    def submit(
        self,
        *,
        server: McpServerEntry,
        req: SubmitToolCallRequest,
        actor: ToolCallActor,
        auto_approval_policy_id: str | None = None,
        auto_approval_evaluation: str | None = None,
    ) -> tuple[ToolCallRecord, list[ToolCallEvent]]: ...

    def get(self, tool_call_id: str, *, actor: ToolCallActor) -> ToolCallRecord: ...

    def list_tool_calls(
        self,
        *,
        actor: ToolCallActor,
        statuses: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[ToolCallRecord]: ...

    def events_after_id(self, *, actor: OperatorActor, after_event_id: int = 0) -> list[ToolCallEvent]: ...

    def mark_running(self, tool_call_id: str, *, actor: OperatorActor) -> tuple[ToolCallRecord, ToolCallEvent]: ...

    def deny(
        self, tool_call_id: str, reason: str | None, *, actor: OperatorActor
    ) -> tuple[ToolCallRecord, ToolCallEvent]: ...

    def finish(
        self, tool_call_id: str, *, actor: ToolCallActor, result: dict[str, Any] | None, error: str | None
    ) -> tuple[ToolCallRecord, ToolCallEvent]: ...

    def authorize_execution(self, tool_call_id: str, *, actor: ToolCallActor) -> UUID: ...


class ToolExecutor(Protocol):
    async def execute(
        self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]: ...


class ToolCallEventPublisher(Protocol):
    async def broadcast(self, operator_id: UUID, events: Iterable[ToolCallEvent]) -> None: ...


class OperatorOAuthTokenStore(Protocol):
    async def access_token_for(self, *, server: McpServerEntry, operator_id: UUID) -> str | None: ...


class OperatorActorRequiredError(PermissionError):
    """Raised when an AgentActor reaches an operator-only lifecycle operation."""


class BackendAccountNotConnectedError(Exception):
    def __init__(self, server_id: str) -> None:
        self.server_id = server_id
        super().__init__(f"Connect your {server_id} MCP account in the console before approving this tool call.")


class ToolCallNotFoundError(LookupError):
    """The authenticated actor cannot observe the requested tool call."""


class ToolCallStateConflictError(RuntimeError):
    """The requested lifecycle transition is invalid for the call's durable state."""


async def backend_auth_for_operator(
    *, server: McpServerEntry, operator_id: UUID, oauth_store: OperatorOAuthTokenStore
) -> str | None:
    if _operator_oauth_enabled(server):
        token = await oauth_store.access_token_for(server=server, operator_id=operator_id)
        if not token:
            raise BackendAccountNotConnectedError(server.id)
        return token
    return _credential_token(server)


class ToolCallApplicationService:
    """The single authorization and lifecycle boundary for Haku tool calls."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: ToolCallRepository,
        event_publisher: ToolCallEventPublisher,
        executor: ToolExecutor,
        oauth_store: OperatorOAuthTokenStore,
        in_process_servers: InProcessServers,
        gmail_client: GmailToolsClient | None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._event_publisher = event_publisher
        self._executor = executor
        self._oauth_store = oauth_store
        self._in_process_servers = in_process_servers
        self._gmail_client = gmail_client

    async def submit_and_wait(self, *, req: SubmitToolCallRequest, actor: ToolCallActor) -> ToolCallRecord:
        actor = self._require_actor(actor)
        server = _server_entry(self._settings, req.server_id)
        auto_approval_policy_id, auto_approval_evaluation = await auto_approve_tool_call(
            actor=actor,
            server_id=server.id,
            tool_name=req.tool_name,
            arguments=req.arguments,
            label_prefix=self._settings.gmail_auto_approve_label_prefix,
            gmail=self._gmail_client,
            mcp=self._in_process_servers.get(server.id),
        )

        # A missing operator OAuth association must fail before a RUNNING row is durable. Once
        # persisted, every RUNNING call has all authorization needed to attempt execution.
        auth_token = None
        if auto_approval_policy_id is not None:
            auth_token = await backend_auth_for_operator(
                server=server, operator_id=actor.operator_id, oauth_store=self._oauth_store
            )

        record, events = self._repository.submit(
            server=server,
            req=req,
            actor=actor,
            auto_approval_policy_id=auto_approval_policy_id,
            auto_approval_evaluation=auto_approval_evaluation,
        )
        logger.info(
            "tool call %s submitted status=%s server=%s tool=%s caller=%s approval_policy=%s auto_approval=%s",
            record.tool_call_id,
            record.status,
            record.server_id,
            record.tool_name,
            record.caller,
            record.approval_policy_id,
            record.auto_approval_evaluation,
        )
        if record.status == ToolCallStatus.RUNNING:
            record = await self._execute_and_publish(
                record=record, server=server, auth_token=auth_token, actor=actor, preceding_events=events
            )
        else:
            await self._publish(actor.operator_id, events)
        return await self._wait_terminal(record.tool_call_id, actor, req.wait_for_ms)

    def get(self, tool_call_id: str, *, actor: ToolCallActor) -> ToolCallRecord:
        return self._repository.get(tool_call_id, actor=self._require_actor(actor))

    def list_tool_calls(
        self,
        *,
        actor: ToolCallActor,
        statuses: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[ToolCallRecord]:
        actor = self._require_actor(actor)
        return self._repository.list_tool_calls(
            actor=actor, statuses=statuses, since=since, limit=limit, newest_first=newest_first
        )

    def pending_approvals(self, *, actor: ToolCallActor) -> list[ToolCallRecord]:
        operator = self._require_operator(actor)
        return self._repository.list_tool_calls(actor=operator, statuses=[ToolCallStatus.PENDING_APPROVAL])

    def events_after_id(self, *, actor: ToolCallActor, after_event_id: int = 0) -> list[ToolCallEvent]:
        operator = self._require_operator(actor)
        return self._repository.events_after_id(actor=operator, after_event_id=after_event_id)

    async def decide(
        self, *, tool_call_id: str, decision: ApprovalDecisionRequest, actor: ToolCallActor
    ) -> ToolCallRecord:
        operator = self._require_operator(actor)
        if decision.decision is ApprovalDecision.DENY:
            record, event = self._repository.deny(tool_call_id, decision.reason, actor=operator)
            await self._publish(operator.operator_id, [event])
            logger.info(
                "tool call %s denied server=%s tool=%s reason=%r",
                record.tool_call_id,
                record.server_id,
                record.tool_name,
                decision.reason,
            )
            return record
        if decision.decision is not ApprovalDecision.APPROVE:
            raise AssertionError(f"Unhandled approval {decision.decision=}")

        pending = self._repository.get(tool_call_id, actor=operator)
        server = _server_entry(self._settings, pending.server_id)
        auth_token = await backend_auth_for_operator(
            server=server, operator_id=operator.operator_id, oauth_store=self._oauth_store
        )
        running, event = self._repository.mark_running(tool_call_id, actor=operator)
        return await self._execute_and_publish(
            record=running, server=server, auth_token=auth_token, actor=operator, preceding_events=[event]
        )

    async def _execute_and_publish(
        self,
        *,
        record: ToolCallRecord,
        server: McpServerEntry,
        auth_token: str | None,
        actor: ToolCallActor,
        preceding_events: list[ToolCallEvent],
    ) -> ToolCallRecord:
        if record.status != ToolCallStatus.RUNNING:
            return record
        execution_operator_id = self._repository.authorize_execution(record.tool_call_id, actor=actor)
        cancellation: asyncio.CancelledError | None = None
        try:
            result = await self._executor.execute(server, record.tool_name, record.arguments, auth_token)
        except asyncio.CancelledError as error:
            cancellation = error
            updated, event = self._repository.finish(
                record.tool_call_id, actor=actor, result=None, error="tool execution cancelled"
            )
        except Exception as error:
            updated, event = self._repository.finish(record.tool_call_id, actor=actor, result=None, error=str(error))
        else:
            updated, event = self._repository.finish(record.tool_call_id, actor=actor, result=result, error=None)
        # RUNNING is already durable. Publish its preceding events only after terminal persistence,
        # so a broken event adapter cannot prevent execution and strand the row. One ordered batch
        # also prevents observers from seeing the terminal event before the RUNNING event.
        await self._publish(execution_operator_id, [*preceding_events, event])
        logger.info(
            "tool call %s finished status=%s server=%s tool=%s",
            updated.tool_call_id,
            updated.status,
            updated.server_id,
            updated.tool_name,
        )
        if cancellation is not None:
            raise cancellation
        return updated

    async def _publish(self, operator_id: UUID, events: list[ToolCallEvent]) -> None:
        """Best-effort invalidation after durable changes; the repository remains authoritative."""
        try:
            await self._event_publisher.broadcast(operator_id, events)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to publish tool-call events for operator %s", operator_id)

    async def _wait_terminal(self, tool_call_id: str, actor: ToolCallActor, wait_for_ms: int) -> ToolCallRecord:
        # TODO: replace this bounded poll with a lost-wakeup-safe Postgres invalidation wait. The
        # repository remains authoritative and every read stays actor-scoped in the meantime.
        deadline = asyncio.get_running_loop().time() + (wait_for_ms / 1000)
        while True:
            record = self._repository.get(tool_call_id, actor=actor)
            if record.status in TERMINAL_STATUSES or wait_for_ms <= 0:
                return record
            if asyncio.get_running_loop().time() >= deadline:
                return record
            await asyncio.sleep(0.05)

    @staticmethod
    def _require_actor(actor: ToolCallActor) -> ToolCallActor:
        match actor:
            case AgentActor() | OperatorActor():
                return actor
            case _:
                raise TypeError(f"unsupported tool-call actor: {type(actor).__name__}")

    @staticmethod
    def _require_operator(actor: ToolCallActor) -> OperatorActor:
        if not isinstance(actor, OperatorActor):
            raise OperatorActorRequiredError("operator actor required")
        return actor
