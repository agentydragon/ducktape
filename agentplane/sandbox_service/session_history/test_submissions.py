"""Draft acceptance/reconciliation contract against migrated PostgreSQL, not a live service."""

import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.sandbox_service.session_history import db, store
from agentplane.sandbox_service.session_history.store import HistoryConflictError, Store
from agentplane.sandbox_service.session_history.submissions import (
    CommandSubmission,
    SubmissionConflictError,
    SubmissionRefusedError,
    SubmissionState,
    SubmissionStore,
    submit_command,
)
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.asyncio
CALLER = ServiceAccountRef(namespace="testing", name="caller")


def make_input_submission(*, command_id: str, text: str) -> CommandSubmission:
    return CommandSubmission(
        command=command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=text))
    )


def admission(command: command_pb2.Command, cursor: int = 1) -> event_log_pb2.EventEntry:
    result = event_log_pb2.EventEntry(cursor=cursor)
    result.origin.source_id = "runner-source"
    result.origin.sequence = cursor
    result.event.command_admitted.command.CopyFrom(command)
    return result


@pytest.fixture
async def history(engine: AsyncEngine) -> tuple[Store, SubmissionStore, UUID]:
    session_id = uuid4()
    archive = Store(engine)
    await archive.open(
        session_id,
        sandbox_namespace="testing",
        sandbox_name="sandbox",
        sandbox_uid=uuid4(),
        runner_session_id="runner-session",
    )
    return archive, SubmissionStore(engine), session_id


async def test_persist_before_dispatch(history: tuple[Store, SubmissionStore, UUID]) -> None:
    archive, store, session_id = history
    request = make_input_submission(command_id="input", text="input text")

    async def dispatch(command: command_pb2.Command) -> event_log_pb2.EventEntry:
        assert (await store.read(session_id, "input")).state == SubmissionState.PENDING_ADMISSION
        assert command == command_pb2.Command(
            command_id="input", submit_input=command_pb2.SubmitInput(text="input text")
        )
        return admission(command)

    receipt = await submit_command(store, session_id, request, caller=CALLER, dispatch=dispatch)
    assert receipt == admission(request.copy_command())
    assert (await store.read(session_id, "input")).state == SubmissionState.ADMITTED
    # A direct receipt is not permission to skip the archive prefix.
    assert await archive.read(session_id) == (0, [])
    await archive.append(session_id, [receipt])
    await archive.append(session_id, [receipt])
    assert (await store.read(session_id, "input")).get_receipt() == receipt


async def test_lost_reply_recovers_via_spool_and_new_store(
    history: tuple[Store, SubmissionStore, UUID], engine: AsyncEngine
) -> None:
    archive, store, session_id = history
    request = make_input_submission(command_id="lost", text="hello")
    receipt = admission(request.copy_command())

    async def lost_reply(command: command_pb2.Command) -> event_log_pb2.EventEntry:
        raise TimeoutError("runner may have committed")

    with pytest.raises(TimeoutError):
        await submit_command(store, session_id, request, caller=CALLER, dispatch=lost_reply)
    assert (await store.read(session_id, "lost")).state == SubmissionState.PENDING_ADMISSION
    await archive.append(session_id, [receipt])

    async def must_not_dispatch(command: command_pb2.Command) -> event_log_pb2.EventEntry:
        raise AssertionError("an admitted retry must not dispatch")

    assert (
        await submit_command(SubmissionStore(engine), session_id, request, caller=CALLER, dispatch=must_not_dispatch)
        == receipt
    )


async def test_ingestion_and_direct_receipt_race(history: tuple[Store, SubmissionStore, UUID]) -> None:
    archive, store, session_id = history
    request = make_input_submission(command_id="race", text="hello")
    await store.accept(session_id, request, caller=CALLER)
    receipt = admission(request.copy_command())
    await asyncio.gather(archive.append(session_id, [receipt]), store.record_admission(session_id, receipt))
    assert (await store.read(session_id, "race")).get_receipt() == receipt
    assert (await store.record_rejection(session_id, "race", "late refusal")).state == SubmissionState.ADMITTED


