"""Authorization matrix and lifecycle invariants for the Haku tool-call application service."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.agents.models import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.authentik_operator_token import PostgresAuthentikOperatorTokenStore
from haku.console.conftest import console_settings, write_config
from haku.console.database_schema import Agent, AgentNameReservation, CredentialBinding, StaticCredential
from haku.console.grant_principal import RequestPrincipal
from haku.console.mcp_approval import PostgresToolCallLedger
from haku.console.mcp_config import (
    AccessProfile,
    InProcessCredentialKind,
    InProcessServerRegistration,
    InProcessServers,
    McpServerEntry,
    McpServerNotFoundError,
    NoCredential,
    RemoteMcpBackend,
)
from haku.console.mcp_execution import AgentMcpExecutionCaller, McpExecutionContext
from haku.console.oauth.provider_connection import PostgresProviderConnectionStore
from haku.console.oauth.token_state import PostgresTokenStateStore
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.recall_index_access import RecallIndexAccessPolicy
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tool_call_service import (
    AgentActorRequiredError,
    BackendAccountNotConnectedError,
    OperatorActorRequiredError,
    ToolCallApplicationService,
    ToolCallNotFoundError,
    ToolCallStateConflictError,
)
from haku.console.tool_calls import (
    ApprovalDecision,
    ApprovalDecisionRequest,
    SubmitToolCallRequest,
    ToolCallRecord,
    ToolCallStatus,
)


class _RecordingApprovalNotifier:
    """Records which tool-call edges asked for an out-of-band notification.

    `pending`/`resolved` are separate lists because the whole contract is that they pair up: a
    call notified once and retracted once, and never a retraction for a call that was never
    shown (which would spend a browser's push budget on nothing).
    """

    def __init__(self) -> None:
        self.pending: list[tuple[UUID, str]] = []
        self.resolved: list[tuple[UUID, str, ToolCallStatus]] = []

    async def tool_call_pending(self, *, operator_id: UUID, record: ToolCallRecord) -> None:
        self.pending.append((operator_id, record.tool_call_id))

    async def tool_call_resolved(self, *, operator_id: UUID, record: ToolCallRecord) -> None:
        self.resolved.append((operator_id, record.tool_call_id, record.status))


class _RecordingInvalidationPublisher:
    def __init__(self) -> None:
        self.publications: list[tuple[UUID, str]] = []
        self.subscribed = asyncio.Event()
        self._waiters: dict[tuple[UUID, str], set[asyncio.Event]] = {}

    async def tool_call_changed(self, operator_id: UUID, tool_call_id: str) -> None:
        self.publications.append((operator_id, tool_call_id))
        for waiter in self._waiters.get((operator_id, tool_call_id), ()):
            waiter.set()

    @contextlib.asynccontextmanager
    async def subscribe(self, operator_id: UUID, tool_call_id: str) -> AsyncIterator[asyncio.Event]:
        key = (operator_id, tool_call_id)
        changed = asyncio.Event()
        self._waiters.setdefault(key, set()).add(changed)
        self.subscribed.set()
        try:
            yield changed
        finally:
            waiters = self._waiters[key]
            waiters.remove(changed)
            if not waiters:
                self._waiters.pop(key)


class _RaisingInvalidationPublisher(_RecordingInvalidationPublisher):
    async def tool_call_changed(self, operator_id: UUID, tool_call_id: str) -> None:
        await super().tool_call_changed(operator_id, tool_call_id)
        raise RuntimeError("invalidation transport unavailable")


class _TransitionBeforeYieldInvalidationPublisher(_RecordingInvalidationPublisher):
    def __init__(self) -> None:
        super().__init__()
        self.transition: Callable[[], Awaitable[None]] | None = None

    @contextlib.asynccontextmanager
    async def subscribe(self, operator_id: UUID, tool_call_id: str) -> AsyncIterator[asyncio.Event]:
        key = (operator_id, tool_call_id)
        changed = asyncio.Event()
        self._waiters.setdefault(key, set()).add(changed)
        self.subscribed.set()
        assert self.transition is not None
        await self.transition()
        try:
            yield changed
        finally:
            waiters = self._waiters[key]
            waiters.remove(changed)
            if not waiters:
                self._waiters.pop(key)


class _RecordingExecutor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, str, dict[str, Any], str | None, McpExecutionContext]] = []

    async def execute(
        self,
        server: McpServerEntry,
        tool_name: str,
        arguments: dict[str, Any],
        auth_token: str | None,
        execution_context: McpExecutionContext,
    ) -> dict[str, Any]:
        self.executions.append((server.id, tool_name, arguments, auth_token, execution_context))
        return {"content": [{"type": "text", "text": f"{server.id}:{tool_name}"}], "isError": False}


class _CancellingExecutor(_RecordingExecutor):
    async def execute(
        self,
        server: McpServerEntry,
        tool_name: str,
        arguments: dict[str, Any],
        auth_token: str | None,
        execution_context: McpExecutionContext,
    ) -> dict[str, Any]:
        self.executions.append((server.id, tool_name, arguments, auth_token, execution_context))
        raise asyncio.CancelledError


class _BlockingExecutor(_RecordingExecutor):
    """Blocks in execute() until its task is cancelled, so a dispatched execution stays in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def execute(
        self,
        server: McpServerEntry,
        tool_name: str,
        arguments: dict[str, Any],
        auth_token: str | None,
        execution_context: McpExecutionContext,
    ) -> dict[str, Any]:
        self.executions.append((server.id, tool_name, arguments, auth_token, execution_context))
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable: blocking executor is only released by cancellation")


class _OperatorTokens:
    def __init__(self, tokens: dict[UUID, str]) -> None:
        self.tokens = tokens
        self.lookups: list[UUID] = []

    async def access_token_for(self, *, server: McpServerEntry, operator_id: UUID) -> str | None:
        self.lookups.append(operator_id)
        return self.tokens.get(operator_id)


class _RecordingLedger(PostgresToolCallLedger):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions)
        self.finish_actors: list[RuntimeActor] = []

    async def finish(
        self, tool_call_id: str, *, actor: RuntimeActor, result: dict[str, Any] | None, error: str | None
    ) -> ToolCallRecord:
        self.finish_actors.append(actor)
        return await super().finish(tool_call_id, actor=actor, result=result, error=error)


