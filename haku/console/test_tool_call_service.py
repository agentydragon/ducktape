"""Authorization matrix and lifecycle invariants for the Haku tool-call application service."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from haku.console.agents.models import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.authentik_operator_token import PostgresAuthentikOperatorTokenStore
from haku.console.conftest import console_sessions, console_settings, operator_identity_store, write_config
from haku.console.database_schema import Agent, AgentNameReservation, CredentialBinding, StaticCredential
from haku.console.mcp_approval import PostgresToolCallLedger
from haku.console.mcp_config import McpServerEntry, McpServerNotFoundError, NoBackendAuth
from haku.console.provider_connection import PostgresProviderConnectionStore
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor
from haku.console.tool_call_service import (
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
        self.executions: list[tuple[str, str, dict[str, Any], str | None]] = []

    async def execute(
        self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        self.executions.append((server.id, tool_name, arguments, auth_token))
        return {"content": [{"type": "text", "text": f"{server.id}:{tool_name}"}], "isError": False}


class _CancellingExecutor(_RecordingExecutor):
    async def execute(
        self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        self.executions.append((server.id, tool_name, arguments, auth_token))
        raise asyncio.CancelledError


class _BlockingExecutor(_RecordingExecutor):
    """Blocks in execute() until its task is cancelled, so a dispatched execution stays in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def execute(
        self, server: McpServerEntry, tool_name: str, arguments: dict[str, Any], auth_token: str | None
    ) -> dict[str, Any]:
        self.executions.append((server.id, tool_name, arguments, auth_token))
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
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        super().__init__(sessions)
        self.finish_actors: list[ToolCallActor] = []

    def finish(
        self, tool_call_id: str, *, actor: ToolCallActor, result: dict[str, Any] | None, error: str | None
    ) -> ToolCallRecord:
        self.finish_actors.append(actor)
        return super().finish(tool_call_id, actor=actor, result=result, error=error)


@pytest.fixture
def actors(migrated_db_url: str) -> dict[str, ToolCallActor]:
    identities = operator_identity_store(migrated_db_url)
    operator_ids = {
        "a": identities.resolve_configured_external_user_key("service-operator-a"),
        "b": identities.resolve_configured_external_user_key("service-operator-b"),
    }

    def agent(name: str, operator_id: UUID) -> AgentActor:
        agent_id = uuid4()
        reservation_id = uuid4()
        binding_id = uuid4()
        now = datetime.datetime.now(datetime.UTC)
        engine = create_engine(migrated_db_url)
        try:
            with Session(engine) as session, session.begin():
                # These mutually-referencing rows use deferred foreign keys; flushing the Agent first
                # mirrors the production lifecycle while keeping the fixture entirely in the ORM.
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
                session.flush()
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
        finally:
            engine.dispose()
        return AgentActor(agent_id=agent_id, operator_id=operator_id, binding_id=binding_id)

    actors: dict[str, ToolCallActor] = {
        "oa": OperatorActor(operator_id=operator_ids["a"]),
        "ob": OperatorActor(operator_id=operator_ids["b"]),
        "aa1": agent("agent-a1", operator_ids["a"]),
        "ab1": agent("agent-b1", operator_ids["b"]),
        "aa2": agent("agent-a2", operator_ids["a"]),
        "ab2": agent("agent-b2", operator_ids["b"]),
    }
    return actors


@pytest.fixture
def ledger(migrated_db_url: str) -> _RecordingLedger:
    return _RecordingLedger(console_sessions(migrated_db_url))


@pytest.fixture
def publisher() -> _RecordingInvalidationPublisher:
    return _RecordingInvalidationPublisher()


@pytest.fixture
def executor() -> _RecordingExecutor:
    return _RecordingExecutor()


@pytest.fixture
def tokens(actors: dict[str, ToolCallActor]) -> _OperatorTokens:
    return _OperatorTokens({actors["oa"].operator_id: "token-a", actors["ob"].operator_id: "token-b"})


def _service(
    *,
    database_url: str,
    tmp_path: Path,
    ledger: PostgresToolCallLedger,
    publisher: _RecordingInvalidationPublisher,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
) -> ToolCallApplicationService:
    config_file = write_config(
        tmp_path / "tool-call-service.yaml",
        {
            "mcp": {
                "servers": [
                    {
                        "id": "operator-backend",
                        "server_url": "https://backend.invalid/mcp",
                        "auth": {"kind": "remote_server_oauth"},
                    }
                ]
            }
        },
    )
    return ToolCallApplicationService(
        settings=console_settings(database_url, config_file=config_file),
        repository=ledger,
        invalidation_publisher=publisher,
        executor=executor,
        oauth_store=tokens,
        in_process_servers={},
        provider_store=PostgresProviderConnectionStore(
            console_sessions(database_url),
            operator_identity_store=operator_identity_store(database_url),
            provider_clients={},
        ),
        authentik_token_store=PostgresAuthentikOperatorTokenStore(
            console_sessions(database_url),
            operator_identity_store=operator_identity_store(database_url),
            client_id="test-client",
            client_secret="test-secret",
            issuer="https://auth.test/application/o/haku-console/",
        ),
    )


