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
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from haku.console.auto_approval.github import GitHubRepositoryVisibilityService
from haku.console.auto_approval.registry import AutoApprovalPolicyRegistry, PolicyDenial, auto_approve_tool_call
from haku.console.config import Settings
from haku.console.grants.kubernetes.authorization import KubernetesAuthorizationService
from haku.console.grants.principal import RequestPrincipal
from haku.console.mcp.execution import (
    AgentMcpExecutionCaller,
    McpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
)
from haku.console.mcp_config import (
    InProcessBackend,
    InProcessServers,
    McpServerEntry,
    NoCredential,
    OperatorConnectionCredential,
    OperatorLoginIdentityCredential,
    RemoteServerOAuthAuth,
    StaticBearerAuth,
    _credential_token,
    _server_entry,
    load_console_config,
)
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tool_calls import (
    ApprovalDecision,
    ApprovalDecisionRequest,
    SubmitToolCallRequest,
    ToolCallPayloadField,
    ToolCallRecord,
    ToolCallStatus,
)
from haku.console.tools.gmail_client import GMAIL_SERVER_ID, GmailToolsClient

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {ToolCallStatus.OK, ToolCallStatus.ERROR, ToolCallStatus.DENIED, ToolCallStatus.WITHDRAWN}

_CURSOR_SEPARATOR = "~"


def _execution_caller(actor: RuntimeActor) -> McpExecutionCaller:
    """The trusted execution identity one revalidated actor executes as."""
    match actor:
        case AgentActor() as agent:
            return AgentMcpExecutionCaller(principal=RequestPrincipal.from_source(agent))
        case OperatorActor(operator_id=operator_id):
            return OperatorMcpExecutionCaller(operator_id=operator_id)


@dataclass(frozen=True, slots=True)
class ToolCallPageCursor:
    """A keyset position in the ledger's `(created_at, tool_call_id)` order.

    The ledger pages by this rather than by offset: the history view refetches its first page on
    every live event, and an offset would skip or duplicate rows whenever a call was submitted in
    between. `tool_call_id` is the tiebreak that makes the order total, since a burst of calls can
    share a `created_at`.

    Its wire form is deliberately one opaque string: a client only ever echoes back the
    `next_cursor` it was handed, so the pagination key can change without a wire contract change.
    """

    created_at: datetime.datetime
    tool_call_id: str

    @classmethod
    def of(cls, record: ToolCallRecord) -> ToolCallPageCursor:
        return cls(created_at=record.created_at, tool_call_id=record.tool_call_id)

    @classmethod
    def parse(cls, value: str) -> ToolCallPageCursor:
        """Decode a `next_cursor` handed out by `encode`, raising `ValueError` on anything else."""
        timestamp, separator, tool_call_id = value.partition(_CURSOR_SEPARATOR)
        if not separator or not tool_call_id:
            raise ValueError(f"malformed tool-call cursor: {value!r}")
        return cls(created_at=datetime.datetime.fromisoformat(timestamp), tool_call_id=tool_call_id)

    def encode(self) -> str:
        # A tool_call_id is `tc_<hex>`, so it can never contain the separator.
        return f"{self.created_at.isoformat()}{_CURSOR_SEPARATOR}{self.tool_call_id}"


class ToolCallRepository(Protocol):
    async def submit(
        self,
        *,
        server: McpServerEntry,
        req: SubmitToolCallRequest,
        actor: RuntimeActor,
        auto_approval_policy_id: str | None = None,
        auto_approval_evaluation: str | None = None,
        auto_denial_reason: str | None = None,
    ) -> ToolCallRecord: ...

    async def get(
        self, tool_call_id: str, *, actor: RuntimeActor, fields: frozenset[ToolCallPayloadField] | None = None
    ) -> ToolCallRecord: ...

    async def list_tool_calls(
        self,
        *,
        actor: RuntimeActor,
        fields: frozenset[ToolCallPayloadField] | None = None,
        statuses: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        auto_approved: bool | None = None,
        limit: int = 100,
        newest_first: bool = False,
        cursor: ToolCallPageCursor | None = None,
    ) -> list[ToolCallRecord]: ...

    async def mark_running(self, tool_call_id: str, *, actor: OperatorActor) -> ToolCallRecord: ...

    async def deny(self, tool_call_id: str, reason: str | None, *, actor: OperatorActor) -> ToolCallRecord: ...

    async def withdraw(self, tool_call_id: str, reason: str | None, *, actor: AgentActor) -> ToolCallRecord: ...

    async def finish(
        self, tool_call_id: str, *, actor: RuntimeActor, result: dict[str, Any] | None, error: str | None
    ) -> ToolCallRecord: ...

    async def authorize_execution(
        self, tool_call_id: str, *, actor: RuntimeActor
    ) -> ToolCallExecutionAuthorization: ...