@pytest.fixture
async def actors(
    migrated_engine: AsyncEngine, migrated_identity_store: PostgresOperatorIdentityStore
) -> dict[str, RuntimeActor]:
    identities = migrated_identity_store
    operator_ids = {
        "a": await identities.resolve_configured_external_user_key("service-operator-a"),
        "b": await identities.resolve_configured_external_user_key("service-operator-b"),
    }

    async def agent(name: str, operator_id: UUID) -> AgentActor:
        agent_id = uuid4()
        reservation_id = uuid4()
        binding_id = uuid4()
        now = datetime.datetime.now(datetime.UTC)
        sessions = async_sessionmaker(migrated_engine, expire_on_commit=False)
        async with sessions.begin() as session:
            session.add(
                Agent(
                    agent_id=agent_id,
                    owner_operator_id=operator_id,
                    current_name_reservation_id=reservation_id,
                    status=AgentStatus.ACTIVE,
                    created_at=now,
                    updated_at=now,
                    activated_at=now,
                )
            )
            await session.flush()
            session.add_all(
                [
                    AgentNameReservation(
                        reservation_id=reservation_id,
                        display_name=name,
                        display_name_key=name,
                        originating_interaction_id=None,
                        pending_interaction_id=None,
                        agent_id=agent_id,
                        created_at=now,
                        activated_at=now,
                    ),
                    CredentialBinding(
                        binding_id=binding_id,
                        agent_id=agent_id,
                        kind=CredentialKind.STATIC,
                        status=CredentialBindingStatus.ACTIVE,
                        generation=1,
                        supersedes_binding_id=None,
                        created_at=now,
                        updated_at=now,
                        issued_at=now,
                        activated_at=now,
                        ended_at=None,
                        end_reason=None,
                    ),
                    StaticCredential(
                        binding_id=binding_id,
                        secret_reference=f"test-tool-call-service/{binding_id}",
                        credential_fingerprint=hashlib.sha256(binding_id.bytes).digest(),
                        created_at=now,
                    ),
                ]
            )
        return AgentActor(agent_id=agent_id, operator_id=operator_id, binding_id=binding_id)

    actors: dict[str, RuntimeActor] = {
        "oa": OperatorActor(operator_id=operator_ids["a"]),
        "ob": OperatorActor(operator_id=operator_ids["b"]),
        "aa1": await agent("agent-a1", operator_ids["a"]),
        "ab1": await agent("agent-b1", operator_ids["b"]),
        "aa2": await agent("agent-a2", operator_ids["a"]),
        "ab2": await agent("agent-b2", operator_ids["b"]),
    }
    return actors


@pytest.fixture
def ledger(migrated_sessions: async_sessionmaker[AsyncSession]) -> _RecordingLedger:
    return _RecordingLedger(migrated_sessions)


@pytest.fixture
def publisher() -> _RecordingInvalidationPublisher:
    return _RecordingInvalidationPublisher()


@pytest.fixture
def executor() -> _RecordingExecutor:
    return _RecordingExecutor()


@pytest.fixture
def notifier() -> _RecordingApprovalNotifier:
    return _RecordingApprovalNotifier()


@pytest.fixture
def tokens(actors: dict[str, RuntimeActor]) -> _OperatorTokens:
    return _OperatorTokens({actors["oa"].operator_id: "token-a", actors["ob"].operator_id: "token-b"})


async def _no_gmail_client(_operator_id: UUID) -> None: ...


def _service(
    *,
    database_url: str,
    tmp_path: Path,
    sessions: async_sessionmaker[AsyncSession],
    identity_store: PostgresOperatorIdentityStore,
    ledger: PostgresToolCallLedger,
    publisher: _RecordingInvalidationPublisher,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
    notifier: _RecordingApprovalNotifier,
    servers: list[dict[str, Any]] | None = None,
    in_process_servers: InProcessServers | None = None,
) -> ToolCallApplicationService:
    token_states = PostgresTokenStateStore(sessions, operator_identity_store=identity_store)
    config_file = write_config(
        tmp_path / "tool-call-service.yaml",
        {
            "auto_approval_policies": [{"id": "manual", "type": "never"}],
            "access_profiles": [{"id": "manual", "auto_approval_policy": "manual"}],
            "default_access_profile_id": "manual",
            "mcp": {
                "servers": servers
                or [
                    {
                        "id": "operator-backend",
                        "backend": {
                            "kind": "remote_mcp",
                            "url": "https://backend.invalid/mcp",
                            "auth": {
                                "kind": "remote_server_oauth",
                                "client_registration": {"kind": "dynamic", "client_name": "Haku Console"},
                            },
                        },
                    }
                ]
            },
        },
    )
    return ToolCallApplicationService(
        settings=console_settings(database_url, config_file=config_file),
        repository=ledger,
        invalidation_publisher=publisher,
        executor=executor,
        oauth_store=tokens,
        in_process_servers=in_process_servers or {},
        provider_store=PostgresProviderConnectionStore(
            sessions,
            operator_identity_store=identity_store,
            token_states=token_states,
            provider_definitions={},
            provider_clients={},
            operator_connections={},
        ),
        authentik_token_store=PostgresAuthentikOperatorTokenStore(
            sessions,
            operator_identity_store=identity_store,
            token_states=token_states,
            client_id="test-client",
            client_secret="test-secret",
            issuer="https://auth.test/application/o/haku-console/",
        ),
        approval_notifier=notifier,
        gmail_client_provider=_no_gmail_client,
    )


