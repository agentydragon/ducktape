"""Queueing, cancelling and ending commands in the Dispatch table, against real PostgreSQL."""

from uuid import UUID

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.proto import command_pb2, event_log_pb2
from agentplane.session_dispatcher.models import DeliveryState, Reason
from agentplane.session_dispatcher.store import (
    CommandConflictError,
    DispatchNotFoundError,
    DispatchStore,
    NotCancellableError,
    QueueFullError,
)

# gazelle:include_dep @pypi//protobuf

SANDBOX_UID = UUID("00000000-0000-4000-8000-000000000001")
SANDBOX = "test-sandbox"
SESSION = "test-session"
QUEUE_CAP = 3


@pytest.fixture
def store(engine: AsyncEngine) -> DispatchStore:
    return DispatchStore(engine, queue_cap=QUEUE_CAP)


def submit(command_id: str, text: str = "hello") -> command_pb2.Command:
    return command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=text))


def stop(command_id: str) -> command_pb2.Command:
    return command_pb2.Command(command_id=command_id, stop_runner_session=command_pb2.StopRunnerSession())


async def enqueue(
    store: DispatchStore,
    command: command_pb2.Command,
    *,
    session_id: str = SESSION,
    sandbox_uid: UUID | None = SANDBOX_UID,
) -> DeliveryState:
    return (
        await store.enqueue(session_id=session_id, sandbox_name=SANDBOX, sandbox_uid=sandbox_uid, command=command)
    ).state


async def test_a_repeated_command_is_the_same_dispatch_and_another_payload_conflicts(store: DispatchStore) -> None:
    first = await store.enqueue(session_id=SESSION, sandbox_name=SANDBOX, sandbox_uid=SANDBOX_UID, command=submit("a"))
    again = await store.enqueue(session_id=SESSION, sandbox_name=SANDBOX, sandbox_uid=SANDBOX_UID, command=submit("a"))
    assert again.position == first.position
    with pytest.raises(CommandConflictError):
        await enqueue(store, submit("a", text="other"))


async def test_a_command_for_a_sandbox_that_does_not_exist_has_failed(store: DispatchStore) -> None:
    row = await store.enqueue(session_id=SESSION, sandbox_name=SANDBOX, sandbox_uid=None, command=submit("a"))
    assert (row.state, row.reason) == (DeliveryState.FAILED, Reason.SANDBOX_DELETED)
    assert await store.heads() == []


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
    assert [row.command_id for row in await store.heads()] == ["a", "other"]
    await store.settle("a", from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER)
    assert [row.command_id for row in await store.heads()] == ["other"]
    await store.settle("a", from_states={DeliveryState.DELIVERING_TO_RUNNER}, to=DeliveryState.RUNNER_ADMITTED)
    assert [row.command_id for row in await store.heads()] == ["b", "other"]


async def test_settle_leaves_a_dispatch_that_already_moved_alone(store: DispatchStore) -> None:
    await enqueue(store, submit("a"))
    await store.cancel("a")
    assert (
        await store.settle("a", from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER)
        is None
    )
    assert (await store.get_dispatch("a")).state is DeliveryState.CANCELLED_IN_DISPATCHER


async def test_only_a_queued_command_is_cancelled(store: DispatchStore) -> None:
    await enqueue(store, submit("a"))
    await store.settle("a", from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER)
    with pytest.raises(NotCancellableError):
        await store.cancel("a")
    with pytest.raises(DispatchNotFoundError):
        await store.cancel("missing")


async def test_a_deleted_sandbox_fails_what_was_queued_and_leaves_the_outcome_of_the_rest_unknown(
    store: DispatchStore,
) -> None:
    for command_id in ("queued", "delivering", "admitted"):
        await enqueue(store, submit(command_id), session_id=command_id)
    await enqueue(store, submit("other-sandbox"), session_id="other", sandbox_uid=UUID(int=2))
    await store.settle(
        "delivering", from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER
    )
    await store.settle(
        "admitted",
        from_states={DeliveryState.QUEUED_IN_DISPATCHER},
        to=DeliveryState.RUNNER_ADMITTED,
        admission=event_log_pb2.EventEntry(),
    )
    assert await store.unfinished_sandbox_uids() == {SANDBOX_UID, UUID(int=2)}
    await store.end_sandbox(SANDBOX_UID)
    states = {row.command_id: row.state for row in await store.list_dispatches(sandbox_name=SANDBOX)}
    assert states == {
        "queued": DeliveryState.FAILED,
        "delivering": DeliveryState.UNKNOWN,
        "admitted": DeliveryState.UNKNOWN,
        "other-sandbox": DeliveryState.QUEUED_IN_DISPATCHER,
    }
    assert await store.unfinished_sandbox_uids() == {UUID(int=2)}


async def test_a_call_in_flight_when_the_dispatcher_stopped_is_queued_again(store: DispatchStore) -> None:
    await enqueue(store, submit("a"))
    await store.settle("a", from_states={DeliveryState.QUEUED_IN_DISPATCHER}, to=DeliveryState.DELIVERING_TO_RUNNER)
    await store.reset_delivering()
    row = await store.get_dispatch("a")
    assert (row.state, row.reason) == (DeliveryState.QUEUED_IN_DISPATCHER, Reason.DISPATCHER_RESTARTED)


if __name__ == "__main__":
    pytest_bazel.main()