@pytest.fixture
def service(
    migrated_db_url: str,
    tmp_path: Path,
    ledger: _RecordingLedger,
    publisher: _RecordingInvalidationPublisher,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
) -> ToolCallApplicationService:
    return _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
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
    actors: dict[str, ToolCallActor],
    publisher: _RecordingInvalidationPublisher,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
    service: ToolCallApplicationService,
) -> None:
    operator = actors["oa"]
    assert isinstance(operator, OperatorActor)

    result = await service.execute_direct(req=_request(owner="browser"), actor=operator)

    assert result["content"][0]["text"] == "operator-backend:mutate"
    assert executor.executions == [("operator-backend", "mutate", {"owner": "browser"}, "token-a")]
    assert tokens.lookups == [operator.operator_id]
    assert service.list_tool_calls(actor=operator) == []
    assert publisher.publications == []

    with pytest.raises(OperatorActorRequiredError, match="operator actor required"):
        await service.execute_direct(req=_request(owner="agent"), actor=actors["aa1"])


async def test_two_operator_two_agent_authorization_matrix(
    actors: dict[str, ToolCallActor],
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
        service.list_tool_calls(actor=invalid_actor)
    with pytest.raises(TypeError, match="unsupported tool-call actor"):
        ledger.submit(
            server=McpServerEntry(id="operator-backend", auth=NoBackendAuth()),
            req=_request(owner="lookalike"),
            actor=invalid_actor,
        )
    with pytest.raises(TypeError, match="unsupported tool-call actor"):
        ledger.get(records["oa"].tool_call_id, actor=invalid_actor)
    agent_as_operator: Any = actors["aa1"]
    with pytest.raises(TypeError, match="operator actor required"):
        ledger.mark_running(records["aa1"].tool_call_id, actor=agent_as_operator)

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
                assert service.get(record.tool_call_id, actor=reader) == record
            else:
                with pytest.raises(ToolCallNotFoundError, match="tool call not found"):
                    service.get(record.tool_call_id, actor=reader)
        listed = service.list_tool_calls(actor=reader)
        assert {record.tool_call_id for record in listed} == {records[owner].tool_call_id for owner in visible}

    for operator_name, owned_names in (("oa", {"oa", "aa1", "aa2"}), ("ob", {"ob", "ab1", "ab2"})):
        pending = service.pending_approvals(actor=actors[operator_name])
        assert {record.tool_call_id for record in pending} == {records[owner].tool_call_id for owner in owned_names}

    for agent_name in ("aa1", "aa2", "ab1", "ab2"):
        with pytest.raises(OperatorActorRequiredError):
            service.pending_approvals(actor=actors[agent_name])
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
    assert [execution[3] for execution in executor.executions] == ["token-a", "token-b"]
    assert [
        service.get(records["aa1"].tool_call_id, actor=actors["oa"]).status,
        service.get(records["ab1"].tool_call_id, actor=actors["ob"]).status,
    ] == [ToolCallStatus.OK, ToolCallStatus.OK]


async def test_pending_wait_uses_actor_scoped_event_invalidation(
    actors: dict[str, ToolCallActor], publisher: _RecordingInvalidationPublisher, service: ToolCallApplicationService
) -> None:
    agent = actors["aa1"]
    operator = actors["oa"]

    waiting = asyncio.create_task(service.submit_and_wait(req=_request(owner="wait", wait_for_ms=1000), actor=agent))
    await asyncio.wait_for(publisher.subscribed.wait(), timeout=1)
    [pending] = service.list_tool_calls(actor=agent)
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
    migrated_db_url: str,
    tmp_path: Path,
    actors: dict[str, ToolCallActor],
    ledger: _RecordingLedger,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
) -> None:
    agent = actors["aa1"]
    operator = actors["oa"]
    publisher = _TransitionBeforeYieldInvalidationPublisher()
    service = _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
    )

    # A deny is a synchronous terminal transition (unlike approve, which now dispatches execution to a
    # background task), so it lands in the durable row within the subscribe window — exactly the
    # race this test exercises: _wait_terminal must re-read after subscribing and observe it.
    async def transition_after_subscription_registration() -> None:
        [pending] = service.list_tool_calls(actor=agent)
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
    actors: dict[str, ToolCallActor],
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
    assert ledger.finish_actors == submitted_actors

    missing_auth_actor = actors["aa2"]
    tokens.tokens.pop(missing_auth_actor.operator_id)
    before = {record.tool_call_id for record in service.list_tool_calls(actor=actors["oa"])}
    with pytest.raises(BackendAccountNotConnectedError):
        await service.submit_and_wait(req=_request(owner="missing-auth"), actor=missing_auth_actor)
    after = {record.tool_call_id for record in service.list_tool_calls(actor=actors["oa"])}
    assert after == before