@pytest.fixture
def service(
    *,
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    migrated_identity_store: PostgresOperatorIdentityStore,
    tmp_path: Path,
    ledger: _RecordingLedger,
    publisher: _RecordingInvalidationPublisher,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
    notifier: _RecordingApprovalNotifier,
) -> ToolCallApplicationService:
    return _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        sessions=migrated_sessions,
        identity_store=migrated_identity_store,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
        notifier=notifier,
    )


def _request(*, owner: str, wait_for_ms: int = 0) -> SubmitToolCallRequest:
    return SubmitToolCallRequest(
        server_id="operator-backend",
        tool_name="mutate",
        arguments={"owner": owner},
        rationale=f"exercise {owner}",
        wait_for_ms=wait_for_ms,
    )


async def _always_approve(**_: Any) -> tuple[str, str]:
    return "policy:test", "approved by test policy"


async def test_operator_direct_execution_has_no_ledger_or_invalidation_side_effects(
    actors: dict[str, RuntimeActor],
    publisher: _RecordingInvalidationPublisher,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
    service: ToolCallApplicationService,
) -> None:
    operator = actors["oa"]
    assert isinstance(operator, OperatorActor)

    result = await service.execute_direct(req=_request(owner="browser"), actor=operator)

    assert result["content"][0]["text"] == "operator-backend:mutate"
    assert len(executor.executions) == 1
    server_id, tool_name, arguments, token, execution_context = executor.executions[0]
    assert (server_id, tool_name, arguments, token) == ("operator-backend", "mutate", {"owner": "browser"}, "token-a")
    assert execution_context.tool_call_id is None
    assert tokens.lookups == [operator.operator_id]
    assert await service.list_tool_calls(actor=operator) == []
    assert publisher.publications == []

    with pytest.raises(OperatorActorRequiredError, match="operator actor required"):
        await service.execute_direct(req=_request(owner="agent"), actor=actors["aa1"])


async def test_recall_index_authorizer_denies_argument_escalation_before_submission(
    *,
    actors: dict[str, RuntimeActor],
    executor: _RecordingExecutor,
    ledger: _RecordingLedger,
    migrated_db_url: str,
    migrated_identity_store: PostgresOperatorIdentityStore,
    migrated_sessions: async_sessionmaker[AsyncSession],
    notifier: _RecordingApprovalNotifier,
    publisher: _RecordingInvalidationPublisher,
    tmp_path: Path,
    tokens: _OperatorTokens,
) -> None:
    """The caller identity is trusted; an MCP argument can never add another logical index."""
    stored_agent = actors["aa1"]
    assert isinstance(stored_agent, AgentActor)
    actor = replace(stored_agent, access_profile_id="public-coder")
    access = RecallIndexAccessPolicy(
        (AccessProfile(id="public-coder", auto_approval_policy="manual", recall_index_ids={"ducktape-public"}),),
        configured_index_ids=("ducktape-public",),
    )

    def unexpected_builder(_: str | None) -> Never:
        raise AssertionError("an unauthorized request must not build an MCP server")

    service = _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        sessions=migrated_sessions,
        identity_store=migrated_identity_store,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
        notifier=notifier,
        servers=[{"id": "haku_index", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}],
        in_process_servers={
            "haku_index": InProcessServerRegistration(
                builder=unexpected_builder,
                credential_kind=InProcessCredentialKind.NONE,
                authorizer=access.authorize_index_tool,
            )
        },
    )

    record = await service.submit_and_wait(
        req=SubmitToolCallRequest(
            server_id="haku_index",
            tool_name="search",
            arguments={"query": "private state", "index_id": "haku-state"},
            rationale="attempted index escalation",
            wait_for_ms=0,
        ),
        actor=actor,
    )

    assert (record.status, record.denial_reason) == (ToolCallStatus.DENIED, "recall index access denied")
    assert executor.executions == []
    assert notifier.pending == []