@dataclass(frozen=True, slots=True)
class ToolCallExecutionAuthorization:
    """The revalidated original caller and owning Operator for one executable ledger row."""

    operator_id: UUID
    caller: RuntimeActor


class ToolExecutor(Protocol):
    async def execute(
        self,
        server: McpServerEntry,
        tool_name: str,
        arguments: dict[str, Any],
        auth_token: str | None,
        execution_context: McpExecutionContext,
    ) -> dict[str, Any]: ...


class ToolCallInvalidationPublisher(Protocol):
    async def tool_call_changed(self, operator_id: UUID, tool_call_id: str) -> None: ...

    def subscribe(
        self, operator_id: UUID, tool_call_id: str
    ) -> contextlib.AbstractAsyncContextManager[asyncio.Event]: ...


class PendingApprovalNotifier(Protocol):
    """Out-of-band notice that a call entered or left the approval queue.

    Deliberately narrower than `ToolCallInvalidationPublisher`, which invalidates any view of any
    change for tabs that are already open. This fires only on the two edges that matter to an
    operator who is *not* looking at the console, and it is best-effort in the same way: the
    ledger row stays authoritative and a failed notification never fails the transition.
    """

    async def tool_call_pending(self, *, operator_id: UUID, record: ToolCallRecord) -> None: ...

    async def tool_call_resolved(self, *, operator_id: UUID, record: ToolCallRecord) -> None: ...


class OperatorOAuthTokenStore(Protocol):
    async def access_token_for(self, *, server: McpServerEntry, operator_id: UUID) -> str | None: ...


class ProviderConnectionTokenStore(Protocol):
    async def access_token_for(self, *, connection: str, operator_id: UUID) -> str | None: ...

    async def is_connected(self, *, connection: str, operator_id: UUID) -> bool: ...

    async def is_provisioned(self, *, connection: str) -> bool: ...


class AuthentikOperatorTokenStore(Protocol):
    async def access_token_for(self, *, operator_id: UUID) -> str | None: ...


# Resolves the acting Operator's Gmail client for auto-approval label lookups (or None when the
# Operator has no Google connection). Production builds it from the provider store; tests inject one.
GmailClientProvider = Callable[[UUID], Awaitable[GmailToolsClient | None]]


class OperatorActorRequiredError(PermissionError):
    """Raised when an AgentActor reaches an operator-only lifecycle operation."""


class AgentActorRequiredError(PermissionError):
    """Raised when an OperatorActor reaches an agent-only lifecycle operation."""


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


