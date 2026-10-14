"""Dispatch rows: storing a command before it is acknowledged, and every transition after."""

from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from agentplane.postgres.wakeups import notify
from agentplane.proto import command_pb2, event_log_pb2, runner_pb2
from agentplane.session_dispatcher.channels import Channel, create_wakeups
from agentplane.session_dispatcher.db import Dispatch, SessionOpening
from agentplane.session_dispatcher.models import LIVE_STATES, DeliveryState, Operation, Reason, operation_of

# gazelle:include_dep @pypi//protobuf


class CommandConflictError(Exception):
    """The command ID was stored for other work."""


class QueueFullError(Exception):
    """The Session already holds as many queued commands as it may."""


class OpeningConflictError(Exception):
    """The Session ID was opened in another Sandbox or with another request."""


class DispatchNotFoundError(Exception):
    pass


class NotCancellableError(Exception):
    """Delivery already began, so the command's outcome is the runner's."""


class DispatchStore:
    def __init__(self, engine: AsyncEngine, *, queue_cap: int) -> None:
        self.queue_cap = queue_cap
        self.wakeups = create_wakeups(engine.url)
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def enqueue(
        self, *, session_id: str, sandbox_name: str, sandbox_uid: UUID, command: command_pb2.Command
    ) -> Dispatch:
        """Store `command` as queued; a stop first cancels what its Session has queued. A repeat of the same command returns the stored row."""
        operation = operation_of(command)
        async with self.sessions.begin() as session:
            # One writer per Session, so the cap and a stop's cancellation see every queued row.
            await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"session:{session_id}", 0))))
            existing = await session.scalar(select(Dispatch).where(Dispatch.command_id == command.command_id))
            if existing is not None:
                if (existing.session_id, existing.sandbox_name, existing.command) != (
                    session_id,
                    sandbox_name,
                    command,
                ):
                    raise CommandConflictError(command.command_id)
                return existing
            now = datetime.now(UTC)
            if operation is Operation.STOP_RUNNER_SESSION:
                await session.execute(
                    update(Dispatch)
                    .where(Dispatch.session_id == session_id, Dispatch.state == DeliveryState.QUEUED_IN_DISPATCHER)
                    .values(state=DeliveryState.CANCELLED_IN_DISPATCHER, reason=Reason.STOPPED, updated_at=now)
                )
            else:
                queued = await session.scalar(
                    select(func.count()).where(
                        Dispatch.session_id == session_id, Dispatch.state == DeliveryState.QUEUED_IN_DISPATCHER
                    )
                )
                if queued is not None and queued >= self.queue_cap:
                    raise QueueFullError(f"session {session_id} holds {queued} queued commands, the cap")
            row = Dispatch(
                command_id=command.command_id,
                session_id=session_id,
                sandbox_name=sandbox_name,
                sandbox_uid=sandbox_uid,
                operation=operation,
                command=command,
                state=DeliveryState.QUEUED_IN_DISPATCHER,
                reason=None,
                admission=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            await notify(session, Channel.DISPATCH)
            return row

    async def open_session(
        self,
        *,
        session_id: str,
        sandbox_name: str,
        sandbox_uid: UUID,
        spec: runner_pb2.SessionSpec,
        setup_script: str | None,
    ) -> SessionOpening:
        """Store a Session's opening as queued, or return the stored one when `session_id` was opened in
        the same Sandbox with the same request."""
        async with self.sessions.begin() as session:
            await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"session:{session_id}", 0))))
            existing = await session.get(SessionOpening, session_id)
            if existing is not None:
                if (existing.sandbox_name, existing.sandbox_uid, existing.spec, existing.setup_script) != (
                    sandbox_name,
                    sandbox_uid,
                    spec,
                    setup_script,
                ):
                    raise OpeningConflictError(session_id)
                return existing
            now = datetime.now(UTC)
            opening = SessionOpening(
                session_id=session_id,
                sandbox_name=sandbox_name,
                sandbox_uid=sandbox_uid,
                spec=spec,
                setup_script=setup_script,
                state=DeliveryState.QUEUED_IN_DISPATCHER,
                reason=None,
                attached=None,
                created_at=now,
                updated_at=now,
            )
            session.add(opening)
            await session.flush()
            await notify(session, Channel.DISPATCH)
            return opening

    async def get_opening(self, session_id: str) -> SessionOpening:
        async with self.sessions() as session:
            opening = await session.get(SessionOpening, session_id)
        if opening is None:
            raise DispatchNotFoundError(session_id)
        return opening

    async def opening_heads(self) -> list[SessionOpening]:
        """The openings still to deliver, oldest first."""
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(SessionOpening)
                        .where(SessionOpening.state == DeliveryState.QUEUED_IN_DISPATCHER)
                        .order_by(SessionOpening.created_at)
                    )
                ).all()
            )

    async def settle_opening(
        self,
        session_id: str,
        *,
        from_states: Collection[DeliveryState],
        to: DeliveryState,
        reason: str | None = None,
        attached: runner_pb2.Attached | None = None,
    ) -> SessionOpening | None:
        """Move an opening out of one of `from_states`; None when it is in none of them."""
        async with self.sessions.begin() as session:
            opening = await session.scalar(
                select(SessionOpening)
                .where(SessionOpening.session_id == session_id, SessionOpening.state.in_(from_states))
                .with_for_update()
            )
            if opening is None:
                return None
            opening.state, opening.reason, opening.attached = to, reason, attached
            opening.updated_at = datetime.now(UTC)
            await notify(session, Channel.DISPATCH)
            return opening

    async def note_opening_waiting(self, session_id: str, reason: Reason) -> None:
        """Say what a queued opening waits for; a repeat of the stored reason writes nothing."""
        async with self.sessions.begin() as session:
            noted = await session.execute(
                update(SessionOpening)
                .where(
                    SessionOpening.session_id == session_id,
                    SessionOpening.state == DeliveryState.QUEUED_IN_DISPATCHER,
                    SessionOpening.reason.is_distinct_from(reason),
                )
                .values(reason=reason, updated_at=datetime.now(UTC))
                .returning(SessionOpening.session_id)
            )
            if noted.first() is not None:
                await notify(session, Channel.DISPATCH)

    async def get_dispatch(self, command_id: str) -> Dispatch:
        async with self.sessions() as session:
            row = await session.scalar(select(Dispatch).where(Dispatch.command_id == command_id))
        if row is None:
            raise DispatchNotFoundError(command_id)
        return row

    async def list_dispatches(
        self, *, session_id: str | None = None, sandbox_name: str | None = None
    ) -> list[Dispatch]:
        """Oldest first; exactly one of the two scopes."""
        if (session_id is None) == (sandbox_name is None):
            raise ValueError("exactly one of session_id and sandbox_name is required")
        query = select(Dispatch).order_by(Dispatch.position)
        query = query.where(
            Dispatch.session_id == session_id if session_id is not None else Dispatch.sandbox_name == sandbox_name
        )
        async with self.sessions() as session:
            return list((await session.scalars(query)).all())

    async def cancel(self, command_id: str) -> Dispatch:
        row = await self.settle(
            command_id,
            from_states={DeliveryState.QUEUED_IN_DISPATCHER},
            to=DeliveryState.CANCELLED_IN_DISPATCHER,
            reason=Reason.SENDER,
        )
        if row is None:
            await self.get_dispatch(command_id)
            raise NotCancellableError(command_id)
        return row

    async def heads(self) -> list[Dispatch]:
        """Each Session's next command to deliver: its oldest queued one, once nothing earlier is queued
        or in flight. Oldest first."""
        earlier = aliased(Dispatch)
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(Dispatch)
                        .where(
                            Dispatch.state == DeliveryState.QUEUED_IN_DISPATCHER,
                            ~exists().where(
                                earlier.session_id == Dispatch.session_id,
                                earlier.position < Dispatch.position,
                                earlier.state.in_(LIVE_STATES),
                            ),
                            # A Session's commands wait for its opening.
                            ~exists().where(
                                SessionOpening.session_id == Dispatch.session_id, SessionOpening.state.in_(LIVE_STATES)
                            ),
                        )
                        .order_by(Dispatch.position)
                    )
                ).all()
            )

    async def settle(
        self,
        command_id: str,
        *,
        from_states: Collection[DeliveryState],
        to: DeliveryState,
        reason: str | None = None,
        admission: event_log_pb2.EventEntry | None = None,
    ) -> Dispatch | None:
        """Move a Dispatch out of one of `from_states`; None when it is in none of them, because the
        Sandbox's deletion or a cancel ended it first."""
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(Dispatch)
                .where(Dispatch.command_id == command_id, Dispatch.state.in_(from_states))
                .with_for_update()
            )
            if row is None:
                return None
            row.state, row.reason, row.admission, row.updated_at = to, reason, admission, datetime.now(UTC)
            await notify(session, Channel.DISPATCH)
            return row

    async def note_waiting(self, command_id: str, reason: Reason) -> None:
        """Say what a queued command waits for; a repeat of the stored reason writes nothing."""
        async with self.sessions.begin() as session:
            noted = await session.execute(
                update(Dispatch)
                .where(
                    Dispatch.command_id == command_id,
                    Dispatch.state == DeliveryState.QUEUED_IN_DISPATCHER,
                    Dispatch.reason.is_distinct_from(reason),
                )
                .values(reason=reason, updated_at=datetime.now(UTC))
                .returning(Dispatch.position)
            )
            if noted.first() is not None:
                await notify(session, Channel.DISPATCH)

    async def reset_delivering(self) -> None:
        """At start: a call in flight when the previous process ended is delivered again; the runner
        answers a redelivery with its admission."""
        async with self.sessions.begin() as session:
            await self._move(
                session,
                Dispatch.state == DeliveryState.DELIVERING_TO_RUNNER,
                to=DeliveryState.QUEUED_IN_DISPATCHER,
                reason=Reason.DISPATCHER_RESTARTED,
            )
            await session.execute(
                update(SessionOpening)
                .where(SessionOpening.state == DeliveryState.DELIVERING_TO_RUNNER)
                .values(
                    state=DeliveryState.QUEUED_IN_DISPATCHER,
                    reason=Reason.DISPATCHER_RESTARTED,
                    updated_at=datetime.now(UTC),
                )
            )

    async def end_sandbox(self, sandbox_uid: UUID) -> list[Dispatch]:
        """The Sandbox is gone, and its rows are kept only as long as it exists: what never reached its
        runner fails, what did or may have has an outcome nobody can read any more, and then every
        row of the Sandbox is deleted. Returns the rows this ended, as they ended, for the streams
        watching them."""
        async with self.sessions.begin() as session:
            for_sandbox = Dispatch.sandbox_uid == sandbox_uid
            ended = await self._move(
                session,
                for_sandbox,
                Dispatch.state == DeliveryState.QUEUED_IN_DISPATCHER,
                to=DeliveryState.FAILED,
                reason=Reason.SANDBOX_DELETED,
            )
            ended += await self._move(
                session,
                for_sandbox,
                (Dispatch.state == DeliveryState.DELIVERING_TO_RUNNER)
                | ((Dispatch.state == DeliveryState.RUNNER_ADMITTED) & (Dispatch.operation == Operation.SUBMIT_INPUT)),
                to=DeliveryState.UNKNOWN,
                reason=Reason.SANDBOX_DELETED,
            )
            rows = list(
                await session.scalars(select(Dispatch).where(Dispatch.position.in_(ended)).order_by(Dispatch.position))
            )
            deleted = await session.execute(delete(Dispatch).where(for_sandbox).returning(Dispatch.position))
            await session.execute(delete(SessionOpening).where(SessionOpening.sandbox_uid == sandbox_uid))
            if deleted.first() is not None:
                await notify(session, Channel.DISPATCH)
            session.expunge_all()
            return rows

    async def list_sandbox_uids(self) -> set[UUID]:
        """The Sandboxes that rows are stored for."""
        async with self.sessions() as session:
            return set(await session.scalars(select(Dispatch.sandbox_uid).distinct())) | set(
                await session.scalars(select(SessionOpening.sandbox_uid).distinct())
            )

    @staticmethod
    async def _move(session: AsyncSession, *where: ColumnElement[bool], to: DeliveryState, reason: Reason) -> list[int]:
        """The positions of the rows moved."""
        moved = await session.execute(
            update(Dispatch)
            .where(*where)
            .values(state=to, reason=reason, updated_at=datetime.now(UTC))
            .returning(Dispatch.position)
        )
        positions = list(moved.scalars())
        if positions:
            await notify(session, Channel.DISPATCH)
        return positions