async def test_two_operator_two_agent_authorization_matrix(
    *,
    actors: dict[str, RuntimeActor],
    publisher: _RecordingInvalidationPublisher,
    executor: _RecordingExecutor,
    ledger: _RecordingLedger,
    tokens: _OperatorTokens,
    service: ToolCallApplicationService,
) -> None:
    """Every service read and lifecycle transition is scoped from the authenticated actor."""
    records = {
        name: await service.submit_and_wait(req=_request(owner=name), actor=actor) for name, actor in actors.items()
    }
    assert all(record.status is ToolCallStatus.PENDING_APPROVAL for record in records.values())

    invalid_actor: Any = SimpleNamespace(operator_id=actors["oa"].operator_id)
    with pytest.raises(TypeError, match="unsupported tool-call actor"):
        await service.submit_and_wait(req=_request(owner="lookalike"), actor=invalid_actor)
    with pytest.raises(TypeError, match="unsupported tool-call actor"):
        await service.list_tool_calls(actor=invalid_actor)
    with pytest.raises(TypeError, match="unsupported tool-call actor"):
        await ledger.submit(
            server=McpServerEntry(
                id="operator-backend", backend=RemoteMcpBackend(url="https://backend.invalid/mcp", auth=NoCredential())
            ),
            req=_request(owner="lookalike"),
            actor=invalid_actor,
        )
    with pytest.raises(TypeError, match="unsupported tool-call actor"):
        await ledger.get(records["oa"].tool_call_id, actor=invalid_actor)
    agent_as_operator: Any = actors["aa1"]
    with pytest.raises(TypeError, match="operator actor required"):
        await ledger.mark_running(records["aa1"].tool_call_id, actor=agent_as_operator)

    expected_visible = {
        "oa": {"oa", "aa1", "aa2"},
        "aa1": {"aa1"},
        "aa2": {"aa2"},
        "ob": {"ob", "ab1", "ab2"},
        "ab1": {"ab1"},
        "ab2": {"ab2"},
    }
    # This is the exact 6 readers x 6 records cross-product: two Operators and two Agents each.
    for reader_name, reader in actors.items():
        visible = expected_visible[reader_name]
        for owner_name, record in records.items():
            if owner_name in visible:
                assert await service.get(record.tool_call_id, actor=reader) == record
            else:
                with pytest.raises(ToolCallNotFoundError, match="tool call not found"):
                    await service.get(record.tool_call_id, actor=reader)
        listed = await service.list_tool_calls(actor=reader)
        assert {record.tool_call_id for record in listed} == {records[owner].tool_call_id for owner in visible}

    for operator_name, owned_names in (("oa", {"oa", "aa1", "aa2"}), ("ob", {"ob", "ab1", "ab2"})):
        pending = await service.pending_approvals(actor=actors[operator_name])
        assert {record.tool_call_id for record in pending} == {records[owner].tool_call_id for owner in owned_names}

    for agent_name in ("aa1", "aa2", "ab1", "ab2"):
        with pytest.raises(OperatorActorRequiredError):
            await service.pending_approvals(actor=actors[agent_name])
        with pytest.raises(OperatorActorRequiredError):
            await service.decide(
                tool_call_id=records[agent_name].tool_call_id,
                decision=ApprovalDecisionRequest(decision=ApprovalDecision.DENY),
                actor=actors[agent_name],
            )

    # A foreign decision is indistinguishable from a missing call and has no side effects.
    baseline = (list(tokens.lookups), list(executor.executions), list(publisher.publications))
    with pytest.raises(ToolCallNotFoundError, match="tool call not found"):
        await service.decide(
            tool_call_id=records["ab2"].tool_call_id,
            decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
            actor=actors["oa"],
        )
    assert (tokens.lookups, executor.executions, publisher.publications) == baseline

    approved_a = await service.decide(
        tool_call_id=records["aa1"].tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
        actor=actors["oa"],
    )
    approved_b = await service.decide(
        tool_call_id=records["ab1"].tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
        actor=actors["ob"],
    )
    denied = await service.decide(
        tool_call_id=records["aa2"].tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.DENY, reason="no"),
        actor=actors["oa"],
    )
    # decide() now returns the RUNNING record and runs the tool in the background; deny stays terminal.
    assert [approved_a.status, approved_b.status, denied.status] == [
        ToolCallStatus.RUNNING,
        ToolCallStatus.RUNNING,
        ToolCallStatus.DENIED,
    ]
    # Backend auth is resolved synchronously inside decide (before dispatch), so lookups are ordered
    # even before the background executions run.
    assert tokens.lookups == [actors["oa"].operator_id, actors["ob"].operator_id]
    await service.join_executions()
    # Sorted, not in decision order: `decide()` dispatches each execution as a background task, so
    # which one reaches the executor first is a race. What matters is that each ran under its own
    # Operator's token — the ordering intent is already covered by the `tokens.lookups` assertion
    # above, which is deterministic because auth resolves synchronously inside `decide()`.
    assert sorted(execution[3] or "" for execution in executor.executions) == ["token-a", "token-b"]
    # Approval belongs to an Operator, but actor-scoped in-process tools must execute as the
    # original Agent rather than gaining the approving Operator's broader access.
    expected_agent_ids = {actor.agent_id for actor in (actors["aa1"], actors["ab1"]) if isinstance(actor, AgentActor)}
    assert {
        execution[4].caller.principal.agent_id
        for execution in executor.executions
        if isinstance(execution[4].caller, AgentMcpExecutionCaller)
    } == expected_agent_ids
    assert [
        (await service.get(records["aa1"].tool_call_id, actor=actors["oa"])).status,
        (await service.get(records["ab1"].tool_call_id, actor=actors["ob"])).status,
    ] == [ToolCallStatus.OK, ToolCallStatus.OK]


