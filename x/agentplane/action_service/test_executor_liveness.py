"""Executor liveness: heartbeats, bounded lease expiry, and authenticated reconciliation.

State-transition diagram and reason-code catalog: docs/executor_liveness.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.catalog import ActionCatalog, ActionIdentity
from x.agentplane.action_service.db import ActionConflictError, ActionStore, ExecutionRow, make_sessionmaker
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionState,
    DecisionInput,
    ExecutionClaim,
    ExecutionLease,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    Principal,
    PrincipalRole,
    ReconciliationSource,
    UnknownOutcomeReason,
    Verdict,
)
from x.agentplane.action_service.service import ActionService, ServiceDrainingError

CALLER = Principal(issuer="test-workload", subject="sandbox-a", role=PrincipalRole.CALLER)
OPERATOR = Principal(issuer="test-bff", subject="operator", role=PrincipalRole.OPERATOR)
ACTION_ID = ActionIdentity(group="agentplane", name="echo")
ALREADY_EXPIRED = timedelta(seconds=-1)


class SlowSilentExecutor:
    """Never heartbeats; sleeps past its own lease so the sweep must catch it mid-flight."""

    def __init__(self, sleep_seconds: float) -> None:
        self.requests: list[ExecutionRequest] = []
        self._sleep_seconds = sleep_seconds

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        self.requests.append(request)
        await asyncio.sleep(self._sleep_seconds)
        return ExecutionResult(state=ExecutionState.SUCCEEDED, result={"echo": request.arguments})


async def _allowed_execution(store: ActionStore, *, idempotency_key: str) -> Any:
    view, _ = await store.submit(
        ActionRequestInput(idempotency_key=idempotency_key, action=ACTION_ID, arguments={}), CALLER
    )
    await store.decide(
        view.id,
        DecisionInput(verdict=Verdict.ALLOW, expected_version=view.version, idempotency_key=f"{idempotency_key}-allow"),
        OPERATOR,
        provider=ActionService.HUMAN_PROVIDER,
    )
    return view.id


async def _poll_state(store: ActionStore, request_id: Any, *, want: ActionState) -> None:
    for _ in range(200):
        view = await store.get(request_id, CALLER)
        if view.state is want:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never reached {want}")


async def test_worker_dies_before_start_becomes_unknown_without_ever_calling_the_backend(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    request_id = await _allowed_execution(store, idempotency_key="dies-before-start")

    claim = await store.claim_execution(request_id, executor_id="doomed-executor", lease_duration=ALREADY_EXPIRED)
    assert claim is not None
    # The worker crashes before ever calling mark_running; the backend was never touched.

    expired = await store.expire_stale_leases(executor_health_timeout=timedelta(seconds=30))
    assert expired == [request_id]
    view = await store.get(request_id, CALLER)
    assert view.state is ActionState.EXECUTION_UNKNOWN
    assert view.execution is not None
    assert view.execution.error == {
        "kind": UnknownOutcomeReason.EXECUTOR_LOST,
        "message": "dispatch outcome unknown; not replayed",
    }


async def test_worker_dies_after_start_becomes_unknown_and_is_not_retried(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    request_id = await _allowed_execution(store, idempotency_key="dies-after-start")

    claim = await store.claim_execution(request_id, executor_id="doomed-executor", lease_duration=ALREADY_EXPIRED)
    assert claim is not None
    await store.mark_running(request_id)
    await store.record_executor_heartbeat("doomed-executor")  # the executor itself is otherwise healthy

    expired = await store.expire_stale_leases(executor_health_timeout=timedelta(seconds=30))
    assert expired == [request_id]
    view = await store.get(request_id, CALLER)
    assert view.state is ActionState.EXECUTION_UNKNOWN
    assert view.execution is not None
    assert view.execution.error == {
        "kind": UnknownOutcomeReason.LEASE_EXPIRED,
        "message": "dispatch outcome unknown; not replayed",
    }

    # Never called a second time: no dispatch method exists that would re-run a claimed request.
    assert await store.pending_dispatches() == []


async def test_heartbeat_renewal_keeps_the_lease_alive_past_its_original_duration(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    request_id = await _allowed_execution(store, idempotency_key="renewed-lease")

    claim = await store.claim_execution(
        request_id, executor_id="healthy-executor", lease_duration=timedelta(seconds=0.05)
    )
    assert claim is not None
    await store.mark_running(request_id)
    await asyncio.sleep(0.06)  # past the original lease window
    renewed = await store.heartbeat_execution(
        request_id, claim.executor_id, claim.lease_token, lease_duration=timedelta(seconds=30)
    )
    assert renewed is True

    expired = await store.expire_stale_leases(executor_health_timeout=timedelta(seconds=30))
    assert expired == []
    assert (await store.get(request_id, CALLER)).state is ActionState.RUNNING


async def test_heartbeat_from_the_wrong_lease_token_is_rejected(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    request_id = await _allowed_execution(store, idempotency_key="wrong-token-heartbeat")
    claim = await store.claim_execution(request_id, executor_id="executor-a", lease_duration=timedelta(seconds=30))
    assert claim is not None

    accepted = await store.heartbeat_execution(request_id, "executor-a", uuid4(), lease_duration=timedelta(seconds=30))
    assert accepted is False


async def test_late_completion_from_the_original_executor_reconciles_the_unknown_execution(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    request_id = await _allowed_execution(store, idempotency_key="late-completion")

    claim = await store.claim_execution(request_id, executor_id="slow-executor", lease_duration=ALREADY_EXPIRED)
    assert claim is not None
    await store.mark_running(request_id)
    assert await store.expire_stale_leases(executor_health_timeout=timedelta(seconds=30)) == [request_id]
    assert (await store.get(request_id, CALLER)).state is ActionState.EXECUTION_UNKNOWN

    # The original attempt was never abandoned by the real backend; it finally reports success,
    # authenticated by the same lease it was handed at claim time.
    await store.finish_execution(
        request_id,
        claim.executor_id,
        claim.lease_token,
        ExecutionResult(state=ExecutionState.SUCCEEDED, result={"echo": {}}),
    )
    view = await store.get(request_id, CALLER)
    assert view.state is ActionState.SUCCEEDED
    assert view.execution is not None
    assert view.execution.result == {"echo": {}}
    assert view.execution.reconciled_at is not None

    async with make_sessionmaker(engine)() as session:
        row = await session.get(ExecutionRow, view.execution.id)
        assert row is not None
        assert row.reconciliation_source == ReconciliationSource.LATE_COMPLETION
        assert row.reconciled_by == "slow-executor"


async def test_late_completion_with_a_different_lease_token_is_rejected(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    request_id = await _allowed_execution(store, idempotency_key="late-completion-wrong-token")
    claim = await store.claim_execution(request_id, executor_id="slow-executor", lease_duration=ALREADY_EXPIRED)
    assert claim is not None
    await store.mark_running(request_id)
    await store.expire_stale_leases(executor_health_timeout=timedelta(seconds=30))

    try:
        await store.finish_execution(
            request_id, "slow-executor", uuid4(), ExecutionResult(state=ExecutionState.SUCCEEDED, result={})
        )
        raise AssertionError("expected ActionConflictError")
    except ActionConflictError:
        pass
    assert (await store.get(request_id, CALLER)).state is ActionState.EXECUTION_UNKNOWN


async def test_authoritative_reconciliation_never_preempts_a_still_running_execution(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    request_id = await _allowed_execution(store, idempotency_key="authoritative-reconcile-live")
    claim = await store.claim_execution(request_id, executor_id="worker-1", lease_duration=timedelta(seconds=30))
    assert claim is not None
    await store.mark_running(request_id)

    try:
        await store.reconcile_from_authority(
            request_id, ExecutionResult(state=ExecutionState.FAILED, error={"kind": "test"}), authority="status-api"
        )
        raise AssertionError("expected ActionConflictError")
    except ActionConflictError:
        pass
    assert (await store.get(request_id, CALLER)).state is ActionState.RUNNING


async def test_authoritative_reconciliation_applies_once_the_execution_is_unknown(engine: AsyncEngine) -> None:
    store = ActionStore(make_sessionmaker(engine))
    request_id = await _allowed_execution(store, idempotency_key="authoritative-reconcile-unknown")
    claim = await store.claim_execution(request_id, executor_id="worker-1", lease_duration=ALREADY_EXPIRED)
    assert claim is not None
    await store.mark_running(request_id)
    assert await store.expire_stale_leases(executor_health_timeout=timedelta(seconds=30)) == [request_id]

    await store.reconcile_from_authority(
        request_id,
        ExecutionResult(state=ExecutionState.FAILED, error={"kind": "provider_reported_failure"}),
        authority="status-api",
    )
    view = await store.get(request_id, CALLER)
    assert view.state is ActionState.FAILED
    assert view.execution is not None
    assert view.execution.reconciled_at is not None


async def test_action_service_restarts_and_worker_liveness_never_double_dispatches(
    engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    """`ActionService restarts during running work` and `worker dies after start` collapse into
    one observable fact at the store: a lease stopped being renewed. Proves it end to end through
    the real coordinator, including the backend's own late, authenticated completion.
    """
    sessions = make_sessionmaker(engine)
    store = ActionStore(sessions)
    executor = SlowSilentExecutor(sleep_seconds=0.2)
    service = ActionService(
        store,
        echo_catalog,
        {"agentplane": executor},
        lease_duration=timedelta(seconds=0.05),
        lease_sweep_interval=timedelta(seconds=0.02),
        executor_heartbeat_interval=timedelta(seconds=0.02),
        executor_health_timeout=timedelta(seconds=30),
    )
    await service.start()
    try:
        pending = await service.submit(
            ActionRequestInput(idempotency_key="no-double-dispatch", action=ACTION_ID, arguments={}), CALLER
        )
        await service.decide(
            pending.id,
            DecisionInput(
                verdict=Verdict.ALLOW, expected_version=pending.version, idempotency_key="allow-no-double-dispatch"
            ),
            OPERATOR,
        )
        request_id = pending.id

        await _poll_state(store, request_id, want=ActionState.EXECUTION_UNKNOWN)
        assert len(executor.requests) == 1

        # The still-running dispatch task eventually returns and reconciles instead of erroring.
        await _poll_state(store, request_id, want=ActionState.SUCCEEDED)
        assert len(executor.requests) == 1
    finally:
        await service.close()


class GatedExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return ExecutionResult(state=ExecutionState.SUCCEEDED, result="finished")


async def test_drain_fences_queued_dispatch_and_another_replica_claims_it(
    engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    executor = GatedExecutor()
    executor.release.set()
    request_id = await _allowed_execution(store, idempotency_key="queued-drain")
    first = ActionService(store, echo_catalog, {"agentplane": executor})
    second = ActionService(store, echo_catalog, {"agentplane": executor})
    try:
        await first.start()
        # start() schedules the durable row, but the queued task has not entered the claim RPC.
        first.begin_drain()
        await first.close()
        assert await store.pending_dispatches() == [request_id]
        assert executor.calls == 0
        with pytest.raises(ServiceDrainingError):
            await first.submit(ActionRequestInput(idempotency_key="late", action=ACTION_ID, arguments={}), CALLER)
        await second.start()
        await _poll_state(store, request_id, want=ActionState.SUCCEEDED)
        assert executor.calls == 1
    finally:
        await first.close()
        await second.close()


async def test_drain_includes_a_claim_whose_database_reply_is_still_in_flight(
    engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    executor = GatedExecutor()
    claimed, return_claim = asyncio.Event(), asyncio.Event()
    claim_execution = store.claim_execution

    async def delayed_claim(
        request_id: UUID,
        *,
        executor_id: str,
        lease_duration: timedelta,
        can_dispatch: Callable[[ActionIdentity], bool] | None = None,
    ) -> ExecutionClaim | None:
        result = await claim_execution(
            request_id, executor_id=executor_id, lease_duration=lease_duration, can_dispatch=can_dispatch
        )
        assert result is not None
        claimed.set()
        await return_claim.wait()
        return result

    request_id = await _allowed_execution(store, idempotency_key="claim-race")
    service = ActionService(store, echo_catalog, {"agentplane": executor})
    try:
        with patch.object(store, "claim_execution", delayed_claim):
            await service.start()
            async with asyncio.timeout(5):
                await claimed.wait()
                service.begin_drain()
                closing = asyncio.create_task(service.close())
                return_claim.set()
                await executor.started.wait()
                assert not closing.done()
                executor.release.set()
                await closing
        assert (await store.get(request_id, CALLER)).state is ActionState.SUCCEEDED
        assert executor.calls == 1
    finally:
        return_claim.set()
        executor.release.set()
        await service.close()


async def test_drain_waits_for_terminal_persistence_and_renews_its_lease(
    engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    executor = GatedExecutor()
    persisting, release_persistence, renewed = (asyncio.Event() for _ in range(3))
    finish = store.finish_execution
    heartbeat = store.heartbeat_execution
    service = ActionService(store, echo_catalog, {"agentplane": executor}, lease_duration=timedelta(milliseconds=300))
    request_id = await _allowed_execution(store, idempotency_key="terminal-persistence")

    async def delayed_finish(request_id: UUID, executor_id: str, lease_token: UUID, result: ExecutionResult) -> None:
        persisting.set()
        await release_persistence.wait()
        await finish(request_id, executor_id, lease_token, result)

    async def renew(request_id: UUID, executor_id: str, lease_token: UUID, *, lease_duration: timedelta) -> bool:
        accepted = await heartbeat(request_id, executor_id, lease_token, lease_duration=lease_duration)
        if persisting.is_set() and accepted:
            renewed.set()
        return accepted

    try:
        with patch.object(store, "finish_execution", delayed_finish), patch.object(store, "heartbeat_execution", renew):
            await service.start()
            async with asyncio.timeout(5):
                await executor.started.wait()
                service.begin_drain()
                executor.release.set()
                await persisting.wait()
                await renewed.wait()
                assert (await store.get(request_id, CALLER)).state is ActionState.RUNNING
                release_persistence.set()
                await service.close()
        assert (await store.get(request_id, CALLER)).state is ActionState.SUCCEEDED
        assert not any(task.get_name() == "action-completion-renewal" for task in asyncio.all_tasks())
    finally:
        executor.release.set()
        release_persistence.set()
        await service.close()


async def test_forced_drain_persists_unknown_and_never_replays(
    engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    executor = GatedExecutor()
    request_id = await _allowed_execution(store, idempotency_key="forced-drain")
    first = ActionService(store, echo_catalog, {"agentplane": executor}, drain_timeout=timedelta(0))
    second = ActionService(store, echo_catalog, {"agentplane": executor})
    try:
        await first.start()
        async with asyncio.timeout(5):
            await executor.started.wait()
            await first.close()
        final = await store.get(request_id, CALLER)
        assert final.state is ActionState.EXECUTION_UNKNOWN
        assert final.execution is not None
        assert final.execution.error is not None
        assert final.execution.error["kind"] == UnknownOutcomeReason.COORDINATOR_STOPPED
        assert await store.pending_dispatches() == []
        await second.start()
        await second.close()
        assert executor.calls == 1
    finally:
        await first.close()
        await second.close()


async def test_forced_drain_bounds_unavailable_outcome_persistence(
    engine: AsyncEngine, echo_catalog: ActionCatalog
) -> None:
    store = ActionStore(make_sessionmaker(engine))
    executor = GatedExecutor()
    request_id = await _allowed_execution(store, idempotency_key="hung-persistence")
    service = ActionService(
        store,
        echo_catalog,
        {"agentplane": executor},
        drain_timeout=timedelta(0),
        stop_timeout=timedelta(milliseconds=30),
    )
    persisting = asyncio.Event()
    persistence_stopped = asyncio.Event()

    async def unavailable_finish(
        request_id: UUID, executor_id: str, lease_token: UUID, result: ExecutionResult
    ) -> None:
        persisting.set()
        try:
            await asyncio.Event().wait()
        finally:
            persistence_stopped.set()

    with patch.object(store, "finish_execution", unavailable_finish):
        await service.start()
        async with asyncio.timeout(5):
            await executor.started.wait()
            await service.close()
    assert persisting.is_set()
    assert persistence_stopped.is_set()
    assert await store.pending_dispatches() == []
    # With persistence unavailable, the durable claim remains fenced for lease-sweep recovery.
    assert (await store.get(request_id, CALLER)).state is ActionState.RUNNING


if __name__ == "__main__":
    pytest_bazel.main()