async def test_unknown_server_is_a_transport_independent_not_found(
    actors: dict[str, ToolCallActor], service: ToolCallApplicationService
) -> None:
    actor = actors["aa1"]

    with pytest.raises(McpServerNotFoundError, match="unknown MCP server: missing"):
        await service.submit_and_wait(
            req=_request(owner="missing").model_copy(update={"server_id": "missing"}), actor=actor
        )
    assert service.list_tool_calls(actor=actor) == []


async def test_auto_execution_finishes_before_best_effort_invalidation_publication(
    migrated_db_url: str,
    tmp_path: Path,
    actors: dict[str, ToolCallActor],
    ledger: _RecordingLedger,
    executor: _RecordingExecutor,
    tokens: _OperatorTokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = actors["aa1"]
    monkeypatch.setattr("haku.console.tool_call_service.auto_approve_tool_call", _always_approve)
    publisher = _RaisingInvalidationPublisher()
    service = _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
    )

    completed = await service.submit_and_wait(req=_request(owner="raising-publisher"), actor=actor)

    assert completed.status is ToolCallStatus.OK
    assert service.get(completed.tool_call_id, actor=actor).status is ToolCallStatus.OK
    assert ledger.finish_actors == [actor]
    assert len(executor.executions) == 1
    assert publisher.publications == [(actor.operator_id, completed.tool_call_id)]


async def test_executor_cancellation_terminalizes_before_reraising(
    migrated_db_url: str,
    tmp_path: Path,
    actors: dict[str, ToolCallActor],
    ledger: _RecordingLedger,
    publisher: _RecordingInvalidationPublisher,
    tokens: _OperatorTokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = actors["aa1"]
    monkeypatch.setattr("haku.console.tool_call_service.auto_approve_tool_call", _always_approve)
    executor = _CancellingExecutor()
    service = _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.submit_and_wait(req=_request(owner="cancelled"), actor=actor)

    [terminal] = service.list_tool_calls(actor=actor)
    assert terminal.status is ToolCallStatus.ERROR
    assert terminal.error == "tool execution cancelled"
    assert ledger.finish_actors == [actor]
    assert publisher.publications == [(actor.operator_id, terminal.tool_call_id)]


async def test_decide_dispatches_execution_and_aclose_cancels_in_flight(
    migrated_db_url: str,
    tmp_path: Path,
    actors: dict[str, ToolCallActor],
    ledger: _RecordingLedger,
    publisher: _RecordingInvalidationPublisher,
    tokens: _OperatorTokens,
) -> None:
    executor = _BlockingExecutor()
    service = _service(
        database_url=migrated_db_url,
        tmp_path=tmp_path,
        ledger=ledger,
        publisher=publisher,
        executor=executor,
        tokens=tokens,
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

    # Shutdown cancels the in-flight execution, which terminalizes the row as cancelled.
    await service.aclose()
    assert service._execution_tasks == set()
    terminal = service.get(pending.tool_call_id, actor=actors["oa"])
    assert terminal.status is ToolCallStatus.ERROR
    assert terminal.error == "tool execution cancelled"


async def test_finish_only_accepts_running_calls(actors: dict[str, ToolCallActor], ledger: _RecordingLedger) -> None:
    operator = actors["oa"]
    assert isinstance(operator, OperatorActor)
    server = McpServerEntry(id="operator-backend", server_url="https://backend.invalid/mcp", auth=NoBackendAuth())
    record = ledger.submit(server=server, req=_request(owner="terminal"), actor=operator)

    with pytest.raises(ToolCallStateConflictError, match="not running"):
        ledger.finish(record.tool_call_id, actor=operator, result={"ok": True}, error=None)

    running = ledger.mark_running(record.tool_call_id, actor=operator)
    assert running.status is ToolCallStatus.RUNNING
    finished = ledger.finish(record.tool_call_id, actor=operator, result={"ok": True}, error=None)
    assert finished.status is ToolCallStatus.OK
    with pytest.raises(ToolCallStateConflictError, match="not running"):
        ledger.finish(record.tool_call_id, actor=operator, result={"again": True}, error=None)


async def test_binding_revoked_after_execution_authorization_does_not_strand_running_call(
    migrated_db_url: str, actors: dict[str, ToolCallActor], ledger: _RecordingLedger
) -> None:
    agent = actors["aa1"]
    assert isinstance(agent, AgentActor)
    record = ledger.submit(
        server=McpServerEntry(id="operator-backend", auth=NoBackendAuth()),
        req=_request(owner="revoked-during-execution"),
        actor=agent,
        auto_approval_policy_id="policy:test",
    )

    assert ledger.authorize_execution(record.tool_call_id, actor=agent) == agent.operator_id
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as session, session.begin():
            binding = session.get(CredentialBinding, agent.binding_id)
            assert binding is not None
            now = datetime.datetime.now(datetime.UTC)
            binding.status = CredentialBindingStatus.REVOKED
            binding.updated_at = now
            binding.ended_at = now
            binding.end_reason = "test revocation"
    finally:
        engine.dispose()

    finished = ledger.finish(record.tool_call_id, actor=agent, result={"ok": True}, error=None)
    assert finished.status is ToolCallStatus.OK


if __name__ == "__main__":
    pytest_bazel.main()