async def test_pending_wait_uses_actor_scoped_event_invalidation(
    actors: dict[str, RuntimeActor], publisher: _RecordingInvalidationPublisher, service: ToolCallApplicationService
) -> None:
    agent = actors["aa1"]
    operator = actors["oa"]

    waiting = asyncio.create_task(service.submit_and_wait(req=_request(owner="wait", wait_for_ms=1000), actor=agent))
    await asyncio.wait_for(publisher.subscribed.wait(), timeout=1)
    [pending] = await service.list_tool_calls(actor=agent)
    assert pending.status is ToolCallStatus.PENDING_APPROVAL

    decided = await service.decide(
        tool_call_id=pending.tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
        actor=operator,
    )
    completed = await asyncio.wait_for(waiting, timeout=1)

    # decide returns the RUNNING record; the waiting agent observes the terminal OK the background
    # execution publishes.
    assert decided.status is ToolCallStatus.RUNNING
    assert completed.status is ToolCallStatus.OK
    await service.join_executions()
    assert publisher._waiters == {}


async def test_pending_wait_rereads_after_subscribing_before_waiting(
    *,
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    migrated_identity_store: PostgresOperatorIdentityStore,
    tmp_path: Path,
    actors: dict[str, RuntimeActor],
    ledger: _RecordingLedger,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
    notifier: _RecordingApprovalNotifier,
) -> None:
    agent = actors["aa1"]
    operator = actors["oa"]
    publisher = _TransitionBeforeYieldInvalidationPublisher()
    service = _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        sessions=migrated_sessions,
        identity_store=migrated_identity_store,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
        notifier=notifier,
    )

    # A deny is a synchronous terminal transition (unlike approve, which now dispatches execution to a
    # background task), so it lands in the durable row within the subscribe window — exactly the
    # race this test exercises: _wait_terminal must re-read after subscribing and observe it.
    async def transition_after_subscription_registration() -> None:
        [pending] = await service.list_tool_calls(actor=agent)
        decided = await service.decide(
            tool_call_id=pending.tool_call_id,
            decision=ApprovalDecisionRequest(decision=ApprovalDecision.DENY, reason="reread race"),
            actor=operator,
        )
        assert decided.status is ToolCallStatus.DENIED

    publisher.transition = transition_after_subscription_registration

    completed = await asyncio.wait_for(
        service.submit_and_wait(req=_request(owner="subscribe-reread", wait_for_ms=1000), actor=agent), timeout=1
    )

    assert completed.status is ToolCallStatus.DENIED
    assert publisher._waiters == {}


async def test_auto_approval_resolves_auth_before_persistence_and_finishes_as_agent(
    *,
    actors: dict[str, RuntimeActor],
    ledger: _RecordingLedger,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
    service: ToolCallApplicationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("haku.console.tool_call_service.auto_approve_tool_call", _always_approve)

    submitted_actors = [actors["aa1"], actors["ab1"]]
    completed = [
        await service.submit_and_wait(req=_request(owner=f"auto-{index}"), actor=actor)
        for index, actor in enumerate(submitted_actors)
    ]
    assert [record.status for record in completed] == [ToolCallStatus.OK, ToolCallStatus.OK]
    assert [execution[3] for execution in executor.executions] == ["token-a", "token-b"]
    for execution, actor in zip(executor.executions, submitted_actors, strict=True):
        assert isinstance(actor, AgentActor)
        context = execution[4]
        assert context.caller == AgentMcpExecutionCaller(principal=RequestPrincipal.from_source(actor))
        assert context.tool_call_id is not None
        assert context.approving_operator_id is None
        assert context.approval_policy_id == "policy:test"
    assert ledger.finish_actors == submitted_actors

    missing_auth_actor = actors["aa2"]
    tokens.tokens.pop(missing_auth_actor.operator_id)
    before = {record.tool_call_id for record in await service.list_tool_calls(actor=actors["oa"])}
    with pytest.raises(BackendAccountNotConnectedError):
        await service.submit_and_wait(req=_request(owner="missing-auth"), actor=missing_auth_actor)
    after = {record.tool_call_id for record in await service.list_tool_calls(actor=actors["oa"])}
    assert after == before


async def test_withdraw_retracts_the_agents_own_pending_call(
    actors: dict[str, RuntimeActor], publisher: _RecordingInvalidationPublisher, service: ToolCallApplicationService
) -> None:
    actor = actors["aa1"]
    pending = await service.submit_and_wait(req=_request(owner="stale"), actor=actor)
    assert pending.status is ToolCallStatus.PENDING_APPROVAL
    publisher.publications.clear()

    withdrawn = await service.withdraw(tool_call_id=pending.tool_call_id, reason="superseded", actor=actor)

    assert withdrawn.status is ToolCallStatus.WITHDRAWN
    assert withdrawn.withdrawal_reason == "superseded"
    assert withdrawn.denial_reason is None
    # Published to the owning operator, so their open approvals drawer drops the item live.
    assert publisher.publications == [(actor.operator_id, pending.tool_call_id)]
    assert await service.pending_approvals(actor=actors["oa"]) == []


async def test_queued_call_is_notified_once_and_retracted_by_whichever_exit_it_takes(
    actors: dict[str, RuntimeActor], notifier: _RecordingApprovalNotifier, service: ToolCallApplicationService
) -> None:
    """Each of the three exits retracts the notification the queue entry raised.

    A notification that outlives its call is the failure mode worth pinning: it keeps offering
    Approve/Deny for a decision that has already been made somewhere else.
    """
    agent, operator = actors["aa1"], actors["oa"]

    denied = await service.submit_and_wait(req=_request(owner="denied"), actor=agent)
    approved = await service.submit_and_wait(req=_request(owner="approved"), actor=agent)
    withdrawn = await service.submit_and_wait(req=_request(owner="withdrawn"), actor=agent)
    assert notifier.pending == [(agent.operator_id, call.tool_call_id) for call in (denied, approved, withdrawn)]
    assert notifier.resolved == []

    await service.decide(
        tool_call_id=denied.tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.DENY, reason="no"),
        actor=operator,
    )
    await service.decide(
        tool_call_id=approved.tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
        actor=operator,
    )
    await service.withdraw(tool_call_id=withdrawn.tool_call_id, reason="superseded", actor=agent)

    assert notifier.resolved == [
        (agent.operator_id, denied.tool_call_id, ToolCallStatus.DENIED),
        # Retracted at the decision, not at execution: the ask is settled the moment it is approved.
        (agent.operator_id, approved.tool_call_id, ToolCallStatus.RUNNING),
        (agent.operator_id, withdrawn.tool_call_id, ToolCallStatus.WITHDRAWN),
    ]


