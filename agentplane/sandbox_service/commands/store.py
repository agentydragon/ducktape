"""Durable command admission; transport authorization remains the caller's responsibility.

Staged for the Sandbox Service `SubmitCommand` handler, which lands next. Do not pass a caller-supplied identity here: the RPC boundary must
authenticate the caller and authorize the Session destination first.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.sandbox_service.commands import db
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
    state: SubmissionState
    admission: event_log_pb2.EventEntry | None
    rejection: str | None

    @classmethod
    def from_row(cls, row: db.CommandSubmission) -> Submission:
        return cls(SubmissionState(row.state), row.admission, row.rejection)

    def get_receipt(self) -> event_log_pb2.EventEntry:
        if self.admission is None:
            raise ValueError("submission has no admission receipt")
        result = event_log_pb2.EventEntry()
        result.CopyFrom(self.admission)
        return result


async def reconcile_admission(session: AsyncSession, session_id: UUID, entry: event_log_pb2.EventEntry) -> None:
    """Record one journal entry's admission evidence, locking only the matching submission.

    Commands from other producers have no submission row and need nothing. Repeating an entry is
    idempotent.
    """
    if not entry.event.HasField("command_admitted"):
        return
    command = entry.event.command_admitted.command
    row = await session.scalar(
        select(db.CommandSubmission)
        .where(db.CommandSubmission.session_id == session_id, db.CommandSubmission.command_id == command.command_id)
        .with_for_update()
    )
    if row is None:
        return
    if not entry.cursor or not entry.origin.source_id or entry.origin.sequence != entry.cursor:
        raise SubmissionConflictError("invalid admission origin")
    if row.runner_command != command:
        raise SubmissionConflictError("admission differs from retained command")
    if row.admission is not None and row.admission != entry:
        raise SubmissionConflictError("conflicting admission receipt")
    # Positive durable evidence wins over a racing refusal; never regress an admission.
    row.state = SubmissionState.ADMITTED
    receipt = event_log_pb2.EventEntry()
    receipt.CopyFrom(entry)
    row.admission = receipt
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

    async def record_admission(self, session_id: UUID, entry: event_log_pb2.EventEntry) -> Submission:
        """Record a direct receipt. It is not a contiguous journal prefix, so the cursor stays put."""
        receipt = event_log_pb2.EventEntry()
        receipt.CopyFrom(entry)
        if not receipt.event.HasField("command_admitted"):
            raise SubmissionConflictError("expected a command admission receipt")
        command_id = receipt.event.command_admitted.command.command_id
        async with self._sessions.begin() as session:
            progress = await session.get(db.AdmissionReconciliation, session_id)
            if progress is not None and progress.source_id != receipt.origin.source_id:
                raise SubmissionConflictError("admission source differs from the reconciled journal")
            await reconcile_admission(session, session_id, receipt)
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

    async def read_reconciled_through(self, session_id: UUID) -> int:
        """The last journal cursor reconciled; zero before the first."""
        async with self._sessions() as session:
            progress = await session.get(db.AdmissionReconciliation, session_id)
            return 0 if progress is None else progress.reconciled_through

    async def reconcile(self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> int:
        """Reconcile a contiguous run of journal entries and advance the cursor in one transaction.

        Entries at or before the cursor were already reconciled and are skipped, so a replayed batch
        is harmless; a gap or a different journal source is a conflict. Returns the new cursor.
        """
        async with self._sessions.begin() as session:
            if entries:
                await session.execute(
                    insert(db.AdmissionReconciliation)
                    .values(session_id=session_id, source_id=entries[0].origin.source_id, reconciled_through=0)
                    .on_conflict_do_nothing(index_elements=["session_id"])
                )
            # Serializes reconcilers of one Session; submissions lock only their own rows.
            progress = await session.scalar(
                select(db.AdmissionReconciliation)
                .where(db.AdmissionReconciliation.session_id == session_id)
                .with_for_update()
            )
            if progress is None:
                return 0
            for entry in entries:
                if entry.origin.source_id != progress.source_id:
                    raise SubmissionConflictError("entry source differs from the reconciled journal")
                if entry.cursor <= progress.reconciled_through:
                    continue
                if entry.cursor != progress.reconciled_through + 1:
                    raise SubmissionConflictError(
                        f"expected {progress.reconciled_through + 1}, received {entry.cursor}"
                    )
                await reconcile_admission(session, session_id, entry)
                progress.reconciled_through = entry.cursor
            return progress.reconciled_through


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
    leaves the durable state for reconciliation or an exact retry.
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
