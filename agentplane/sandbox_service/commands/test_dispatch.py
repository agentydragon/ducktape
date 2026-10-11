"""Dispatch attempts against migrated PostgreSQL: once per epoch, deadlines and receipt evidence."""

from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.proto import runner_channel_pb2 as channel_pb2
from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.sandbox_service.commands.dispatch import CommandKey, DispatchStore, ReceiptMismatchError, Refused
from agentplane.sandbox_service.commands.store import CommandSubmission, SubmissionState, SubmissionStore
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf

CALLER = ServiceAccountRef(namespace="testing", name="caller")
COMMAND = command_pb2.Command(command_id="test-command", submit_input=command_pb2.SubmitInput(text="hello"))
RUNNER_SESSION = "test-runner-session"
REFUSED = channel_pb2.CommandRefused(reason=channel_pb2.CommandRefused.REASON_HARNESS_STOPPED)
EPOCH = 1


def admission(command: command_pb2.Command) -> event_log_pb2.EventEntry:
    result = event_log_pb2.EventEntry(cursor=1)
    result.origin.source_id = "test-runner-source"
    result.origin.sequence = 1
    result.event.command_admitted.command.CopyFrom(command)
    return result


@pytest.fixture
def dispatches(engine: AsyncEngine) -> DispatchStore:
    return DispatchStore(engine)


@pytest.fixture
def sandbox_uid() -> UUID:
    return uuid4()


@pytest.fixture
def session_id() -> UUID:
    return uuid4()


@pytest.fixture
async def submissions(engine: AsyncEngine, session_id: UUID) -> SubmissionStore:
    store = SubmissionStore(engine)
    await store.accept(session_id, CommandSubmission(command=COMMAND), caller=CALLER)
    return store


def key_of(session_id: UUID) -> CommandKey:
    return CommandKey(session_id, COMMAND.command_id)


async def open_attempt(dispatches: DispatchStore, sandbox_uid: UUID, session_id: UUID, deadline_s: float = 60) -> None:
    await dispatches.open(
        key_of(session_id), sandbox_uid=sandbox_uid, runner_session_id=RUNNER_SESSION, deadline_s=deadline_s
    )


def receipt(
    *, admitted: event_log_pb2.EventEntry | None = None, refused: channel_pb2.CommandRefused | None = None
) -> channel_pb2.CommandReceipt:
    return channel_pb2.CommandReceipt(
        session_id=RUNNER_SESSION, command_id=COMMAND.command_id, admitted=admitted, refused=refused
    )


@pytest.mark.usefixtures("submissions")
async def test_attempt_is_sent_at_most_once_per_connection_epoch(
    dispatches: DispatchStore, sandbox_uid: UUID, session_id: UUID
) -> None:
    key = key_of(session_id)
    await open_attempt(dispatches, sandbox_uid, session_id)
    delivery = await dispatches.claim(key, sandbox_uid=sandbox_uid, epoch=2)
    assert delivery is not None
    assert (delivery.runner_session_id, delivery.command) == (RUNNER_SESSION, COMMAND)
    assert await dispatches.claim(key, sandbox_uid=sandbox_uid, epoch=2) is None
    # A newer connection may resend; an older one may not send what a newer one already has.
    assert await dispatches.claim(key, sandbox_uid=sandbox_uid, epoch=1) is None
    assert await dispatches.claim(key, sandbox_uid=sandbox_uid, epoch=3) is not None
    assert await dispatches.claim(key, sandbox_uid=uuid4(), epoch=4) is None


@pytest.mark.usefixtures("submissions")
async def test_expired_attempt_is_never_sent_but_a_late_receipt_counts(
    dispatches: DispatchStore, sandbox_uid: UUID, session_id: UUID
) -> None:
    await open_attempt(dispatches, sandbox_uid, session_id, deadline_s=0)
    assert await dispatches.claim(key_of(session_id), sandbox_uid=sandbox_uid, epoch=EPOCH) is None
    await dispatches.record(sandbox_uid, receipt(admitted=admission(COMMAND)))
    assert await dispatches.read_outcome(key_of(session_id)) == admission(COMMAND)


async def test_admission_never_regresses_and_ends_delivery(
    dispatches: DispatchStore, submissions: SubmissionStore, sandbox_uid: UUID, session_id: UUID
) -> None:
    key = key_of(session_id)
    await open_attempt(dispatches, sandbox_uid, session_id)
    await dispatches.record(sandbox_uid, receipt(refused=REFUSED))
    assert await dispatches.read_outcome(key) == Refused(REFUSED)
    assert (await submissions.read(session_id, COMMAND.command_id)).state == SubmissionState.PENDING_ADMISSION
    await open_attempt(dispatches, sandbox_uid, session_id)
    await dispatches.record(sandbox_uid, receipt(admitted=admission(COMMAND)))
    await dispatches.record(sandbox_uid, receipt(refused=REFUSED))
    assert await dispatches.read_outcome(key) == admission(COMMAND)
    # Nothing is delivered after admission, even when a submit reopens the attempt.
    await open_attempt(dispatches, sandbox_uid, session_id)
    assert await dispatches.claim(key, sandbox_uid=sandbox_uid, epoch=EPOCH) is None


@pytest.mark.usefixtures("submissions")
async def test_a_resubmit_reopens_a_refused_attempt_but_not_an_open_one(
    dispatches: DispatchStore, sandbox_uid: UUID, session_id: UUID
) -> None:
    key = key_of(session_id)
    await open_attempt(dispatches, sandbox_uid, session_id)
    assert await dispatches.claim(key, sandbox_uid=sandbox_uid, epoch=EPOCH) is not None
    # While the attempt is open, a resubmit does not send it again on the same connection.
    await open_attempt(dispatches, sandbox_uid, session_id)
    assert await dispatches.claim(key, sandbox_uid=sandbox_uid, epoch=EPOCH) is None
    await dispatches.record(sandbox_uid, receipt(refused=REFUSED))
    assert await dispatches.claim(key, sandbox_uid=sandbox_uid, epoch=EPOCH) is None
    # After the refusal, a resubmit (say, once the harness is resumed) may send it again.
    await open_attempt(dispatches, sandbox_uid, session_id)
    assert await dispatches.read_outcome(key) is None
    assert await dispatches.claim(key, sandbox_uid=sandbox_uid, epoch=EPOCH) is not None


@pytest.mark.usefixtures("submissions")
@pytest.mark.parametrize(
    ("other_sandbox", "runner_session", "command_id"),
    [
        (True, RUNNER_SESSION, COMMAND.command_id),
        (False, "test-other-session", COMMAND.command_id),
        (False, RUNNER_SESSION, "test-unknown-command"),
    ],
    ids=["other-sandbox", "other-session", "unknown-command"],
)
async def test_receipt_must_match_a_command_sent_to_the_sandbox(
    dispatches: DispatchStore,
    sandbox_uid: UUID,
    session_id: UUID,
    other_sandbox: bool,
    runner_session: str,
    command_id: str,
) -> None:
    await open_attempt(dispatches, sandbox_uid, session_id)
    mismatched = channel_pb2.CommandReceipt(session_id=runner_session, command_id=command_id, refused=REFUSED)
    with pytest.raises(ReceiptMismatchError):
        await dispatches.record(uuid4() if other_sandbox else sandbox_uid, mismatched)
    assert await dispatches.read_outcome(key_of(session_id)) is None


if __name__ == "__main__":
    pytest_bazel.main()