async def test_calls_that_never_queue_are_never_notified(
    actors: dict[str, RuntimeActor],
    notifier: _RecordingApprovalNotifier,
    service: ToolCallApplicationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auto-approved call never consumes operator attention, so it must not spend a push.

    Every push a browser shows draws down a budget it will eventually enforce, so a retraction
    for a notification that was never raised is not merely redundant — it is corrosive.
    """
    monkeypatch.setattr("haku.console.tool_call_service.auto_approve_tool_call", _always_approve)

    completed = await service.submit_and_wait(req=_request(owner="auto"), actor=actors["aa1"])

    assert completed.status is ToolCallStatus.OK
    assert notifier.pending == []
    assert notifier.resolved == []


async def test_a_failing_notifier_never_fails_the_transition(
    actors: dict[str, RuntimeActor], service: ToolCallApplicationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notification is best-effort in exactly the way invalidation is: the ledger row wins."""

    async def explode(**_: object) -> None:
        raise RuntimeError("push service unreachable")

    monkeypatch.setattr(service._approval_notifier, "tool_call_pending", explode)

    pending = await service.submit_and_wait(req=_request(owner="unreachable"), actor=actors["aa1"])

    assert pending.status is ToolCallStatus.PENDING_APPROVAL
    assert (await service.pending_approvals(actor=actors["oa"]))[0].tool_call_id == pending.tool_call_id


async def test_withdraw_rejects_calls_that_are_no_longer_pending(
    actors: dict[str, RuntimeActor], service: ToolCallApplicationService
) -> None:
    actor = actors["aa1"]
    pending = await service.submit_and_wait(req=_request(owner="raced"), actor=actor)
    await service.decide(
        tool_call_id=pending.tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
        actor=actors["oa"],
    )

    # The operator won the race; the agent is told the real status rather than silently succeeding.
    with pytest.raises(ToolCallStateConflictError, match="not pending approval; status=running"):
        await service.withdraw(tool_call_id=pending.tool_call_id, reason="too late", actor=actor)

    withdrawn = await service.submit_and_wait(req=_request(owner="twice"), actor=actor)
    await service.withdraw(tool_call_id=withdrawn.tool_call_id, reason="first", actor=actor)
    with pytest.raises(ToolCallStateConflictError, match="not pending approval; status=withdrawn"):
        await service.withdraw(tool_call_id=withdrawn.tool_call_id, reason="second", actor=actor)


async def test_withdraw_is_agent_only_and_scoped_to_the_submitting_agent(
    actors: dict[str, RuntimeActor], service: ToolCallApplicationService
) -> None:
    actor = actors["aa1"]
    pending = await service.submit_and_wait(req=_request(owner="mine"), actor=actor)

    # An operator's verb is `deny`; letting one write `withdrawn` would record the agent as having
    # retracted a request the operator in fact dismissed.
    with pytest.raises(AgentActorRequiredError, match="agent actor required"):
        await service.withdraw(tool_call_id=pending.tool_call_id, reason="not mine", actor=actors["oa"])

    # A sibling agent under the same operator, and an unrelated operator's agent, both see only a
    # not-found — no existence oracle for another agent's queue.
    for other in (actors["aa2"], actors["ab1"]):
        with pytest.raises(ToolCallNotFoundError, match="not found"):
            await service.withdraw(tool_call_id=pending.tool_call_id, reason="not mine", actor=other)

    assert (await service.get(pending.tool_call_id, actor=actor)).status is ToolCallStatus.PENDING_APPROVAL


async def test_withdraw_survives_credential_binding_rotation(
    actors: dict[str, RuntimeActor], service: ToolCallApplicationService, migrated_sessions
) -> None:
    """An Agent that reconnected can still clear its predecessor binding's ask out of the queue.

    Withdrawal is scoped to the Agent, not the exact binding — unlike `finish`/`authorize_execution`,
    which gate external execution. Otherwise an OAuth reconnect would strand the old binding's
    pending calls in the operator's queue forever.
    """
    original = actors["aa1"]
    assert isinstance(original, AgentActor)
    pending = await service.submit_and_wait(req=_request(owner="pre-rotation"), actor=original)

    successor_id = uuid4()
    now = datetime.datetime.now(datetime.UTC)
    async with migrated_sessions.begin() as session:
        # `uq_credential_bindings_one_active_per_agent` allows one ACTIVE binding per Agent, so a
        # reconnect retires the predecessor as it activates the successor — as production does.
        predecessor = await session.get_one(CredentialBinding, original.binding_id)
        predecessor.status = CredentialBindingStatus.REVOKED
        predecessor.ended_at = now
        predecessor.end_reason = "superseded by reconnect"
        predecessor.updated_at = now
        await session.flush()
        session.add_all(
            [
                CredentialBinding(
                    binding_id=successor_id,
                    agent_id=original.agent_id,
                    kind=CredentialKind.STATIC,
                    status=CredentialBindingStatus.ACTIVE,
                    generation=2,
                    supersedes_binding_id=original.binding_id,
                    created_at=now,
                    updated_at=now,
                    issued_at=now,
                    activated_at=now,
                    ended_at=None,
                    end_reason=None,
                ),
                StaticCredential(
                    binding_id=successor_id,
                    secret_reference=f"test-tool-call-service/{successor_id}",
                    credential_fingerprint=hashlib.sha256(successor_id.bytes).digest(),
                    created_at=now,
                ),
            ]
        )

    reconnected = AgentActor(agent_id=original.agent_id, operator_id=original.operator_id, binding_id=successor_id)
    withdrawn = await service.withdraw(
        tool_call_id=pending.tool_call_id, reason="reconnected; no longer needed", actor=reconnected
    )

    assert withdrawn.status is ToolCallStatus.WITHDRAWN


async def test_pending_wait_returns_when_the_agent_withdraws(
    actors: dict[str, RuntimeActor], service: ToolCallApplicationService
) -> None:
    actor = actors["aa1"]
    pending = await service.submit_and_wait(req=_request(owner="awaited"), actor=actor)

    # A second connection holds the promise open while this one retracts it; WITHDRAWN must count as
    # terminal or the waiter would block for the full wait_for_ms.
    waiting = asyncio.create_task(service._wait_terminal(pending.tool_call_id, actor, 60_000))
    await asyncio.sleep(0)
    await service.withdraw(tool_call_id=pending.tool_call_id, reason="changed my mind", actor=actor)

    settled = await asyncio.wait_for(waiting, timeout=5)
    assert settled.status is ToolCallStatus.WITHDRAWN


async def test_unknown_server_is_a_transport_independent_not_found(
    actors: dict[str, RuntimeActor], service: ToolCallApplicationService
) -> None:
    actor = actors["aa1"]

    with pytest.raises(McpServerNotFoundError, match="unknown MCP server: missing"):
        await service.submit_and_wait(
            req=_request(owner="missing").model_copy(update={"server_id": "missing"}), actor=actor
        )
    assert await service.list_tool_calls(actor=actor) == []


async def test_list_tool_calls_filters_by_auto_approved(
    actors: dict[str, RuntimeActor], service: ToolCallApplicationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = actors["aa1"]
    manual = await service.submit_and_wait(req=_request(owner="manual"), actor=actor)

    monkeypatch.setattr("haku.console.tool_call_service.auto_approve_tool_call", _always_approve)
    auto = await service.submit_and_wait(req=_request(owner="auto"), actor=actor)

    operator = actors["oa"]
    assert [r.tool_call_id for r in await service.list_tool_calls(actor=operator, auto_approved=False)] == [
        manual.tool_call_id
    ]
    assert [r.tool_call_id for r in await service.list_tool_calls(actor=operator, auto_approved=True)] == [
        auto.tool_call_id
    ]
    assert {r.tool_call_id for r in await service.list_tool_calls(actor=operator)} == {
        manual.tool_call_id,
        auto.tool_call_id,
    }


async def test_auto_execution_finishes_before_best_effort_invalidation_publication(
    *,
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    migrated_identity_store: PostgresOperatorIdentityStore,
    tmp_path: Path,
    actors: dict[str, RuntimeActor],
    ledger: _RecordingLedger,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
    monkeypatch: pytest.MonkeyPatch,
    notifier: _RecordingApprovalNotifier,
) -> None:
    actor = actors["aa1"]
    monkeypatch.setattr("haku.console.tool_call_service.auto_approve_tool_call", _always_approve)
    publisher = _RaisingInvalidationPublisher()
    service = _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        sessions=migrated_sessions,
        identity_store=migrated_identity_store,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
        notifier=notifier,
    )

    completed = await service.submit_and_wait(req=_request(owner="raising-publisher"), actor=actor)

    assert completed.status is ToolCallStatus.OK
    assert (await service.get(completed.tool_call_id, actor=actor)).status is ToolCallStatus.OK
    assert ledger.finish_actors == [actor]
    assert len(executor.executions) == 1
    assert publisher.publications == [(actor.operator_id, completed.tool_call_id)]


async def test_executor_cancellation_terminalizes_before_reraising(
    *,
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    migrated_identity_store: PostgresOperatorIdentityStore,
    tmp_path: Path,
    actors: dict[str, RuntimeActor],
    ledger: _RecordingLedger,
    publisher: _RecordingInvalidationPublisher,
    tokens: _OperatorTokens,
    monkeypatch: pytest.MonkeyPatch,
    notifier: _RecordingApprovalNotifier,
) -> None:
    actor = actors["aa1"]
    monkeypatch.setattr("haku.console.tool_call_service.auto_approve_tool_call", _always_approve)
    executor = _CancellingExecutor()
    service = _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        sessions=migrated_sessions,
        identity_store=migrated_identity_store,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
        notifier=notifier,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.submit_and_wait(req=_request(owner="cancelled"), actor=actor)

    [terminal] = await service.list_tool_calls(actor=actor)
    assert terminal.status is ToolCallStatus.ERROR
    assert terminal.error == "tool execution cancelled"
    assert ledger.finish_actors == [actor]
    assert publisher.publications == [(actor.operator_id, terminal.tool_call_id)]


async def test_decide_dispatches_execution_and_aclose_cancels_in_flight(
    *,
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    migrated_identity_store: PostgresOperatorIdentityStore,
    tmp_path: Path,
    actors: dict[str, RuntimeActor],
    ledger: _RecordingLedger,
    publisher: _RecordingInvalidationPublisher,
    tokens: _OperatorTokens,
    notifier: _RecordingApprovalNotifier,
) -> None:
    executor = _BlockingExecutor()
    service = _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        sessions=migrated_sessions,
        identity_store=migrated_identity_store,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
        notifier=notifier,
    )
    pending = await service.submit_and_wait(req=_request(owner="aa1"), actor=actors["aa1"])
    assert pending.status is ToolCallStatus.PENDING_APPROVAL

    # decide returns immediately with RUNNING while execution blocks in the background task.
    decided = await service.decide(
        tool_call_id=pending.tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
        actor=actors["oa"],
    )
    assert decided.status is ToolCallStatus.RUNNING
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    [execution] = executor.executions
    context = execution[4]
    agent = actors["aa1"]
    operator = actors["oa"]
    assert isinstance(agent, AgentActor)
    assert isinstance(operator, OperatorActor)
    assert context.caller == AgentMcpExecutionCaller(principal=RequestPrincipal.from_source(agent))
    assert context.tool_call_id == pending.tool_call_id
    assert context.approving_operator_id == operator.operator_id
    assert context.approval_policy_id is None

    # Shutdown cancels the in-flight execution, which terminalizes the row as cancelled.
    await service.aclose()
    assert service._execution_tasks == set()
    terminal = await service.get(pending.tool_call_id, actor=actors["oa"])
    assert terminal.status is ToolCallStatus.ERROR
    assert terminal.error == "tool execution cancelled"