async def test_reconciliation_and_checkpoint_roll_back_together(history: tuple[Store, SubmissionStore, UUID]) -> None:
    archive, store, session_id = history
    request = make_input_submission(command_id="atomic", text="hello")
    await store.accept(session_id, request, caller=CALLER)
    receipt = admission(request.copy_command())
    with pytest.raises(HistoryConflictError, match="expected 2"):
        await archive.append(session_id, [receipt, admission(request.copy_command(), 3)])
    assert await archive.read(session_id) == (0, [])
    assert (await store.read(session_id, "atomic")).state == SubmissionState.PENDING_ADMISSION
    await archive.append(session_id, [receipt])
    assert (await store.read(session_id, "atomic")).state == SubmissionState.ADMITTED


async def test_concurrent_identical_and_conflicting_acceptance(
    history: tuple[Store, SubmissionStore, UUID], engine: AsyncEngine
) -> None:
    _, store, session_id = history
    request = make_input_submission(command_id="same", text="original")
    results = await asyncio.gather(
        *[SubmissionStore(engine).accept(session_id, request, caller=CALLER) for _ in range(3)]
    )
    assert all(result.state == SubmissionState.PENDING_ADMISSION for result in results)
    changed = [make_input_submission(command_id="same", text="different")]
    for conflict in changed:
        with pytest.raises(SubmissionConflictError):
            await store.accept(session_id, conflict, caller=CALLER)
    with pytest.raises(SubmissionConflictError):
        await store.accept(session_id, request, caller=ServiceAccountRef(namespace="testing", name="other"))


async def test_definitive_refusal_is_retained(history: tuple[Store, SubmissionStore, UUID]) -> None:
    _, store, session_id = history
    request = make_input_submission(command_id="refused", text="hello")
    attempts = 0

    async def refuse(command: command_pb2.Command) -> event_log_pb2.EventEntry:
        nonlocal attempts
        attempts += 1
        raise SubmissionRefusedError("definitive refusal")

    for _ in range(2):
        with pytest.raises(SubmissionRefusedError, match="definitive refusal"):
            await submit_command(store, session_id, request, caller=CALLER, dispatch=refuse)
    assert attempts == 1
    assert (await store.read(session_id, "refused")).state == SubmissionState.REJECTED


async def test_bad_receipt_never_advances_submission(history: tuple[Store, SubmissionStore, UUID]) -> None:
    _, store, session_id = history
    request = make_input_submission(command_id="original", text="hello")
    await store.accept(session_id, request, caller=CALLER)
    with pytest.raises(SubmissionConflictError):
        await store.record_admission(
            session_id, admission(make_input_submission(command_id="original", text="changed").copy_command())
        )
    assert (await store.read(session_id, "original")).state == SubmissionState.PENDING_ADMISSION


async def test_cancelled_dispatch_stays_pending(history: tuple[Store, SubmissionStore, UUID]) -> None:
    _, store, session_id = history
    request = make_input_submission(command_id="cancelled", text="hello")

    async def cancel(command: command_pb2.Command) -> event_log_pb2.EventEntry:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await submit_command(store, session_id, request, caller=CALLER, dispatch=cancel)
    assert (await store.read(session_id, "cancelled")).state == SubmissionState.PENDING_ADMISSION


@pytest.mark.parametrize(
    "command",
    [
        command_pb2.Command(command_id="input", submit_input=command_pb2.SubmitInput(text="hello")),
        command_pb2.Command(command_id="model", change_model=command_pb2.ChangeModel(model="model")),
        command_pb2.Command(
            command_id="effort", change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort="high")
        ),
        command_pb2.Command(command_id="interrupt", interrupt_turn=command_pb2.InterruptTurn(turn_id="turn")),
        command_pb2.Command(command_id="stop", stop_runner_session=command_pb2.StopRunnerSession()),
    ],
)
async def test_admit_every_runner_operation_and_load_typed_columns(
    history: tuple[Store, SubmissionStore, UUID], engine: AsyncEngine, command: command_pb2.Command
) -> None:
    archive, store, session_id = history
    request = CommandSubmission(command=command)

    async def dispatch(value: command_pb2.Command) -> event_log_pb2.EventEntry:
        return admission(value)

    receipt = await submit_command(store, session_id, request, caller=CALLER, dispatch=dispatch)
    await archive.append(session_id, [receipt])
    async with async_sessionmaker(engine)() as session:
        row = await session.get(db.CommandSubmission, (session_id, command.command_id))
        assert row is not None
        assert isinstance(row.runner_command, command_pb2.Command)
        assert row.runner_command == command
        assert isinstance(row.admission, event_log_pb2.EventEntry)
        assert row.admission == receipt


