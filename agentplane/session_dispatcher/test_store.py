"""Queueing, cancelling and ending commands in the Dispatch table, against real PostgreSQL."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.proto import command_pb2, event_log_pb2, runner_pb2, session_dispatcher_pb2
from agentplane.session_dispatcher.db import SessionOpening, WakeAttempt
from agentplane.session_dispatcher.models import DeliveryState, Reason
from agentplane.session_dispatcher.store import (
    WAKE_LIMIT,
    WAKE_WINDOW_S,
    CommandConflictError,
    DispatchNotFoundError,
    DispatchStore,
    NotCancellableError,
    OpeningConflictError,
    QueueFullError,
    SandboxOwner,
)
from agentplane.session_dispatcher.terminal_broadcast import TerminalBroadcast
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf

SANDBOX_UID = UUID("00000000-0000-4000-8000-000000000001")
SANDBOX = "test-sandbox"
SESSION = "test-session"
QUEUE_CAP = 3
WAKE_CALLER = ServiceAccountRef(namespace="agentplane", name="app")


@pytest.fixture
def store(engine: AsyncEngine, terminal_broadcast: TerminalBroadcast) -> DispatchStore:
    return DispatchStore(engine, queue_cap=QUEUE_CAP, terminal_broadcast=terminal_broadcast)


def submit(command_id: str, text: str = "hello") -> command_pb2.Command:
    return command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=text))


def stop(command_id: str) -> command_pb2.Command:
    return command_pb2.Command(command_id=command_id, stop_runner_session=command_pb2.StopRunnerSession())


async def enqueue(
    store: DispatchStore, command: command_pb2.Command, *, session_id: str = SESSION, sandbox_uid: UUID = SANDBOX_UID
) -> DeliveryState:
    return (
        await store.enqueue(session_id=session_id, sandbox_name=SANDBOX, sandbox_uid=sandbox_uid, command=command)
    ).state


async def owner_token(
    store: DispatchStore,
    *,
    sandbox_uid: UUID = SANDBOX_UID,
    lease_uid: str = "lease-1",
    lease_transitions: int = 1,
    holder_identity: str = "dispatcher-1",
    expected_generation: int | None = None,
) -> SandboxOwner:
    if expected_generation is None:
        expected_generation = await store.ownership_generation(sandbox_uid)
    owner = await store.claim_sandbox_owner(
        sandbox_uid,
        lease_uid=lease_uid,
        lease_transitions=lease_transitions,
        holder_identity=holder_identity,
        expected_generation=expected_generation,
    )
    assert owner is not None
    return owner


async def test_a_repeated_command_is_the_same_dispatch_and_another_payload_conflicts(store: DispatchStore) -> None:
    first = await store.enqueue(session_id=SESSION, sandbox_name=SANDBOX, sandbox_uid=SANDBOX_UID, command=submit("a"))
    again = await store.enqueue(session_id=SESSION, sandbox_name=SANDBOX, sandbox_uid=SANDBOX_UID, command=submit("a"))
    assert again.position == first.position
    with pytest.raises(CommandConflictError):
        await enqueue(store, submit("a", text="other"))


async def test_a_session_queues_no_more_than_the_cap_and_a_stop_is_not_counted(store: DispatchStore) -> None:
    for number in range(QUEUE_CAP):
        await enqueue(store, submit(f"input-{number}"))
    with pytest.raises(QueueFullError):
        await enqueue(store, submit("one-too-many"))
    # Another Session is unaffected.
    assert await enqueue(store, submit("elsewhere"), session_id="other-session") is DeliveryState.QUEUED_IN_DISPATCHER
    assert await enqueue(store, stop("stop")) is DeliveryState.QUEUED_IN_DISPATCHER


async def test_a_stop_cancels_what_its_session_has_queued_and_goes_first(store: DispatchStore) -> None:
    await enqueue(store, submit("a"))
    await enqueue(store, submit("b"))
    await enqueue(store, submit("elsewhere"), session_id="other-session")
    await enqueue(store, stop("stop"))
    states = {row.command_id: (row.state, row.reason) for row in await store.list_dispatches(session_id=SESSION)}
    assert states == {
        "a": (DeliveryState.CANCELLED_IN_DISPATCHER, Reason.STOPPED),
        "b": (DeliveryState.CANCELLED_IN_DISPATCHER, Reason.STOPPED),
        "stop": (DeliveryState.QUEUED_IN_DISPATCHER, None),
    }
    assert (await store.get_dispatch("elsewhere")).state is DeliveryState.QUEUED_IN_DISPATCHER


async def test_a_session_has_one_head_and_none_while_an_earlier_command_is_in_flight(store: DispatchStore) -> None:
    await enqueue(store, submit("a"))
    await enqueue(store, submit("b"))
    await enqueue(store, submit("other"), session_id="other-session")
    owner = await owner_token(store)
    assert [row.command_id for row in await store.heads()] == ["a", "other"]
    await store.settle(
        "a", owner=owner, from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER
    )
    assert [row.command_id for row in await store.heads()] == ["other"]
    await store.settle(
        "a", owner=owner, from_states={DeliveryState.DELIVERING_TO_RUNNER}, to=DeliveryState.RUNNER_ADMITTED
    )
    assert [row.command_id for row in await store.heads()] == ["b", "other"]


async def test_wake_attempts_are_shared_across_store_instances_and_deduplicate_retries(
    store: DispatchStore, engine: AsyncEngine
) -> None:
    for generation in range(1, WAKE_LIMIT + 1):
        assert (
            await store.reserve_wake_attempt(caller=WAKE_CALLER, sandbox_uid=SANDBOX_UID, wake_generation=generation)
            is None
        )

    # A retry for the same guarded generation does not spend another unit.
    assert await store.reserve_wake_attempt(caller=WAKE_CALLER, sandbox_uid=SANDBOX_UID, wake_generation=1) is None

    restarted_store = DispatchStore(engine, queue_cap=QUEUE_CAP, terminal_broadcast=store.terminal_broadcast)
    retry_after = await restarted_store.reserve_wake_attempt(
        caller=WAKE_CALLER, sandbox_uid=UUID(int=2), wake_generation=1
    )
    assert retry_after is not None
    assert 0 < retry_after <= WAKE_WINDOW_S
    # A different authenticated caller has an independent budget.
    assert (
        await restarted_store.reserve_wake_attempt(
            caller=ServiceAccountRef(namespace="agentplane", name="notifications"),
            sandbox_uid=UUID(int=2),
            wake_generation=1,
        )
        is None
    )


async def test_concurrent_wake_reservations_cannot_exceed_one_callers_budget(store: DispatchStore) -> None:
    outcomes = await asyncio.gather(
        *(
            store.reserve_wake_attempt(caller=WAKE_CALLER, sandbox_uid=UUID(int=number + 1), wake_generation=1)
            for number in range(WAKE_LIMIT + 1)
        )
    )
    assert sum(outcome is None for outcome in outcomes) == WAKE_LIMIT
    assert sum(outcome is not None for outcome in outcomes) == 1


async def test_wake_reservation_is_charged_again_after_the_window_expires(store: DispatchStore) -> None:
    await store.reserve_wake_attempt(caller=WAKE_CALLER, sandbox_uid=SANDBOX_UID, wake_generation=1)
    async with store.sessions.begin() as session:
        await session.execute(update(WakeAttempt).values(attempted_at=datetime(2000, 1, 1, tzinfo=UTC)))
    assert await store.reserve_wake_attempt(caller=WAKE_CALLER, sandbox_uid=SANDBOX_UID, wake_generation=1) is None
    async with store.sessions() as session:
        row_count = await session.scalar(select(func.count()).select_from(WakeAttempt))
    assert row_count == 1


async def test_settle_leaves_a_dispatch_that_already_moved_alone(store: DispatchStore) -> None:
    await enqueue(store, submit("a"))
    owner = await owner_token(store)
    await store.cancel("a")
    assert (
        await store.settle(
            "a", owner=owner, from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER
        )
        is None
    )
    assert (await store.get_dispatch("a")).state is DeliveryState.CANCELLED_IN_DISPATCHER


async def test_only_a_queued_command_is_cancelled(store: DispatchStore) -> None:
    await enqueue(store, submit("a"))
    owner = await owner_token(store)
    await store.settle(
        "a", owner=owner, from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER
    )
    with pytest.raises(NotCancellableError):
        await store.cancel("a")
    with pytest.raises(DispatchNotFoundError):
        await store.cancel("missing")


async def test_a_deleted_sandbox_ends_its_commands_and_takes_its_rows(store: DispatchStore) -> None:
    for command_id in ("queued", "delivering", "admitted"):
        await enqueue(store, submit(command_id), session_id=command_id)
    await enqueue(
        store,
        command_pb2.Command(command_id="model-admitted", change_model=command_pb2.ChangeModel(model="test-model")),
        session_id="model-admitted",
    )
    await enqueue(store, submit("other-sandbox"), session_id="other", sandbox_uid=UUID(int=2))
    owner = await owner_token(store)
    await store.settle(
        "delivering",
        owner=owner,
        from_states={DeliveryState.QUEUED_IN_DISPATCHER},
        to=DeliveryState.DELIVERING_TO_RUNNER,
    )
    for command_id in ("admitted", "model-admitted"):
        await store.settle(
            command_id,
            owner=owner,
            from_states={DeliveryState.QUEUED_IN_DISPATCHER},
            to=DeliveryState.RUNNER_ADMITTED,
            admission=event_log_pb2.EventEntry(),
        )
    assert await store.list_sandbox_uids() == {SANDBOX_UID, UUID(int=2)}
    ended = await store.end_sandbox(SANDBOX_UID)
    assert {event.dispatch.command_id: event.dispatch.state for event in ended} == {
        "queued": session_dispatcher_pb2.DELIVERY_STATE_FAILED,
        "delivering": session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
        "admitted": session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
    }
    assert [row.command_id for row in await store.list_dispatches(sandbox_name=SANDBOX)] == ["other-sandbox"]
    assert await store.list_sandbox_uids() == {UUID(int=2)}


async def test_a_session_opens_once_and_its_opening_goes_before_its_commands(store: DispatchStore) -> None:
    spec = runner_pb2.SessionSpec(model="test-model")

    async def open_session(
        *,
        sandbox_name: str = SANDBOX,
        sandbox_uid: UUID = SANDBOX_UID,
        spec: runner_pb2.SessionSpec = spec,
        setup_script: str | None = None,
    ) -> SessionOpening:
        return await store.open_session(
            session_id=SESSION, sandbox_name=sandbox_name, sandbox_uid=sandbox_uid, spec=spec, setup_script=setup_script
        )

    opening = await open_session()
    assert opening.state == DeliveryState.QUEUED_IN_DISPATCHER
    await enqueue(store, submit("first"))
    assert await store.heads() == []
    assert (await open_session()).created_at == opening.created_at
    for conflict in (
        open_session(sandbox_name="other-sandbox"),
        open_session(sandbox_uid=UUID(int=2)),
        open_session(spec=runner_pb2.SessionSpec(model="other-model")),
        open_session(setup_script="true"),
    ):
        with pytest.raises(OpeningConflictError):
            await conflict
    owner = await owner_token(store)
    await store.settle_opening(
        SESSION, owner=owner, from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.RUNNER_ADMITTED
    )
    assert [row.command_id for row in await store.heads()] == ["first"]


async def test_a_deleted_sandbox_ends_its_openings_and_takes_them(store: DispatchStore) -> None:
    await store.open_session(
        session_id=SESSION,
        sandbox_name=SANDBOX,
        sandbox_uid=SANDBOX_UID,
        spec=runner_pb2.SessionSpec(),
        setup_script=None,
    )
    await store.end_sandbox(SANDBOX_UID)
    with pytest.raises(DispatchNotFoundError):
        await store.get_opening(SESSION)


async def test_failover_recovery_is_scoped_and_stale_holders_cannot_settle_or_reclaim(store: DispatchStore) -> None:
    await enqueue(store, submit("a"))
    await enqueue(store, submit("other"), session_id="other", sandbox_uid=UUID(int=2))
    old_owner = await owner_token(store)
    other_owner = await owner_token(store, sandbox_uid=UUID(int=2), lease_uid="lease-2")
    await store.settle(
        "a", owner=old_owner, from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER
    )
    await store.settle(
        "other",
        owner=other_owner,
        from_states={DeliveryState.QUEUED_IN_DISPATCHER},
        to=DeliveryState.DELIVERING_TO_RUNNER,
    )

    new_owner = await owner_token(
        store, lease_transitions=2, holder_identity="dispatcher-2", expected_generation=old_owner.generation
    )
    assert (
        await store.settle(
            "a", owner=old_owner, from_states={DeliveryState.DELIVERING_TO_RUNNER}, to=DeliveryState.RUNNER_ADMITTED
        )
        is None
    )
    # The old process was paused after observing its Lease. A successor committed, so even a
    # recreated Lease UID cannot make the old proof advance the DB generation.
    assert (
        await store.claim_sandbox_owner(
            SANDBOX_UID,
            lease_uid="recreated-lease-for-stale-owner",
            lease_transitions=1,
            holder_identity="dispatcher-1",
            expected_generation=old_owner.generation,
        )
        is None
    )
    assert await store.recover_sandbox(old_owner) is False
    assert await store.recover_sandbox(new_owner) is True
    recovered = await store.get_dispatch("a")
    untouched = await store.get_dispatch("other")
    assert (recovered.state, recovered.reason) == (DeliveryState.QUEUED_IN_DISPATCHER, Reason.DISPATCHER_RESTARTED)
    assert untouched.state is DeliveryState.DELIVERING_TO_RUNNER


if __name__ == "__main__":
    pytest_bazel.main()