async def test_finish_only_accepts_running_calls(actors: dict[str, RuntimeActor], ledger: _RecordingLedger) -> None:
    operator = actors["oa"]
    assert isinstance(operator, OperatorActor)
    server = McpServerEntry(
        id="operator-backend", backend=RemoteMcpBackend(url="https://backend.invalid/mcp", auth=NoCredential())
    )
    record = await ledger.submit(server=server, req=_request(owner="terminal"), actor=operator)

    with pytest.raises(ToolCallStateConflictError, match="not running"):
        await ledger.finish(record.tool_call_id, actor=operator, result={"ok": True}, error=None)

    running = await ledger.mark_running(record.tool_call_id, actor=operator)
    assert running.status is ToolCallStatus.RUNNING
    finished = await ledger.finish(record.tool_call_id, actor=operator, result={"ok": True}, error=None)
    assert finished.status is ToolCallStatus.OK
    with pytest.raises(ToolCallStateConflictError, match="not running"):
        await ledger.finish(record.tool_call_id, actor=operator, result={"again": True}, error=None)


async def test_execution_authorization_reloads_profile_changed_after_operator_approval(
    migrated_sessions, actors: dict[str, RuntimeActor], ledger: _RecordingLedger
) -> None:
    agent = actors["aa1"]
    operator = actors["oa"]
    assert isinstance(agent, AgentActor)
    assert isinstance(operator, OperatorActor)
    record = await ledger.submit(
        server=McpServerEntry(
            id="operator-backend", backend=RemoteMcpBackend(url="https://backend.invalid/mcp", auth=NoCredential())
        ),
        req=_request(owner="profile-changed-after-approval"),
        actor=agent,
    )
    await ledger.mark_running(record.tool_call_id, actor=operator)

    async with migrated_sessions.begin() as session:
        durable_agent = await session.get(Agent, agent.agent_id)
        assert durable_agent is not None
        durable_agent.access_profile_id = "profile-after-approval"

    authorization = await ledger.authorize_execution(record.tool_call_id, actor=operator)

    assert authorization.operator_id == operator.operator_id
    assert authorization.caller == replace(agent, access_profile_id="profile-after-approval")