async def test_unknown_fields_round_trip_and_participate_in_retry_identity(
    history: tuple[Store, SubmissionStore, UUID], engine: AsyncEngine
) -> None:
    _, store, session_id = history
    command = command_pb2.Command(command_id="future", change_model=command_pb2.ChangeModel(model="model"))
    # Unknown varint field 100 = 123, as a newer writer could append to the Command.
    future = command_pb2.Command.FromString(command.SerializeToString() + b"\xa0\x06\x7b")
    request = CommandSubmission(command=future)
    await store.accept(session_id, request, caller=CALLER)
    async with async_sessionmaker(engine)() as session:
        row = await session.get(db.CommandSubmission, (session_id, "future"))
        assert row is not None
        assert row.runner_command == future
        assert row.runner_command.SerializeToString(deterministic=True) == future.SerializeToString(deterministic=True)
    await SubmissionStore(engine).accept(session_id, request, caller=CALLER)
    with pytest.raises(SubmissionConflictError):
        await store.accept(session_id, CommandSubmission(command=command), caller=CALLER)


async def test_caller_mutation_during_dispatch_does_not_change_submission(
    history: tuple[Store, SubmissionStore, UUID],
) -> None:
    _, store, session_id = history
    request = make_input_submission(command_id="mutable", text="original")
    expected = request.copy_command()

    async def dispatch(value: command_pb2.Command) -> event_log_pb2.EventEntry:
        request.command.submit_input.text = "mutated by caller while awaiting reply"
        return admission(value)

    receipt = await submit_command(store, session_id, request, caller=CALLER, dispatch=dispatch)
    assert receipt.event.command_admitted.command == expected
    with pytest.raises(SubmissionConflictError):
        await store.accept(session_id, request, caller=CALLER)


async def test_interrupt_is_not_blocked_behind_an_input_receipt_wait(
    history: tuple[Store, SubmissionStore, UUID],
) -> None:
    _, store, session_id = history
    input_waiting, release_input = asyncio.Event(), asyncio.Event()
    request = make_input_submission(command_id="waiting", text="hello")

    async def dispatch_input(command: command_pb2.Command) -> event_log_pb2.EventEntry:
        input_waiting.set()
        await release_input.wait()
        return admission(command)

    async def dispatch_control(command: command_pb2.Command) -> event_log_pb2.EventEntry:
        assert command.HasField("interrupt_turn")
        return admission(command, 2)

    task = asyncio.create_task(submit_command(store, session_id, request, caller=CALLER, dispatch=dispatch_input))
    try:
        await asyncio.wait_for(input_waiting.wait(), timeout=10)
        control = CommandSubmission(
            command=command_pb2.Command(
                command_id="interrupt", interrupt_turn=command_pb2.InterruptTurn(turn_id="turn")
            )
        )
        receipt = await asyncio.wait_for(
            submit_command(store, session_id, control, caller=CALLER, dispatch=dispatch_control), timeout=10
        )
        assert receipt.event.command_admitted.command == control.command
        assert not task.done()
    finally:
        release_input.set()
        await task


async def test_acceptance_does_not_wait_for_ingestion_transaction(
    history: tuple[Store, SubmissionStore, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, submissions, session_id = history
    ingestion_waiting, release_ingestion = asyncio.Event(), asyncio.Event()
    reconcile = store.reconcile_admission

    async def pause_ingestion(session: AsyncSession, identity: UUID, entry: event_log_pb2.EventEntry) -> None:
        await reconcile(session, identity, entry)
        ingestion_waiting.set()
        await release_ingestion.wait()

    monkeypatch.setattr(store, "reconcile_admission", pause_ingestion)
    earlier = make_input_submission(command_id="being-ingested", text="earlier")
    await submissions.accept(session_id, earlier, caller=CALLER)
    task = asyncio.create_task(archive.append(session_id, [admission(earlier.copy_command())]))
    try:
        await asyncio.wait_for(ingestion_waiting.wait(), timeout=10)
        request = make_input_submission(command_id="independent", text="hello")
        result = await asyncio.wait_for(submissions.accept(session_id, request, caller=CALLER), timeout=10)
        assert result.state == SubmissionState.PENDING_ADMISSION
        settled = await asyncio.wait_for(
            submissions.record_admission(session_id, admission(request.copy_command(), 2)), timeout=10
        )
        assert settled.state == SubmissionState.ADMITTED
        assert not task.done()
    finally:
        release_ingestion.set()
        await task


if __name__ == "__main__":
    pytest_bazel.main()
