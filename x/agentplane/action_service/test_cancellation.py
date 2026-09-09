"""Pre-claim withdrawal, competing transitions, ownership and durable retry semantics."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.catalog import ActionCatalog, ActionIdentity
from x.agentplane.action_service.db import ActionConflictError, ActionNotFoundError, ActionStore, make_sessionmaker
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CancellationOutcome,
    DecisionContext,
    DecisionInput,
    ExecutionLease,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    Principal,
    PrincipalRole,
    ProviderOutcome,
    ProviderVerdict,
    Verdict,
)
from x.agentplane.action_service.service import ActionService

CALLER = Principal(issuer="test-workload", subject="sandbox-a", role=PrincipalRole.CALLER)
OTHER_CALLER = Principal(issuer="test-workload", subject="sandbox-b", role=PrincipalRole.CALLER)
OPERATOR = Principal(issuer="test-bff", subject="operator", role=PrincipalRole.OPERATOR)
LEASE_DURATION = timedelta(seconds=30)


@pytest.fixture
def store(engine: AsyncEngine) -> ActionStore:
    return ActionStore(make_sessionmaker(engine))


@pytest.fixture
def envelope() -> ActionRequestInput:
    return ActionRequestInput(
        idempotency_key="test-cancellation",
        action=ActionIdentity(group="agentplane", name="echo"),
        arguments={},
        origin={"thread_id": "untrusted-thread", "caller_principal": OTHER_CALLER.key},
    )


@pytest.fixture
async def pending(store: ActionStore, envelope: ActionRequestInput) -> ActionRequestView:
    request, _ = await store.submit(envelope, CALLER)
    return request


def decision(pending: ActionRequestView, verdict: Verdict = Verdict.ALLOW) -> DecisionInput:
    return DecisionInput(verdict=verdict, expected_version=pending.version, idempotency_key="test-decision")


@pytest.fixture
async def allowed(store: ActionStore, pending: ActionRequestView) -> ActionRequestView:
    request, _ = await store.decide(pending.id, decision(pending), OPERATOR, provider=ActionService.HUMAN_PROVIDER)
    return request


async def test_pending_cancellation_is_durable_and_idempotent(
    store: ActionStore, engine: AsyncEngine, pending: ActionRequestView, envelope: ActionRequestInput
) -> None:
    result = await store.cancel(pending.id, CALLER)
    assert result.outcome is CancellationOutcome.CANCELLED
    assert result.request.state is ActionState.CANCELLED
    assert result.request.decision is None
    assert result.request.execution is None
    assert result.request.version == pending.version + 1
    restarted = ActionStore(make_sessionmaker(engine))
    duplicate = await restarted.cancel(pending.id, CALLER)
    assert duplicate.outcome is CancellationOutcome.ALREADY_CANCELLED
    assert duplicate.request == result.request
    retried, created = await restarted.submit(envelope, CALLER)
    assert not created
    assert retried == result.request
    fresh, created = await restarted.submit(envelope.model_copy(update={"idempotency_key": "fresh-attempt"}), CALLER)
    assert created
    assert fresh.id != pending.id
    assert fresh.state is ActionState.DECISION_PENDING
    events = await restarted.events(pending.id, CALLER)
    assert [event.state for event in events] == [ActionState.DECISION_PENDING, ActionState.CANCELLED]
    assert events[-1].actor_principal == CALLER.key
    assert events[-1].at == result.request.updated_at
    with pytest.raises(ActionConflictError):
        await store.decide(pending.id, decision(pending), OPERATOR, provider=ActionService.HUMAN_PROVIDER)
    # A refreshed version does not authorize a late approval either.
    with pytest.raises(ActionConflictError):
        await store.decide(pending.id, decision(result.request), OPERATOR, provider=ActionService.HUMAN_PROVIDER)


async def test_allowed_cancellation_preserves_decision_and_prevents_dispatch(
    store: ActionStore, engine: AsyncEngine, pending: ActionRequestView, allowed: ActionRequestView
) -> None:
    result = await store.cancel(allowed.id, CALLER)
    assert result.outcome is CancellationOutcome.CANCELLED
    assert result.request.decision == allowed.decision
    assert result.request.execution is not None
    assert result.request.execution.state is ExecutionState.CANCELLED
    assert result.request.execution.started_at is None
    assert result.request.execution.completed_at == result.request.updated_at
    assert result.request.execution.result is result.request.execution.error is None
    restarted = ActionStore(make_sessionmaker(engine))
    assert await restarted.pending_dispatches() == []
    assert (
        await restarted.claim_execution(allowed.id, executor_id="queued-worker", lease_duration=LEASE_DURATION) is None
    )
    replay, should_dispatch = await restarted.decide(
        allowed.id, decision(pending), OPERATOR, provider=ActionService.HUMAN_PROVIDER
    )
    assert replay.state is ActionState.CANCELLED
    assert not should_dispatch
    with pytest.raises(ActionConflictError):
        await restarted.mark_running(allowed.id)
    with pytest.raises(ActionConflictError):
        await restarted.finish_execution(
            allowed.id, "unclaimed-worker", uuid4(), ExecutionResult(state=ExecutionState.SUCCEEDED)
        )


@pytest.mark.parametrize("principal", [OTHER_CALLER, OPERATOR])
async def test_only_owning_principal_can_cancel(
    store: ActionStore, pending: ActionRequestView, principal: Principal
) -> None:
    with pytest.raises(ActionNotFoundError):
        await store.cancel(pending.id, principal)
    assert (await store.get(pending.id, CALLER)).state is ActionState.DECISION_PENDING
    await store.cancel(pending.id, CALLER)
    with pytest.raises(ActionNotFoundError):
        await store.cancel(pending.id, principal)


@pytest.mark.parametrize(
    "state",
    [
        ActionState.DISPATCHING,
        ActionState.RUNNING,
        ActionState.EXECUTION_UNKNOWN,
        ActionState.SUCCEEDED,
        ActionState.FAILED,
    ],
)
async def test_claim_is_cutoff_even_before_executor_starts(
    store: ActionStore, allowed: ActionRequestView, state: ActionState
) -> None:
    claim = await store.claim_execution(allowed.id, executor_id="test-worker", lease_duration=LEASE_DURATION)
    assert claim is not None
    if state is not ActionState.DISPATCHING:
        await store.mark_running(allowed.id)
    if state in {ActionState.EXECUTION_UNKNOWN, ActionState.SUCCEEDED, ActionState.FAILED}:
        await store.finish_execution(
            allowed.id, claim.executor_id, claim.lease_token, ExecutionResult(state=ExecutionState(state.value))
        )
    before = await store.get(allowed.id, CALLER)
    events = await store.events(allowed.id, CALLER)
    result = await store.cancel(allowed.id, CALLER)
    assert result.outcome is (
        CancellationOutcome.ALREADY_FINISHED
        if state in {ActionState.SUCCEEDED, ActionState.FAILED}
        else CancellationOutcome.TOO_LATE
    )
    assert result.request == before
    assert await store.events(allowed.id, CALLER) == events


async def test_denied_request_is_already_finished(store: ActionStore, pending: ActionRequestView) -> None:
    denied, _ = await store.decide(
        pending.id, decision(pending, Verdict.DENY), OPERATOR, provider=ActionService.HUMAN_PROVIDER
    )
    result = await store.cancel(pending.id, CALLER)
    assert result.outcome is CancellationOutcome.ALREADY_FINISHED
    assert result.request.state is denied.state
    assert result.request.decision == denied.decision
    assert result.request.version == denied.version


async def test_concurrent_claim_and_cancel_have_one_winner(store: ActionStore, allowed: ActionRequestView) -> None:
    claim, cancellation = await asyncio.gather(
        store.claim_execution(allowed.id, executor_id="racing-worker", lease_duration=LEASE_DURATION),
        store.cancel(allowed.id, CALLER),
    )
    if claim is None:
        assert cancellation.outcome is CancellationOutcome.CANCELLED
        assert (await store.get(allowed.id, CALLER)).state is ActionState.CANCELLED
    else:
        assert cancellation.outcome is CancellationOutcome.TOO_LATE
        assert (await store.get(allowed.id, CALLER)).state is ActionState.DISPATCHING


async def test_concurrent_approval_cannot_revive_cancelled_request(
    store: ActionStore, pending: ActionRequestView
) -> None:
    approval, cancellation = await asyncio.gather(
        store.decide(pending.id, decision(pending), OPERATOR, provider=ActionService.HUMAN_PROVIDER),
        store.cancel(pending.id, CALLER),
        return_exceptions=True,
    )
    assert not isinstance(cancellation, BaseException)
    assert cancellation.outcome is CancellationOutcome.CANCELLED
    if isinstance(approval, BaseException):
        assert isinstance(approval, ActionConflictError)
    assert (await store.get(pending.id, CALLER)).state is ActionState.CANCELLED
    assert await store.pending_dispatches() == []


class GatedProvider:
    name = "gated-provider"

    def __init__(self, verdict: ProviderVerdict) -> None:
        self.verdict = verdict
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(self, context: DecisionContext) -> ProviderOutcome:
        self.entered.set()
        await self.release.wait()
        return ProviderOutcome(verdict=self.verdict, reason_code="test-gated-vote")


class UnreachableExecutor:
    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        raise AssertionError("cancelled request reached executor")


@pytest.mark.parametrize("verdict", list(ProviderVerdict))
async def test_cancellation_during_provider_evaluation_is_returned_by_submit(
    store: ActionStore, echo_catalog: ActionCatalog, envelope: ActionRequestInput, verdict: ProviderVerdict
) -> None:
    provider = GatedProvider(verdict)
    service = ActionService(store, echo_catalog, {"agentplane": UnreachableExecutor()}, providers=[provider])
    try:
        async with asyncio.timeout(10):
            async with asyncio.TaskGroup() as tasks:
                submitted = tasks.create_task(service.submit(envelope, CALLER))
                await provider.entered.wait()
                pending_requests = await store.list_requests(CALLER)
                assert len(pending_requests) == 1
                await service.cancel(pending_requests[0].id, CALLER)
                provider.release.set()
        assert submitted.result().state is ActionState.CANCELLED
        assert submitted.result().decision is None
        assert submitted.result().execution is None
        assert await store.pending_dispatches() == []
    finally:
        await service.close()


if __name__ == "__main__":
    pytest_bazel.main()
