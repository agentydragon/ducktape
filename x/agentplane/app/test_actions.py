"""Action Hub persistence, scope, final Decisions, and one-shot Execution semantics."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_bazel

from x.agentplane.app.actions import (
    ActionConflictError,
    ActionHub,
    ActionNotFoundError,
    ActionState,
    DecisionInput,
    EchoExecutor,
    ExecutionOutcomeUnknownError,
    ExecutionRequest,
    ExecutionResult,
    NewActionRequest,
    Verdict,
)
from x.agentplane.app.identity import CallerIdentity, CallerKind
from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.runner import protocol_pb2 as pb

CALLER = CallerIdentity(CallerKind.TOKEN, "system:serviceaccount:test:caller")
OTHER = CallerIdentity(CallerKind.TOKEN, "system:serviceaccount:test:other")
OPERATOR = CallerIdentity(CallerKind.OPERATOR, "operator")
SPEC = pb.SessionSpec(provider=pb.PROVIDER_CLAUDE, cwd="/work", model="test")


class ControlledExecutor:
    def __init__(self, result: ExecutionResult | Exception | None = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[ExecutionRequest] = []
        self.result = result or ExecutionResult(state=ActionState.SUCCEEDED, result={"done": True})

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({EchoExecutor.CAPABILITY})

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        self.started.set()
        await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
async def action_hub(store: TrajectoryStore) -> AsyncIterator[ActionHub]:
    hub = ActionHub(store.engine, EchoExecutor())
    await hub.ensure_schema()
    try:
        yield hub
    finally:
        await hub.close()


async def _thread(store: TrajectoryStore, session_id: str = "s-1") -> UUID:
    return await store.thread("sandbox", session_id, SPEC)


async def _submit(hub: ActionHub, thread_id: UUID, caller: CallerIdentity = CALLER):
    return await hub.submit(
        NewActionRequest(
            capability=EchoExecutor.CAPABILITY,
            arguments={"text": "hello", "token": "do-not-project", "nested": {"password": "hidden"}},
            origin_thread_id=thread_id,
        ),
        caller,
    )


async def _wait_for_state(hub: ActionHub, request_id: UUID, state: ActionState) -> None:
    for _ in range(100):
        if (await hub.get(request_id, CALLER)).state is state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"action request did not reach {state}")


async def test_submission_is_durable_immutable_and_redacted(store: TrajectoryStore, action_hub: ActionHub) -> None:
    receipt = await _submit(action_hub, await _thread(store))

    assert receipt.state is ActionState.DECISION_PENDING
    assert receipt.arguments == {"text": "hello", "token": "[redacted]", "nested": {"password": "[redacted]"}}
    assert receipt.caller_principal == CALLER.principal
    assert receipt.origin_thread_id is not None
    assert await action_hub.history(receipt.id, CALLER) == [ActionState.DECISION_PENDING]

    restarted = ActionHub(store.engine, EchoExecutor())
    await restarted.ensure_schema()
    assert await restarted.get(receipt.id, CALLER) == receipt


async def test_caller_reads_only_its_own_and_operator_reads_all(store: TrajectoryStore, action_hub: ActionHub) -> None:
    own = await _submit(action_hub, await _thread(store, "s-own"))
    other = await _submit(action_hub, await _thread(store, "s-other"), OTHER)

    assert [view.id for view in await action_hub.list_requests(CALLER)] == [own.id]
    assert {view.id for view in await action_hub.list_requests(OPERATOR)} == {own.id, other.id}
    with pytest.raises(ActionNotFoundError):
        await action_hub.get(other.id, CALLER)


async def test_deny_is_final_idempotent_and_never_dispatches(store: TrajectoryStore) -> None:
    executor = ControlledExecutor()
    hub = ActionHub(store.engine, executor)
    await hub.ensure_schema()
    request = await _submit(hub, await _thread(store))
    body = DecisionInput(verdict=Verdict.DENY, expected_version=request.version, idempotency_key="deny-1")

    denied = await hub.decide(request.id, body, issuer=OPERATOR.principal, provider="human_operator")
    repeated = await hub.decide(request.id, body, issuer=OPERATOR.principal, provider="human_operator")

    assert denied.state is ActionState.DENIED
    assert repeated == denied
    assert denied.decision is not None
    assert denied.decision.verdict is Verdict.DENY
    assert denied.execution is None
    assert executor.calls == []
    assert await hub.history(request.id, CALLER) == [ActionState.DECISION_PENDING, ActionState.DENIED]
    with pytest.raises(ActionConflictError):
        await hub.decide(
            request.id,
            DecisionInput(verdict=Verdict.ALLOW, expected_version=denied.version, idempotency_key="allow-after-deny"),
            issuer=OPERATOR.principal,
            provider="human_operator",
        )


async def test_allow_dispatches_exactly_once_and_reaches_success(store: TrajectoryStore) -> None:
    executor = ControlledExecutor()
    hub = ActionHub(store.engine, executor)
    await hub.ensure_schema()
    try:
        request = await _submit(hub, await _thread(store))
        body = DecisionInput(verdict=Verdict.ALLOW, expected_version=request.version, idempotency_key="allow-1")

        allowed, repeated = await asyncio.gather(
            hub.decide(request.id, body, issuer=OPERATOR.principal, provider="human_operator"),
            hub.decide(request.id, body, issuer=OPERATOR.principal, provider="human_operator"),
        )
        await asyncio.wait_for(executor.started.wait(), timeout=1)

        assert allowed.state is ActionState.ALLOWED
        assert repeated.id == request.id
        assert len(executor.calls) == 1
        assert (await hub.get(request.id, CALLER)).state is ActionState.RUNNING

        executor.release.set()
        await _wait_for_state(hub, request.id, ActionState.SUCCEEDED)
        completed = await hub.get(request.id, CALLER)
        assert completed.execution is not None
        assert completed.execution.result == {"done": True}
        assert len(executor.calls) == 1
        assert await hub.history(request.id, CALLER) == [
            ActionState.DECISION_PENDING,
            ActionState.ALLOWED,
            ActionState.DISPATCHING,
            ActionState.RUNNING,
            ActionState.SUCCEEDED,
        ]
    finally:
        await hub.close()


@pytest.mark.parametrize(
    ("executor_result", "expected", "error_kind"),
    [
        (RuntimeError("adapter failed"), ActionState.FAILED, "RuntimeError"),
        (
            ExecutionOutcomeUnknownError("connection lost after send"),
            ActionState.EXECUTION_UNKNOWN,
            "execution_outcome_unknown",
        ),
        (
            ExecutionResult(state=ActionState.CANCELLED, error={"kind": "target_cancelled"}),
            ActionState.CANCELLED,
            "target_cancelled",
        ),
    ],
)
async def test_execution_failure_unknown_and_cancelled_are_terminal_without_retry(
    store: TrajectoryStore, executor_result: ExecutionResult | Exception, expected: ActionState, error_kind: str
) -> None:
    executor = ControlledExecutor(executor_result)
    hub = ActionHub(store.engine, executor)
    await hub.ensure_schema()
    try:
        request = await _submit(hub, await _thread(store))
        await hub.decide(
            request.id,
            DecisionInput(verdict=Verdict.ALLOW, expected_version=request.version, idempotency_key=f"allow-{expected}"),
            issuer=OPERATOR.principal,
            provider="human_operator",
        )
        await asyncio.wait_for(executor.started.wait(), timeout=1)
        executor.release.set()
        await _wait_for_state(hub, request.id, expected)

        terminal = await hub.get(request.id, CALLER)
        assert terminal.execution is not None
        assert terminal.execution.error is not None
        assert terminal.execution.error["kind"] == error_kind
        assert len(executor.calls) == 1
    finally:
        await hub.close()


async def test_restart_marks_inflight_execution_unknown_without_replay(store: TrajectoryStore) -> None:
    executor = ControlledExecutor()
    hub = ActionHub(store.engine, executor)
    await hub.ensure_schema()
    request = await _submit(hub, await _thread(store))
    await hub.decide(
        request.id,
        DecisionInput(verdict=Verdict.ALLOW, expected_version=request.version, idempotency_key="allow-before-restart"),
        issuer=OPERATOR.principal,
        provider="human_operator",
    )
    await asyncio.wait_for(executor.started.wait(), timeout=1)

    after_restart = ControlledExecutor()
    restarted = ActionHub(store.engine, after_restart)
    assert await restarted.recover_uncertain_executions() == 1
    recovered = await restarted.get(request.id, CALLER)
    assert recovered.state is ActionState.EXECUTION_UNKNOWN
    assert recovered.execution is not None
    assert recovered.execution.error == {
        "kind": "process_restarted",
        "message": "dispatch outcome is unknown; not replayed",
    }
    assert len(executor.calls) == 1
    assert after_restart.calls == []
    assert (await restarted.history(request.id, CALLER))[-1] is ActionState.EXECUTION_UNKNOWN
    await hub.close()


if __name__ == "__main__":
    pytest_bazel.main()