async def backend_auth_for_operator(
    *,
    server: McpServerEntry,
    operator_id: UUID,
    oauth_store: OperatorOAuthTokenStore,
    provider_store: ProviderConnectionTokenStore,
    authentik_store: AuthentikOperatorTokenStore,
) -> str | None:
    """Resolve the server's backend credential for the acting operator, per its ``auth`` variant.

    - ``OperatorConnectionCredential``: the operator's configured external-account token (Google).
    - ``RemoteServerOAuthAuth``: the operator's OAuth token at the remote MCP server itself.
    - ``OperatorLoginIdentityCredential``: the operator's own Authentik login token (captured via
      offline_access), which the server exchanges for a per-host token (hostexec); missing ⇒ the
      operator has not logged in with offline_access yet.
    - ``StaticBearerAuth``: the console's fixed configured bearer, not operator-scoped.
    - ``NoCredential``: none — the server carries its own credential.
    """
    credential = server.backend.credential if isinstance(server.backend, InProcessBackend) else server.backend.auth
    match credential:
        case OperatorConnectionCredential(connection=connection):
            return await _require_operator_linked_token(
                provider_store.access_token_for(connection=connection, operator_id=operator_id), server.id
            )
        case RemoteServerOAuthAuth():
            return await _require_operator_linked_token(
                oauth_store.access_token_for(server=server, operator_id=operator_id), server.id
            )
        case OperatorLoginIdentityCredential():
            return await _require_operator_linked_token(
                authentik_store.access_token_for(operator_id=operator_id), server.id
            )
        case StaticBearerAuth(bearer_token_secret=secret):
            return _credential_token(server.id, secret)
        case NoCredential():
            return None


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
        approval_notifier: PendingApprovalNotifier,
        gmail_client_provider: GmailClientProvider,
        kubernetes_authorization: KubernetesAuthorizationService | None = None,
        github_repository_visibility: GitHubRepositoryVisibilityService | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._invalidation_publisher = invalidation_publisher
        self._approval_notifier = approval_notifier
        self._executor = executor
        self._oauth_store = oauth_store
        self._in_process_servers = in_process_servers
        self._provider_store = provider_store
        self._authentik_token_store = authentik_token_store
        self._gmail_client_provider = gmail_client_provider
        self._kubernetes_authorization = kubernetes_authorization
        self._github_repository_visibility = github_repository_visibility
        self._auto_approval_policies = AutoApprovalPolicyRegistry(
            load_console_config(settings.config_file),
            kubernetes_authorization=self._kubernetes_authorization,
            github_repository_visibility=self._github_repository_visibility,
        )
        # In-flight background execution tasks dispatched by `decide`. Held so they aren't GC'd
        # mid-run, and drained/cancelled at shutdown (`aclose`).
        self._execution_tasks: set[asyncio.Task[ToolCallRecord]] = set()

    async def _backend_auth(self, server: McpServerEntry, operator_id: UUID) -> str | None:
        return await backend_auth_for_operator(
            server=server,
            operator_id=operator_id,
            oauth_store=self._oauth_store,
            provider_store=self._provider_store,
            authentik_store=self._authentik_token_store,
        )

    async def submit_and_wait(self, *, req: SubmitToolCallRequest, actor: RuntimeActor) -> ToolCallRecord:
        actor = self._require_actor(actor)
        server = _server_entry(self._settings, req.server_id)
        # Gmail label auto-approval resolves label IDs against the acting Operator's own Gmail; the
        # schema check needs the tool's input schema, so build the (credential-independent) server.
        gmail = await self._gmail_client_provider(actor.operator_id) if server.id == GMAIL_SERVER_ID else None
        server_builder = self._in_process_servers.get(server.id)
        authorizer = server_builder.authorizer if server_builder is not None else None
        if authorizer is not None and (authorization_denial := authorizer(actor, req.tool_name, req.arguments)):
            record = await self._repository.submit(
                server=server,
                req=req,
                actor=actor,
                auto_approval_evaluation="denied: trusted in-process authorization",
                auto_denial_reason=authorization_denial,
            )
            logger.info(
                "tool call %s auto-denied (in-process authorization) server=%s tool=%s caller=%s reason=%r",
                record.tool_call_id,
                record.server_id,
                record.tool_name,
                record.caller,
                record.denial_reason,
            )
            await self._publish(actor.operator_id, record.tool_call_id)
            return record
        decision = await auto_approve_tool_call(
            policies=self._auto_approval_policies,
            actor=actor,
            server_id=server.id,
            tool_name=req.tool_name,
            arguments=req.arguments,
            gmail=gmail,
            mcp=server_builder.builder(None) if server_builder is not None else None,
        )
        if isinstance(decision, PolicyDenial):
            # Schema failure or policy auto-denial: the call can never execute, so it is
            # persisted born-denied (full audit row, never pending).
            record = await self._repository.submit(
                server=server,
                req=req,
                actor=actor,
                auto_approval_evaluation=decision.evaluation,
                auto_denial_reason=decision.reason,
            )
            logger.info(
                "tool call %s auto-denied (policy/schema) server=%s tool=%s caller=%s reason=%r",
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

        record = await self._repository.submit(
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
            await self._notify_pending(actor.operator_id, record)
        return await self._wait_terminal(record.tool_call_id, actor, req.wait_for_ms)

    async def execute_direct(self, *, req: SubmitToolCallRequest, actor: RuntimeActor) -> dict[str, Any]:
        """Execute as the authenticated Operator without policy, ledger, events, or promises."""

        operator = self._require_operator(actor)
        server = _server_entry(self._settings, req.server_id)
        auth_token = await self._backend_auth(server, operator.operator_id)
        logger.info(
            "operator direct MCP call server=%s tool=%s operator_id=%s", server.id, req.tool_name, operator.operator_id
        )
        return await self._executor.execute(
            server,
            req.tool_name,
            req.arguments,
            auth_token,
            McpExecutionContext(
                caller=OperatorMcpExecutionCaller(operator_id=operator.operator_id),
                tool_call_id=None,
                approving_operator_id=None,
                approval_policy_id=None,
            ),
        )

    async def get(
        self, tool_call_id: str, *, actor: RuntimeActor, fields: frozenset[ToolCallPayloadField] | None = None
    ) -> ToolCallRecord:
        return await self._repository.get(tool_call_id, actor=self._require_actor(actor), fields=fields)

    async def list_tool_calls(
        self,
        *,
        actor: RuntimeActor,
        fields: frozenset[ToolCallPayloadField] | None = None,
        statuses: list[ToolCallStatus] | None = None,
        since: datetime.datetime | None = None,
        auto_approved: bool | None = None,
        limit: int = 100,
        newest_first: bool = False,
        cursor: ToolCallPageCursor | None = None,
    ) -> list[ToolCallRecord]:
        return await self._repository.list_tool_calls(
            actor=self._require_actor(actor),
            fields=fields,
            statuses=statuses,
            since=since,
            auto_approved=auto_approved,
            limit=limit,
            newest_first=newest_first,
            cursor=cursor,
        )

    async def pending_approvals(self, *, actor: RuntimeActor) -> list[ToolCallRecord]:
        operator = self._require_operator(actor)
        return await self._repository.list_tool_calls(actor=operator, statuses=[ToolCallStatus.PENDING_APPROVAL])

    async def decide(
        self, *, tool_call_id: str, decision: ApprovalDecisionRequest, actor: RuntimeActor
    ) -> ToolCallRecord:
        operator = self._require_operator(actor)
        if decision.decision is ApprovalDecision.DENY:
            record = await self._repository.deny(tool_call_id, decision.reason, actor=operator)
            await self._publish(operator.operator_id, record.tool_call_id)
            await self._notify_resolved(operator.operator_id, record)
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

        pending = await self._repository.get(tool_call_id, actor=operator)
        server = _server_entry(self._settings, pending.server_id)
        auth_token = await self._backend_auth(server, operator.operator_id)
        running = await self._repository.mark_running(tool_call_id, actor=operator)
        # The ask is settled the moment it is approved, even though the run has not started. Retract
        # here rather than at terminal, so a notification on another device stops offering buttons
        # for a decision that has already been made.
        await self._notify_resolved(operator.operator_id, running)
        # Deciding is not executing. Dispatch the tool run as a tracked background task and return the
        # RUNNING record immediately, so approving never blocks on a slow or unreachable backend (e.g.
        # an offline roaming hostexec target). The terminal state reaches observers the same way an
        # auto-approved call's does: _publish (WS invalidation) plus the durable row agents poll.
        self._dispatch_execution(record=running, server=server, auth_token=auth_token, actor=operator)
        return running

    async def withdraw(self, *, tool_call_id: str, reason: str | None, actor: RuntimeActor) -> ToolCallRecord:
        """Retract an Agent's own pending call. Operators decide; only the requester withdraws.

        Deliberately not exposed to `OperatorActor`: an operator's verb is `deny`, and letting one
        write `withdrawn` would record the agent as having retracted a request the operator in fact
        dismissed — destroying the distinction the status exists to draw.
        """
        agent = self._require_agent(actor)
        record = await self._repository.withdraw(tool_call_id, reason, actor=agent)
        logger.info(
            "tool call %s withdrawn server=%s tool=%s agent=%s reason=%r",
            record.tool_call_id,
            record.server_id,
            record.tool_name,
            agent.agent_id,
            reason,
        )
        # The owning operator is `agent.operator_id`: the repository's binding revalidation proves
        # the agent is owned by it, so this is the same verified publication target `submit` uses.
        await self._publish(agent.operator_id, record.tool_call_id)
        # Only a pending call can be withdrawn, so this always retracts a notification the operator
        # may be looking at — the requester no longer wants the answer.
        await self._notify_resolved(agent.operator_id, record)
        return record

    async def _execute_and_publish(
        self, *, record: ToolCallRecord, server: McpServerEntry, auth_token: str | None, actor: RuntimeActor
    ) -> ToolCallRecord:
        if record.status != ToolCallStatus.RUNNING:
            return record
        execution = await self._repository.authorize_execution(record.tool_call_id, actor=actor)
        execution_context = self._execution_context(record, actor, execution)
        cancellation: asyncio.CancelledError | None = None
        try:
            result = await self._executor.execute(
                server, record.tool_name, record.arguments, auth_token, execution_context
            )
        except asyncio.CancelledError as error:
            cancellation = error
            updated = await self._repository.finish(
                record.tool_call_id, actor=actor, result=None, error="tool execution cancelled"
            )
        except Exception as error:
            updated = await self._repository.finish(record.tool_call_id, actor=actor, result=None, error=str(error))
        else:
            updated = await self._repository.finish(record.tool_call_id, actor=actor, result=result, error=None)
        # The durable row is authoritative. One invalidation after terminal persistence is enough:
        # observers re-read the complete record rather than replaying intermediate transitions.
        await self._publish(execution.operator_id, updated.tool_call_id)
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

    def _dispatch_execution(
        self, *, record: ToolCallRecord, server: McpServerEntry, auth_token: str | None, actor: RuntimeActor
    ) -> None:
        """Run an approved call's execution as a tracked background task (see `decide`)."""
        task = asyncio.create_task(
            self._execute_and_publish(record=record, server=server, auth_token=auth_token, actor=actor)
        )
        self._execution_tasks.add(task)
        task.add_done_callback(self._on_execution_done)

    @staticmethod
    def _execution_context(
        record: ToolCallRecord, deciding_actor: RuntimeActor, execution: ToolCallExecutionAuthorization
    ) -> McpExecutionContext:
        """Build trusted explicit execution identity after the repository revalidates the caller."""

        return McpExecutionContext(
            caller=_execution_caller(execution.caller),
            tool_call_id=record.tool_call_id,
            approving_operator_id=deciding_actor.operator_id if isinstance(deciding_actor, OperatorActor) else None,
            approval_policy_id=record.approval_policy_id,
        )

    def _on_execution_done(self, task: asyncio.Task[ToolCallRecord]) -> None:
        self._execution_tasks.discard(task)
        # _execute_and_publish records failures on the row and only re-raises CancelledError, so a
        # non-cancelled exception escaping here is a bug in the orchestration itself — surface it.
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("tool-call execution task crashed", exc_info=error)

    async def join_executions(self) -> None:
        """Await in-flight execution tasks to completion without cancelling — a graceful drain (as
        opposed to `aclose`'s cancel). Lets a caller wait for approved calls to finish."""
        while self._execution_tasks:
            await asyncio.gather(*tuple(self._execution_tasks), return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel in-flight execution tasks and wait for them to settle. Each terminalizes its row as
        cancelled (see `_execute_and_publish`); with the console's Recreate rollout a bounded cancel is
        the clean shutdown. Called from the app lifespan before the event hub closes."""
        tasks = tuple(self._execution_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _publish(self, operator_id: UUID, tool_call_id: str) -> None:
        """Best-effort invalidation after durable changes; the repository remains authoritative."""
        try:
            await self._invalidation_publisher.tool_call_changed(operator_id, tool_call_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to publish tool-call invalidation for operator %s", operator_id)

    async def _notify_pending(self, operator_id: UUID, record: ToolCallRecord) -> None:
        try:
            await self._approval_notifier.tool_call_pending(operator_id=operator_id, record=record)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to notify pending approval %s", record.tool_call_id)

    async def _notify_resolved(self, operator_id: UUID, record: ToolCallRecord) -> None:
        """Retract the notification for a call that has left the queue.

        Called only where the call was `pending_approval` immediately before the transition, so a
        never-notified call (auto-approved, born-denied) never produces a spurious retraction —
        which matters because every push spends the browser's budget for showing them.
        """
        try:
            await self._approval_notifier.tool_call_resolved(operator_id=operator_id, record=record)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to notify resolved approval %s", record.tool_call_id)

    async def _wait_terminal(self, tool_call_id: str, actor: RuntimeActor, wait_for_ms: int) -> ToolCallRecord:
        record = await self._repository.get(tool_call_id, actor=actor)
        if record.status in TERMINAL_STATUSES or wait_for_ms <= 0:
            return record

        loop = asyncio.get_running_loop()
        deadline = loop.time() + (wait_for_ms / 1000)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return await self._repository.get(tool_call_id, actor=actor)

            # Subscribe before re-reading the authoritative row. A transition committed between
            # the first read and subscription is then observed by the second read; a transition
            # committed after it wakes this subscription through PostgreSQL LISTEN/NOTIFY.
            async with self._invalidation_publisher.subscribe(actor.operator_id, tool_call_id) as changed:
                record = await self._repository.get(tool_call_id, actor=actor)
                if record.status in TERMINAL_STATUSES:
                    return record
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return record
                try:
                    await asyncio.wait_for(changed.wait(), timeout=remaining)
                except TimeoutError:
                    return await self._repository.get(tool_call_id, actor=actor)

    @staticmethod
    def _require_actor(actor: RuntimeActor) -> RuntimeActor:
        match actor:
            case AgentActor() | OperatorActor():
                return actor
            case _:
                raise TypeError(f"unsupported tool-call actor: {type(actor).__name__}")

    @staticmethod
    def _require_operator(actor: RuntimeActor) -> OperatorActor:
        if not isinstance(actor, OperatorActor):
            raise OperatorActorRequiredError("operator actor required")
        return actor

    @staticmethod
    def _require_agent(actor: RuntimeActor) -> AgentActor:
        if not isinstance(actor, AgentActor):
            raise AgentActorRequiredError("agent actor required")
        return actor
