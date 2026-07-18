"""Application service for Haku's actor-scoped tool-call lifecycle.

FastAPI and FastMCP are transport adapters. They resolve one request actor and delegate here;
Postgres, backend MCP execution, operator OAuth, and event delivery implement the narrow ports
below. Keeping orchestration independent of those adapters makes this the one place where actor
scope is carried through policy, persistence, execution, publication, and waiting.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import Awaitable, Callable
from enum import Enum, auto
from typing import Any, Protocol
from uuid import UUID

from haku.console.auto_approval import SchemaDenial, auto_approve_tool_call
from haku.console.config import Settings
from haku.console.mcp_config import (
    InProcessServers,
    McpServerEntry,
    _credential_token,
    _operator_oauth_enabled,
    _server_entry,
)
from haku.console.provider_connection_registry import ProviderConnectionKind
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tool_calls import (
    ApprovalDecision,
    ApprovalDecisionRequest,
    SubmitToolCallRequest,
    ToolCallRecord,
    ToolCallStatus,
)
from haku.console.tools.gmail_client import GMAIL_SERVER_ID, GmailToolsClient

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
        auto_denial_reason: str | None = None,
    ) -> ToolCallRecord: ...

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

    def mark_running(self, tool_call_id: str, *, actor: OperatorActor) -> ToolCallRecord: ...

    def deny(self, tool_call_id: str, reason: str | None, *, actor: OperatorActor) -> ToolCallRecord: ...

    def finish(
        self, tool_call_id: str, *, actor: ToolCallActor, result: dict[str, Any] | None, error: str | None
    ) -> ToolCallRecord: ...

    def authorize_execution(self, tool_call_id: str, *, actor: ToolCallActor) -> UUID: ...


class ToolExecutor(Protocol):
    async def execute(
        self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]: ...


class ToolCallInvalidationPublisher(Protocol):
    async def tool_call_changed(self, operator_id: UUID, tool_call_id: str) -> None: ...

    def subscribe(
        self, operator_id: UUID, tool_call_id: str
    ) -> contextlib.AbstractAsyncContextManager[asyncio.Event]: ...


class OperatorOAuthTokenStore(Protocol):
    async def access_token_for(self, *, server: McpServerEntry, operator_id: UUID) -> str | None: ...


class ProviderConnectionTokenStore(Protocol):
    async def access_token_for(self, *, provider: ProviderConnectionKind, operator_id: UUID) -> str | None: ...


class AuthentikOperatorTokenStore(Protocol):
    async def access_token_for(self, *, operator_id: UUID) -> str | None: ...


# Resolves the acting Operator's Gmail client for auto-approval label lookups (or None when the
# Operator has no Google connection). Production builds it from the provider store; tests inject one.
GmailClientProvider = Callable[[UUID], Awaitable[GmailToolsClient | None]]


async def _no_gmail_client(_operator_id: UUID) -> GmailToolsClient | None:
    return None


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


async def _require_operator_linked_token(token: Awaitable[str | None], server_id: str) -> str:
    """Await an operator-linked token (provider connection or operator OAuth), or fail loud."""
    resolved = await token
    if not resolved:
        raise BackendAccountNotConnectedError(server_id)
    return resolved


class ServerAuthMode(Enum):
    """How a server's backend credential for the acting operator is sourced.

    The three operator-scoped modes all execute the call as the operator, differing only in *which*
    credential and where it is linked:

    - ``PROVIDER``: the operator's account at a well-known external provider (Google). The console is
      a fixed pre-registered OAuth client of that provider, and one linked connection is shared
      across servers (``provider_connection``).
    - ``REMOTE_SERVER_OAUTH``: the operator's account at the connected MCP server *itself*, which is
      its own OAuth authorization server. Linked per server through that server's DCR/PKCE flow
      (``operator_oauth``, e.g. kubectl-passthrough).
    - ``OPERATOR_IDENTITY``: the operator's *own* console-login identity — the Authentik token they
      sign into the console with — captured at login and reused/exchanged downstream (hostexec). Not
      a separately linked account; the operator acting as themselves.

    ``STATIC`` is the non-operator mode: a fixed configured bearer the console holds.
    """

    PROVIDER = auto()
    REMOTE_SERVER_OAUTH = auto()
    OPERATOR_IDENTITY = auto()
    STATIC = auto()


def server_auth_mode(server: McpServerEntry) -> ServerAuthMode:
    """Select a server's backend-auth mode. The per-mode failure behavior is the caller's."""
    if server.provider_connection is not None:
        return ServerAuthMode.PROVIDER
    if _operator_oauth_enabled(server):
        return ServerAuthMode.REMOTE_SERVER_OAUTH
    if server.operator_identity_token:
        return ServerAuthMode.OPERATOR_IDENTITY
    return ServerAuthMode.STATIC


async def backend_auth_for_operator(
    *,
    server: McpServerEntry,
    operator_id: UUID,
    oauth_store: OperatorOAuthTokenStore,
    provider_store: ProviderConnectionTokenStore,
    authentik_store: AuthentikOperatorTokenStore,
) -> str | None:
    match server_auth_mode(server):
        case ServerAuthMode.PROVIDER:
            assert server.provider_connection is not None  # PROVIDER ⇒ provider_connection set
            return await _require_operator_linked_token(
                provider_store.access_token_for(provider=server.provider_connection, operator_id=operator_id), server.id
            )
        case ServerAuthMode.REMOTE_SERVER_OAUTH:
            return await _require_operator_linked_token(
                oauth_store.access_token_for(server=server, operator_id=operator_id), server.id
            )
        case ServerAuthMode.OPERATOR_IDENTITY:
            # The Operator's own Authentik access token (captured at login); the server exchanges it
            # for a per-host token. Missing ⇒ the operator has not logged in with offline_access yet.
            return await _require_operator_linked_token(
                authentik_store.access_token_for(operator_id=operator_id), server.id
            )
        case ServerAuthMode.STATIC:
            return _credential_token(server)


class ToolCallApplicationService:
    """The single authorization and lifecycle boundary for Haku tool calls."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: ToolCallRepository,
        invalidation_publisher: ToolCallInvalidationPublisher,
        executor: ToolExecutor,
        oauth_store: OperatorOAuthTokenStore,
        in_process_servers: InProcessServers,
        provider_store: ProviderConnectionTokenStore,
        authentik_token_store: AuthentikOperatorTokenStore,
        gmail_client_provider: GmailClientProvider = _no_gmail_client,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._invalidation_publisher = invalidation_publisher
        self._executor = executor
        self._oauth_store = oauth_store
        self._in_process_servers = in_process_servers
        self._provider_store = provider_store
        self._authentik_token_store = authentik_token_store
        self._gmail_client_provider = gmail_client_provider

    async def _backend_auth(self, server: McpServerEntry, operator_id: UUID) -> str | None:
        return await backend_auth_for_operator(
            server=server,
            operator_id=operator_id,
            oauth_store=self._oauth_store,
            provider_store=self._provider_store,
            authentik_store=self._authentik_token_store,
        )

    async def submit_and_wait(self, *, req: SubmitToolCallRequest, actor: ToolCallActor) -> ToolCallRecord:
        actor = self._require_actor(actor)
        server = _server_entry(self._settings, req.server_id)
        # Gmail label auto-approval resolves label IDs against the acting Operator's own Gmail; the
        # schema check needs the tool's input schema, so build the (credential-independent) server.
        gmail = await self._gmail_client_provider(actor.operator_id) if server.id == GMAIL_SERVER_ID else None
        server_builder = self._in_process_servers.get(server.id)
        decision = await auto_approve_tool_call(
            actor=actor,
            server_id=server.id,
            tool_name=req.tool_name,
            arguments=req.arguments,
            label_prefix=self._settings.gmail_auto_approve_label_prefix,
            gmail=gmail,
            mcp=server_builder(None) if server_builder is not None else None,
        )
        if isinstance(decision, SchemaDenial):
            # Arguments failed an owned in-process schema: the call can never execute, so it is
            # persisted born-denied (full audit row, never pending) and the validation error goes
            # straight back to the caller to self-correct (operator directive 2026-07-16).
            record = self._repository.submit(
                server=server,
                req=req,
                actor=actor,
                auto_approval_evaluation=decision.evaluation,
                auto_denial_reason=decision.reason,
            )
            logger.info(
                "tool call %s auto-denied (schema) server=%s tool=%s caller=%s reason=%r",
                record.tool_call_id,
                record.server_id,
                record.tool_name,
                record.caller,
                record.denial_reason,
            )
            await self._publish(actor.operator_id, record.tool_call_id)
            return record
        auto_approval_policy_id, auto_approval_evaluation = decision

        # A missing operator OAuth association must fail before a RUNNING row is durable. Once
        # persisted, every RUNNING call has all authorization needed to attempt execution.
        auth_token = None
        if auto_approval_policy_id is not None:
            auth_token = await self._backend_auth(server, actor.operator_id)

        record = self._repository.submit(
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
            record = await self._execute_and_publish(record=record, server=server, auth_token=auth_token, actor=actor)
        else:
            await self._publish(actor.operator_id, record.tool_call_id)
        return await self._wait_terminal(record.tool_call_id, actor, req.wait_for_ms)

    async def execute_direct(self, *, req: SubmitToolCallRequest, actor: ToolCallActor) -> dict[str, Any]:
        """Execute as the authenticated Operator without policy, ledger, events, or promises."""

        operator = self._require_operator(actor)
        server = _server_entry(self._settings, req.server_id)
        auth_token = await self._backend_auth(server, operator.operator_id)
        logger.info(
            "operator direct MCP call server=%s tool=%s operator_id=%s", server.id, req.tool_name, operator.operator_id
        )
        return await self._executor.execute(server, req.tool_name, req.arguments, auth_token)

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

    async def decide(
        self, *, tool_call_id: str, decision: ApprovalDecisionRequest, actor: ToolCallActor
    ) -> ToolCallRecord:
        operator = self._require_operator(actor)
        if decision.decision is ApprovalDecision.DENY:
            record = self._repository.deny(tool_call_id, decision.reason, actor=operator)
            await self._publish(operator.operator_id, record.tool_call_id)
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
        auth_token = await self._backend_auth(server, operator.operator_id)
        running = self._repository.mark_running(tool_call_id, actor=operator)
        return await self._execute_and_publish(record=running, server=server, auth_token=auth_token, actor=operator)

    async def _execute_and_publish(
        self, *, record: ToolCallRecord, server: McpServerEntry, auth_token: str | None, actor: ToolCallActor
    ) -> ToolCallRecord:
        if record.status != ToolCallStatus.RUNNING:
            return record
        execution_operator_id = self._repository.authorize_execution(record.tool_call_id, actor=actor)
        cancellation: asyncio.CancelledError | None = None
        try:
            result = await self._executor.execute(server, record.tool_name, record.arguments, auth_token)
        except asyncio.CancelledError as error:
            cancellation = error
            updated = self._repository.finish(
                record.tool_call_id, actor=actor, result=None, error="tool execution cancelled"
            )
        except Exception as error:
            updated = self._repository.finish(record.tool_call_id, actor=actor, result=None, error=str(error))
        else:
            updated = self._repository.finish(record.tool_call_id, actor=actor, result=result, error=None)
        # The durable row is authoritative. One invalidation after terminal persistence is enough:
        # observers re-read the complete record rather than replaying intermediate transitions.
        await self._publish(execution_operator_id, updated.tool_call_id)
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

    async def _publish(self, operator_id: UUID, tool_call_id: str) -> None:
        """Best-effort invalidation after durable changes; the repository remains authoritative."""
        try:
            await self._invalidation_publisher.tool_call_changed(operator_id, tool_call_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to publish tool-call invalidation for operator %s", operator_id)

    async def _wait_terminal(self, tool_call_id: str, actor: ToolCallActor, wait_for_ms: int) -> ToolCallRecord:
        record = self._repository.get(tool_call_id, actor=actor)
        if record.status in TERMINAL_STATUSES or wait_for_ms <= 0:
            return record

        loop = asyncio.get_running_loop()
        deadline = loop.time() + (wait_for_ms / 1000)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._repository.get(tool_call_id, actor=actor)

            # Subscribe before re-reading the authoritative row. A transition committed between
            # the first read and subscription is then observed by the second read; a transition
            # committed after it wakes this subscription through PostgreSQL LISTEN/NOTIFY.
            async with self._invalidation_publisher.subscribe(actor.operator_id, tool_call_id) as changed:
                record = self._repository.get(tool_call_id, actor=actor)
                if record.status in TERMINAL_STATUSES:
                    return record
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return record
                try:
                    await asyncio.wait_for(changed.wait(), timeout=remaining)
                except TimeoutError:
                    return self._repository.get(tool_call_id, actor=actor)

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
