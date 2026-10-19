"""Authorization matrix and lifecycle invariants for the Haku tool-call application service."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.conftest import console_settings, write_config
from haku.console.database_schema import Agent, AgentNameReservation, CredentialBinding, StaticCredential
from haku.console.identity.agent import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.identity.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.mcp.approval import PostgresToolCallLedger
from haku.console.mcp.execution import AgentMcpExecutionCaller, McpExecutionContext
from haku.console.mcp.principal import RequestPrincipal
from haku.console.mcp.testing import join_executions, seed_pending_tool_call
from haku.console.mcp.tool_call_service import (
    AgentActorRequiredError,
    OperatorActorRequiredError,
    ToolCallApplicationService,
    ToolCallNotFoundError,
    ToolCallStateConflictError,
)
from haku.console.mcp_config import InProcessBackend, InProcessServers, McpServerEntry, NoCredential
from haku.console.tool_call_actor import AgentActor, OperatorActor, RuntimeActor
from haku.console.tool_calls import (
    ApprovalDecision,
    ApprovalDecisionRequest,
    SubmitToolCallRequest,
    ToolCallRecord,
    ToolCallStatus,
)


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
def executor() -> _RecordingExecutor:
    return _RecordingExecutor()


def _service(
    *,
    database_url: str,
    tmp_path: Path,
    ledger: PostgresToolCallLedger,
    executor: _RecordingExecutor,
    servers: list[dict[str, Any]] | None = None,
    in_process_servers: InProcessServers | None = None,
) -> ToolCallApplicationService:
    configured_servers = servers or [
        {"id": "operator-backend", "backend": {"kind": "in_process", "credential": {"kind": "none"}}}
    ]
    config_file = write_config(
        tmp_path / "tool-call-service.yaml",
        {
            "auto_approval_policies": [{"id": "manual", "type": "never"}],
            "access_profiles": [{"id": "manual", "auto_approval_policy": "manual"}],
            "default_access_profile_id": "manual",
            "mcp": {"servers": {server["id"].replace("-", "_"): server for server in configured_servers}},
        },
    )
    return ToolCallApplicationService(
        settings=console_settings(database_url, config_file=config_file),
        repository=ledger,
        executor=executor,
        in_process_servers=in_process_servers or {},
    )


@pytest.fixture
def service(
    *, migrated_db_url: str, tmp_path: Path, ledger: _RecordingLedger, executor: _RecordingExecutor
) -> ToolCallApplicationService:
    return _service(database_url=migrated_db_url, tmp_path=tmp_path, ledger=ledger, executor=executor)


def _request(*, owner: str) -> SubmitToolCallRequest:
    return SubmitToolCallRequest(
        server_id="operator-backend",
        tool_name="mutate",
        arguments={"owner": owner},
        rationale=f"exercise {owner}",
        wait_for_ms=0,
    )


async def _submit_pending(ledger: _RecordingLedger, *, actor: RuntimeActor, owner: str) -> ToolCallRecord:
    return await seed_pending_tool_call(
        server=McpServerEntry(id="operator-backend", backend=InProcessBackend(credential=NoCredential())),
        ledger=ledger,
        req=_request(owner=owner),
        actor=actor,
    )


async def test_two_operator_two_agent_authorization_matrix(
    *,
    actors: dict[str, RuntimeActor],
    executor: _RecordingExecutor,
    ledger: _RecordingLedger,
    service: ToolCallApplicationService,
) -> None:
    """Every service read and lifecycle transition is scoped from the authenticated actor."""
    records = {name: await _submit_pending(ledger, actor=actor, owner=name) for name, actor in actors.items()}
    assert all(record.status is ToolCallStatus.PENDING_APPROVAL for record in records.values())

    invalid_actor: Any = SimpleNamespace(operator_id=actors["oa"].operator_id)
    with pytest.raises(TypeError, match="unsupported tool-call actor"):
        await service.list_tool_calls(actor=invalid_actor)
    with pytest.raises(TypeError, match="unsupported tool-call actor"):
        await seed_pending_tool_call(
            ledger=ledger,
            server=McpServerEntry(id="operator-backend", backend=InProcessBackend(credential=NoCredential())),
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
    baseline = list(executor.executions)
    with pytest.raises(ToolCallNotFoundError, match="tool call not found"):
        await service.decide(
            tool_call_id=records["ab2"].tool_call_id,
            decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
            actor=actors["oa"],
        )
    assert executor.executions == baseline

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
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.DENY, decision_note="no"),
        actor=actors["oa"],
    )
    # decide() now returns the RUNNING record and runs the tool in the background; deny stays terminal.
    assert [approved_a.status, approved_b.status, denied.status] == [
        ToolCallStatus.RUNNING,
        ToolCallStatus.RUNNING,
        ToolCallStatus.DENIED,
    ]
    await join_executions(service)
    # Every registered server declares `NoCredential`, so backend auth resolves to None regardless
    # of which Operator approved.
    assert [execution[3] for execution in executor.executions] == [None, None]
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


async def test_withdraw_rejects_calls_that_are_no_longer_pending(
    actors: dict[str, RuntimeActor], ledger: _RecordingLedger, service: ToolCallApplicationService
) -> None:
    actor = actors["aa1"]
    pending = await _submit_pending(ledger, actor=actor, owner="raced")
    await service.decide(
        tool_call_id=pending.tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
        actor=actors["oa"],
    )

    # The operator won the race; the agent is told the real status rather than silently succeeding.
    with pytest.raises(ToolCallStateConflictError, match=ToolCallStatus.RUNNING):
        await service.withdraw(tool_call_id=pending.tool_call_id, reason="too late", actor=actor)

    withdrawn = await _submit_pending(ledger, actor=actor, owner="twice")
    await service.withdraw(tool_call_id=withdrawn.tool_call_id, reason="first", actor=actor)
    with pytest.raises(ToolCallStateConflictError, match=ToolCallStatus.WITHDRAWN):
        await service.withdraw(tool_call_id=withdrawn.tool_call_id, reason="second", actor=actor)


async def test_withdraw_is_agent_only_and_scoped_to_the_submitting_agent(
    actors: dict[str, RuntimeActor], ledger: _RecordingLedger, service: ToolCallApplicationService
) -> None:
    actor = actors["aa1"]
    pending = await _submit_pending(ledger, actor=actor, owner="mine")

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
    actors: dict[str, RuntimeActor], ledger: _RecordingLedger, service: ToolCallApplicationService, migrated_sessions
) -> None:
    """An Agent that reconnected can still clear its predecessor binding's ask out of the queue.

    Withdrawal is scoped to the Agent, not the exact binding — unlike `finish`/`authorize_execution`,
    which gate external execution. Otherwise an OAuth reconnect would strand the old binding's
    pending calls in the operator's queue forever.
    """
    original = actors["aa1"]
    assert isinstance(original, AgentActor)
    pending = await _submit_pending(ledger, actor=original, owner="pre-rotation")

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


async def test_executor_cancellation_terminalizes_before_reraising(
    *, migrated_db_url: str, tmp_path: Path, actors: dict[str, RuntimeActor], ledger: _RecordingLedger
) -> None:
    actor = actors["aa1"]
    approver = actors["oa"]
    executor = _CancellingExecutor()
    service = _service(database_url=migrated_db_url, tmp_path=tmp_path, ledger=ledger, executor=executor)
    pending = await _submit_pending(ledger, actor=actor, owner="cancelled")
    await service.decide(
        tool_call_id=pending.tool_call_id,
        decision=ApprovalDecisionRequest(decision=ApprovalDecision.APPROVE),
        actor=approver,
    )
    await join_executions(service)

    [terminal] = await service.list_tool_calls(actor=actor)
    assert terminal.status is ToolCallStatus.ERROR
    assert ledger.finish_actors == [approver]


async def test_decide_dispatches_execution_and_aclose_cancels_in_flight(
    *, migrated_db_url: str, tmp_path: Path, actors: dict[str, RuntimeActor], ledger: _RecordingLedger
) -> None:
    executor = _BlockingExecutor()
    service = _service(database_url=migrated_db_url, tmp_path=tmp_path, ledger=ledger, executor=executor)
    pending = await _submit_pending(ledger, actor=actors["aa1"], owner="aa1")
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
    assert context.caller == AgentMcpExecutionCaller(
        principal=RequestPrincipal.from_source(agent), operator_id=agent.operator_id
    )
    assert context.tool_call_id == pending.tool_call_id
    assert context.approving_operator_id == operator.operator_id
    assert context.approval_policy_id is None

    # Shutdown cancels the in-flight execution, which terminalizes the row as cancelled.
    await service.aclose()
    assert service._execution_tasks == set()
    terminal = await service.get(pending.tool_call_id, actor=actors["oa"])
    assert terminal.status is ToolCallStatus.ERROR


async def test_finish_only_accepts_running_calls(actors: dict[str, RuntimeActor], ledger: _RecordingLedger) -> None:
    operator = actors["oa"]
    assert isinstance(operator, OperatorActor)
    server = McpServerEntry(id="operator-backend", backend=InProcessBackend(credential=NoCredential()))
    record = await seed_pending_tool_call(ledger=ledger, server=server, req=_request(owner="terminal"), actor=operator)

    with pytest.raises(ToolCallStateConflictError):
        await ledger.finish(record.tool_call_id, actor=operator, result={"ok": True}, error=None)

    running = await ledger.mark_running(record.tool_call_id, actor=operator)
    assert running.status is ToolCallStatus.RUNNING
    finished = await ledger.finish(record.tool_call_id, actor=operator, result={"ok": True}, error=None)
    assert finished.status is ToolCallStatus.OK
    with pytest.raises(ToolCallStateConflictError):
        await ledger.finish(record.tool_call_id, actor=operator, result={"again": True}, error=None)


async def test_execution_authorization_reloads_profile_changed_after_operator_approval(
    migrated_sessions, actors: dict[str, RuntimeActor], ledger: _RecordingLedger
) -> None:
    agent = actors["aa1"]
    operator = actors["oa"]
    assert isinstance(agent, AgentActor)
    assert isinstance(operator, OperatorActor)
    record = await seed_pending_tool_call(
        ledger=ledger,
        server=McpServerEntry(id="operator-backend", backend=InProcessBackend(credential=NoCredential())),
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
    record = await seed_pending_tool_call(
        ledger=ledger,
        server=McpServerEntry(id="operator-backend", backend=InProcessBackend(credential=NoCredential())),
        req=_request(owner="revoked-during-execution"),
        actor=agent,
    )
    operator = actors["oa"]
    assert isinstance(operator, OperatorActor)
    await ledger.mark_running(record.tool_call_id, actor=operator)

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
