"""Dispatch rows: storing a command before it is acknowledged, and every transition after."""

from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from agentplane.postgres.wakeups import notify
from agentplane.proto import command_pb2, event_log_pb2
from agentplane.session_dispatcher.channels import Channel, create_wakeups
from agentplane.session_dispatcher.db import Dispatch
from agentplane.session_dispatcher.models import LIVE_STATES, DeliveryState, Operation, Reason, operation_of

# gazelle:include_dep @pypi//protobuf


class CommandConflictError(Exception):
    """The command ID was stored for other work."""


class QueueFullError(Exception):
    """The Session already holds as many queued commands as it may."""


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
        self, *, session_id: str, sandbox_name: str, sandbox_uid: UUID | None, command: command_pb2.Command
    ) -> Dispatch:
        """Store `command` as queued, or as failed for a Sandbox that does not exist; a stop first
        cancels what its Session has queued. A repeat of the same command returns the stored row."""
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
            if sandbox_uid is None:
                state, reason = DeliveryState.FAILED, Reason.SANDBOX_DELETED
            else:
                state, reason = DeliveryState.QUEUED_IN_DISPATCHER, None
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
                state=state,
                reason=reason,
                admission=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            await notify(session, Channel.DISPATCH)
            return row

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

    async def forget_sandbox(self, sandbox_uid: UUID) -> None:
        """The Sandbox is gone, and its rows are kept only as long as it exists."""
        async with self.sessions.begin() as session:
            deleted = await session.execute(
                delete(Dispatch).where(Dispatch.sandbox_uid == sandbox_uid).returning(Dispatch.position)
            )
            if deleted.first() is not None:
                await notify(session, Channel.DISPATCH)

    async def list_sandbox_uids(self) -> set[UUID]:
        """The Sandboxes that rows are stored for."""
        async with self.sessions() as session:
            return {uid for uid in await session.scalars(select(Dispatch.sandbox_uid).distinct()) if uid is not None}

    @staticmethod
    async def _move(session: AsyncSession, *where: ColumnElement[bool], to: DeliveryState, reason: Reason) -> None:
        moved = await session.execute(
            update(Dispatch)
            .where(*where)
            .values(state=to, reason=reason, updated_at=datetime.now(UTC))
            .returning(Dispatch.position)
        )
        if moved.first() is not None:
            await notify(session, Channel.DISPATCH)