async def test_binding_revoked_after_execution_authorization_does_not_strand_running_call(
    migrated_sessions, actors: dict[str, RuntimeActor], ledger: _RecordingLedger
) -> None:
    agent = actors["aa1"]
    assert isinstance(agent, AgentActor)
    record = await ledger.submit(
        server=McpServerEntry(
            id="operator-backend", backend=RemoteMcpBackend(url="https://backend.invalid/mcp", auth=NoCredential())
        ),
        req=_request(owner="revoked-during-execution"),
        actor=agent,
        auto_approval_policy_id="policy:test",
    )

    authorization = await ledger.authorize_execution(record.tool_call_id, actor=agent)
    assert authorization.operator_id == agent.operator_id
    assert authorization.caller == agent
    async with migrated_sessions.begin() as session:
        binding = await session.get(CredentialBinding, agent.binding_id)
        assert binding is not None
        now = datetime.datetime.now(datetime.UTC)
        binding.status = CredentialBindingStatus.REVOKED
        binding.updated_at = now
        binding.ended_at = now
        binding.end_reason = "test revocation"

    finished = await ledger.finish(record.tool_call_id, actor=agent, result={"ok": True}, error=None)
    assert finished.status is ToolCallStatus.OK


if __name__ == "__main__":
    pytest_bazel.main()
