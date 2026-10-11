"""Durable command admission; transport authorization remains the caller's responsibility.

Do not pass a caller-supplied identity here: the RPC boundary must authenticate the caller and
authorize the Session destination first. A submission whose receipt was lost stays pending until an
exact retry delivers it again; nothing settles it in the background.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Set
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.proto import command_pb2, event_log_pb2
from agentplane.runner_gateway.commands import db
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//protobuf


class CommandSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    command: command_pb2.Command

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if not 1 <= len(self.command.command_id) <= 128:
            raise ValueError("command ID must contain 1 to 128 characters")
        if self.command.WhichOneof("operation") is None:
            raise ValueError("a supported command operation is required")
        if self.command.ByteSize() > 1_048_576:
            raise ValueError("command exceeds the submission size limit")
        return self

    def copy_command(self) -> command_pb2.Command:
        """Keep the service envelope out of the runner and retain unknown protobuf fields."""
        result = command_pb2.Command()
        result.CopyFrom(self.command)
        return result

    def snapshot(self) -> CommandSubmission:
        # A frozen Pydantic model does not freeze its nested mutable protobuf. Call before
        # the first await, revalidating in case the caller mutated the command after creation.
        return CommandSubmission(command=self.copy_command())


class SubmissionConflictError(ValueError):
    pass


class SubmissionNotFoundError(LookupError):
    pass


class SubmissionRefusedError(ValueError):
    """Only a definitive non-admission refusal, never a timeout or transport failure."""


class SubmissionState(StrEnum):
    PENDING_ADMISSION = "pending_admission"
    ADMITTED = "admitted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Submission:
    command: command_pb2.Command
    accepted_at: datetime
    state: SubmissionState
    admission: event_log_pb2.EventEntry | None
    rejection: str | None

    @classmethod
    def from_row(cls, row: db.CommandSubmission) -> Submission:
        return cls(row.runner_command, row.accepted_at, SubmissionState(row.state), row.admission, row.rejection)

    def get_receipt(self) -> event_log_pb2.EventEntry:
        if self.admission is None:
            raise ValueError("submission has no admission receipt")
        result = event_log_pb2.EventEntry()
        result.CopyFrom(self.admission)
        return result


async def record_receipt(session: AsyncSession, session_id: UUID, receipt: event_log_pb2.EventEntry) -> None:
    """Record a runner's admission receipt on its submission, locking only that row.

    Commands from other producers have no submission row and need nothing. Repeating a receipt is
    idempotent.
    """
    command = receipt.event.command_admitted.command
    row = await session.scalar(
        select(db.CommandSubmission)
        .where(db.CommandSubmission.session_id == session_id, db.CommandSubmission.command_id == command.command_id)
        .with_for_update()
    )
    if row is None:
        return
    if not receipt.cursor or not receipt.origin.source_id or receipt.origin.sequence != receipt.cursor:
        raise SubmissionConflictError("invalid admission origin")
    if row.runner_command != command:
        raise SubmissionConflictError("admission differs from retained command")
    if row.admission is not None and row.admission != receipt:
        raise SubmissionConflictError("conflicting admission receipt")
    # Positive durable evidence wins over a racing refusal; never regress an admission.
    row.state = SubmissionState.ADMITTED
    admission = event_log_pb2.EventEntry()
    admission.CopyFrom(receipt)
    row.admission = admission
    row.rejection = None


class SubmissionStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def accept(self, session_id: UUID, request: CommandSubmission, *, caller: ServiceAccountRef) -> Submission:
        """Persist before runner contact. Destination authorization must precede this call.

        Caller provenance is server supplied, retained immutably, and checked on retries.
        """
        request = request.snapshot()
        command = request.command
        async with self._sessions.begin() as session:
            # The unique key arbitrates only identical command IDs, not all Session work.
            await session.execute(
                insert(db.CommandSubmission)
                .values(
                    session_id=session_id,
                    command_id=command.command_id,
                    runner_command=command,
                    caller_namespace=caller.namespace,
                    caller_name=caller.name,
                    state=SubmissionState.PENDING_ADMISSION,
                )
                .on_conflict_do_nothing(index_elements=["session_id", "command_id"])
            )
            row = await session.get(db.CommandSubmission, (session_id, command.command_id))
            if row is None:
                raise SubmissionNotFoundError(command.command_id)
            if row.runner_command != command or (row.caller_namespace, row.caller_name) != (
                caller.namespace,
                caller.name,
            ):
                raise SubmissionConflictError("command ID already belongs to a different submission")
            return Submission.from_row(row)

    async def read(self, session_id: UUID, command_id: str) -> Submission:
        """Internal lookup only; a status RPC must authorize Session access."""
        async with self._sessions() as session:
            row = await session.get(db.CommandSubmission, (session_id, command_id))
            if row is None:
                raise SubmissionNotFoundError(command_id)
            return Submission.from_row(row)

    async def list(
        self, session_id: UUID, *, states: Set[SubmissionState], after: tuple[datetime, str] | None, limit: int
    ) -> list[Submission]:
        """The Session's submissions in states (any, if empty) in acceptance order, after a
        (accepted_at, command_id) position. Internal lookup only, like `read`."""
        query = select(db.CommandSubmission).where(db.CommandSubmission.session_id == session_id)
        if states:
            query = query.where(db.CommandSubmission.state.in_(states))
        if after is not None:
            query = query.where(tuple_(db.CommandSubmission.accepted_at, db.CommandSubmission.command_id) > after)
        query = query.order_by(db.CommandSubmission.accepted_at, db.CommandSubmission.command_id).limit(limit)
        async with self._sessions() as session:
            return [Submission.from_row(row) for row in await session.scalars(query)]

    async def record_admission(self, session_id: UUID, entry: event_log_pb2.EventEntry) -> Submission:
        receipt = event_log_pb2.EventEntry()
        receipt.CopyFrom(entry)
        if not receipt.event.HasField("command_admitted"):
            raise SubmissionConflictError("expected a command admission receipt")
        command_id = receipt.event.command_admitted.command.command_id
        async with self._sessions.begin() as session:
            await record_receipt(session, session_id, receipt)
            row = await session.get(db.CommandSubmission, (session_id, command_id))
            if row is None:
                raise SubmissionNotFoundError(command_id)
            return Submission.from_row(row)

    async def record_rejection(self, session_id: UUID, command_id: str, reason: str) -> Submission:
        if not reason or len(reason) > 512:
            raise ValueError("a bounded, sanitized rejection reason is required")
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(db.CommandSubmission)
                .where(db.CommandSubmission.session_id == session_id, db.CommandSubmission.command_id == command_id)
                .with_for_update()
            )
            if row is None:
                raise SubmissionNotFoundError(command_id)
            if row.state == SubmissionState.PENDING_ADMISSION:
                row.state = SubmissionState.REJECTED
                row.rejection = reason
            return Submission.from_row(row)


async def submit_command(
    store: SubmissionStore,
    session_id: UUID,
    request: CommandSubmission,
    *,
    caller: ServiceAccountRef,
    dispatch: Callable[[command_pb2.Command], Awaitable[event_log_pb2.EventEntry]],
) -> event_log_pb2.EventEntry:
    """One caller-driven attempt, not a background queue or harness startup mechanism.

    The RPC must authorize first and provide a destination-bound dispatcher. Only proven
    non-admission may raise SubmissionRefusedError. Every other exception, cancellation included,
    leaves the submission pending until an exact retry delivers it again.
    """
    request = request.snapshot()
    current = await store.accept(session_id, request, caller=caller)
    if current.state == SubmissionState.ADMITTED:
        return current.get_receipt()
    if current.state == SubmissionState.REJECTED:
        raise SubmissionRefusedError(current.rejection)
    try:
        receipt = await dispatch(request.copy_command())
    except SubmissionRefusedError as error:
        current = await store.record_rejection(session_id, request.command.command_id, str(error))
        if current.state == SubmissionState.ADMITTED:
            return current.get_receipt()
        raise
    if receipt.event.command_admitted.command != request.command:
        raise SubmissionConflictError("dispatcher returned a receipt for different work")
    return (await store.record_admission(session_id, receipt)).get_receipt()
