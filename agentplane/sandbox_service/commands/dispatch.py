"""Dispatch attempts: bounded, epoch-fenced delivery of submissions over the runner channel.

Each submission has one attempt, recorded on its own row; a submit opens or reopens it. Deliveries
and receipts name the command by its own ID, which the runner deduplicates on.

Contract: `agentplane/docs/runner_channel.md` § Dispatch attempts. Notifications are wakeups only;
every decision re-reads committed state.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner_channel import protocol_pb2 as channel_pb2
from agentplane.sandbox_service.commands import db
from agentplane.sandbox_service.commands.store import SubmissionConflictError, SubmissionState, record_receipt

# gazelle:include_dep @pypi//protobuf

DISPATCH_CHANNEL = "agentplane_runner_dispatch"
OUTCOME_CHANNEL = "agentplane_command_outcome"


class ReceiptMismatchError(ValueError):
    """A receipt names no command sent to this connection's Sandbox."""


@dataclass(frozen=True)
class CommandKey:
    session_id: UUID
    command_id: str


@dataclass(frozen=True)
class Delivery:
    runner_session_id: str
    command: command_pb2.Command
    deadline: datetime


@dataclass(frozen=True)
class Refused:
    refusal: channel_pb2.CommandRefused


type Outcome = event_log_pb2.EventEntry | Refused | None


# Command IDs may contain spaces, so each payload ends with the command ID.


def format_outcome(key: CommandKey) -> str:
    return f"{key.session_id} {key.command_id}"


def parse_outcome(payload: str) -> CommandKey:
    session_id, command_id = payload.split(" ", 1)
    return CommandKey(UUID(session_id), command_id)


def format_dispatch(sandbox_uid: UUID, key: CommandKey) -> str:
    return f"{sandbox_uid} {format_outcome(key)}"


def parse_dispatch(payload: str) -> tuple[UUID, CommandKey]:
    sandbox_uid, key = payload.split(" ", 1)
    return UUID(sandbox_uid), parse_outcome(key)


class DispatchStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def open(self, key: CommandKey, *, sandbox_uid: UUID, runner_session_id: str, deadline_s: float) -> None:
        """Open the submission's attempt until `deadline_s` from now.

        A submit while the attempt is open to the same Sandbox only extends its deadline. Otherwise,
        after a refusal or the deadline, the attempt starts afresh and may be sent again on the
        current connection.
        """
        submission = db.CommandSubmission
        deadline = func.now() + timedelta(seconds=deadline_s)
        open_here = and_(
            submission.attempt_sandbox_uid == sandbox_uid,
            submission.attempt_deadline > func.now(),
            submission.attempt_refusal.is_(None),
        )
        async with self._sessions.begin() as session:
            (
                await session.execute(
                    update(submission)
                    .where(submission.session_id == key.session_id, submission.command_id == key.command_id)
                    .values(
                        attempt_sandbox_uid=sandbox_uid,
                        attempt_runner_session_id=runner_session_id,
                        attempt_deadline=case(
                            (open_here, func.greatest(submission.attempt_deadline, deadline)), else_=deadline
                        ),
                        attempt_sent_epoch=case((open_here, submission.attempt_sent_epoch), else_=None),
                        attempt_refusal=None,
                    )
                    .returning(submission.command_id)
                )
            ).one()

    async def notify_dispatch(self, sandbox_uid: UUID, key: CommandKey) -> None:
        async with self._sessions.begin() as session:
            await session.execute(select(func.pg_notify(DISPATCH_CHANNEL, format_dispatch(sandbox_uid, key))))

    async def claim(self, key: CommandKey, *, sandbox_uid: UUID, epoch: int) -> Delivery | None:
        """Mark the attempt sent on `epoch`, or None if it is not this connection's to send now.

        Succeeds only before the deadline, for a still-pending unrefused submission not already sent on
        `epoch` or a later one. The caller checks that `epoch` is the Sandbox's current connection.
        """
        submission = db.CommandSubmission
        async with self._sessions.begin() as session:
            row = (
                await session.execute(
                    update(submission)
                    .where(
                        submission.session_id == key.session_id,
                        submission.command_id == key.command_id,
                        submission.attempt_sandbox_uid == sandbox_uid,
                        submission.attempt_deadline > func.now(),
                        submission.attempt_refusal.is_(None),
                        submission.attempt_sent_epoch.is_(None) | (submission.attempt_sent_epoch < epoch),
                        submission.state == SubmissionState.PENDING_ADMISSION,
                    )
                    .values(attempt_sent_epoch=epoch)
                    .returning(
                        submission.attempt_runner_session_id, submission.runner_command, submission.attempt_deadline
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return Delivery(
            runner_session_id=row.attempt_runner_session_id, command=row.runner_command, deadline=row.attempt_deadline
        )

    async def record(self, sandbox_uid: UUID, receipt: channel_pb2.CommandReceipt) -> None:
        """Record a runner's receipt on its submission, then wake the waiter.

        A late receipt still counts, and positive admission evidence never regresses to a refusal. A
        refusal sent before a resubmit reopened the attempt is indistinguishable from a fresh one: it
        ends the reopened wait, and the submission stays pending.
        """
        async with self._sessions.begin() as session:
            submission = await session.scalar(
                select(db.CommandSubmission)
                .where(
                    db.CommandSubmission.attempt_sandbox_uid == sandbox_uid,
                    db.CommandSubmission.attempt_runner_session_id == receipt.session_id,
                    db.CommandSubmission.command_id == receipt.command_id,
                )
                .with_for_update()
            )
            if submission is None:
                raise ReceiptMismatchError("receipt does not match a command sent to this Sandbox")
            match receipt.WhichOneof("outcome"):
                case "admitted":
                    entry = receipt.admitted
                    if (
                        not entry.event.HasField("command_admitted")
                        or entry.event.command_admitted.command.command_id != submission.command_id
                    ):
                        raise SubmissionConflictError("receipt admits a different command")
                    await record_receipt(session, submission.session_id, entry)
                case "refused":
                    if submission.state == SubmissionState.PENDING_ADMISSION:
                        refusal = channel_pb2.CommandRefused()
                        refusal.CopyFrom(receipt.refused)
                        submission.attempt_refusal = refusal
                case _:
                    raise ReceiptMismatchError("receipt has no outcome")
            key = CommandKey(submission.session_id, submission.command_id)
            await session.execute(select(func.pg_notify(OUTCOME_CHANNEL, format_outcome(key))))

    async def read_outcome(self, key: CommandKey) -> Outcome:
        """Admission from any source settles the attempt, including history ingestion's."""
        async with self._sessions() as session:
            admission, refusal = (
                (
                    await session.execute(
                        select(db.CommandSubmission.admission, db.CommandSubmission.attempt_refusal).where(
                            db.CommandSubmission.session_id == key.session_id,
                            db.CommandSubmission.command_id == key.command_id,
                        )
                    )
                )
                .tuples()
                .one()
            )
        if admission is not None:
            return admission
        return None if refusal is None else Refused(refusal)
