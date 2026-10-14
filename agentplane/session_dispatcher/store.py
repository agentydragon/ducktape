"""Dispatch rows: storing a command before it is acknowledged, and every transition after."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from agentplane.postgres.wakeups import notify
from agentplane.proto import command_pb2, event_log_pb2, runner_pb2
from agentplane.session_dispatcher.channels import Channel, create_wakeups
from agentplane.session_dispatcher.db import Dispatch, SandboxOwnership, SessionOpening
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


@dataclass(frozen=True)
class SandboxOwner:
    """A Lease acquisition paired with the PostgreSQL generation that fences old holders."""

    sandbox_uid: UUID
    generation: int
    lease_uid: str
    lease_transitions: int
    holder_identity: str


class DispatchStore:
    def __init__(self, engine: AsyncEngine, *, queue_cap: int) -> None:
        self.queue_cap = queue_cap
        self.wakeups = create_wakeups(engine.url)
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def ownership_generation(self, sandbox_uid: UUID) -> int:
        """The DB generation to compare after acquiring and validating a fresh Kubernetes Lease."""
        async with self.sessions() as session:
            generation = await session.scalar(
                select(SandboxOwnership.generation).where(SandboxOwnership.sandbox_uid == sandbox_uid)
            )
        return 0 if generation is None else generation

    async def is_current_owner(self, owner: SandboxOwner) -> bool:
        async with self.sessions() as session:
            current = await session.get(SandboxOwnership, owner.sandbox_uid)
        return current is not None and (
            current.generation,
            current.lease_uid,
            current.lease_transitions,
            current.holder_identity,
        ) == (owner.generation, owner.lease_uid, owner.lease_transitions, owner.holder_identity)

    async def claim_sandbox_owner(
        self,
        sandbox_uid: UUID,
        *,
        lease_uid: str,
        lease_transitions: int,
        holder_identity: str,
        expected_generation: int,
    ) -> SandboxOwner | None:
        """Commit a freshly validated Lease proof only if the DB generation has not moved since it was read.

        The generation CAS is the ownership linearization point. A process paused after reading an
        old Lease cannot advance the DB row over a successor that has already committed.
        """
        async with self.sessions.begin() as session:
            current = await session.scalar(
                select(SandboxOwnership).where(SandboxOwnership.sandbox_uid == sandbox_uid).with_for_update()
            )
            generation = 0 if current is None else current.generation
            if generation != expected_generation:
                return None
            same_acquisition = current is not None and (
                current.lease_uid,
                current.lease_transitions,
                current.holder_identity,
            ) == (lease_uid, lease_transitions, holder_identity)
            if current is not None and not same_acquisition:
                # Resource versions are opaque and Lease UIDs can be recreated. Only the fresh
                # Kubernetes proof plus this expected-generation CAS orders a different object.
                if current.lease_uid == lease_uid and lease_transitions <= current.lease_transitions:
                    return None
                current.generation += 1
                current.lease_uid = lease_uid
                current.lease_transitions = lease_transitions
                current.holder_identity = holder_identity
                current.updated_at = datetime.now(UTC)
            elif current is None:
                current = SandboxOwnership(
                    sandbox_uid=sandbox_uid,
                    generation=1,
                    lease_uid=lease_uid,
                    lease_transitions=lease_transitions,
                    holder_identity=holder_identity,
                    updated_at=datetime.now(UTC),
                )
                session.add(current)
            if not same_acquisition:
                await notify(session, Channel.DISPATCH)
            return SandboxOwner(
                sandbox_uid=sandbox_uid,
                generation=current.generation,
                lease_uid=current.lease_uid,
                lease_transitions=current.lease_transitions,
                holder_identity=current.holder_identity,
            )

    async def enqueue(
        self,
        *,
        session_id: str,
        sandbox_name: str,
        sandbox_uid: UUID,
        command: command_pb2.Command,
        wake_eligible: bool = False,
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
                wake_eligible=wake_eligible and operation is not Operation.STOP_RUNNER_SESSION,
                wake_from_incarnation=None,
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

    async def note_waking(self, command_id: str, *, owner: SandboxOwner, incarnation: str | None) -> bool:
        """Persist the incarnation that must end before a queued command can be delivered."""
        value = "" if incarnation is None else incarnation
        async with self.sessions.begin() as session:
            sandbox_uid = await session.scalar(select(Dispatch.sandbox_uid).where(Dispatch.command_id == command_id))
            if sandbox_uid != owner.sandbox_uid or not await self._fence_owner(session, owner):
                return False
            updated = await session.execute(
                update(Dispatch)
                .where(
                    Dispatch.command_id == command_id,
                    Dispatch.state == DeliveryState.QUEUED_IN_DISPATCHER,
                    Dispatch.wake_from_incarnation.is_(None),
                )
                .values(wake_from_incarnation=value, updated_at=datetime.now(UTC))
                .returning(Dispatch.position)
            )
            if updated.first() is not None:
                await notify(session, Channel.DISPATCH)
                return True
            return bool(
                await session.scalar(
                    select(
                        exists().where(
                            Dispatch.command_id == command_id, Dispatch.state == DeliveryState.QUEUED_IN_DISPATCHER
                        )
                    )
                )
            )

    async def open_session(
        self,
        *,
        session_id: str,
        sandbox_name: str,
        sandbox_uid: UUID,
        spec: runner_pb2.SessionSpec,
        setup_script: str | None,
        wake_eligible: bool = False,
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
                wake_eligible=wake_eligible,
                wake_from_incarnation=None,
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

    async def note_opening_waking(
        self, session_id: str, *, owner: SandboxOwner, incarnation: str | None
    ) -> bool:
        """Persist the incarnation that must end before a queued Session opening can be delivered."""
        value = "" if incarnation is None else incarnation
        async with self.sessions.begin() as session:
            sandbox_uid = await session.scalar(
                select(SessionOpening.sandbox_uid).where(SessionOpening.session_id == session_id)
            )
            if sandbox_uid != owner.sandbox_uid or not await self._fence_owner(session, owner):
                return False
            updated = await session.execute(
                update(SessionOpening)
                .where(
                    SessionOpening.session_id == session_id,
                    SessionOpening.state == DeliveryState.QUEUED_IN_DISPATCHER,
                    SessionOpening.wake_from_incarnation.is_(None),
                )
                .values(wake_from_incarnation=value, updated_at=datetime.now(UTC))
                .returning(SessionOpening.session_id)
            )
            if updated.first() is not None:
                await notify(session, Channel.DISPATCH)
                return True
            return bool(
                await session.scalar(
                    select(
                        exists().where(
                            SessionOpening.session_id == session_id,
                            SessionOpening.state == DeliveryState.QUEUED_IN_DISPATCHER,
                        )
                    )
                )
            )

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
        owner: SandboxOwner,
        from_states: Collection[DeliveryState],
        to: DeliveryState,
        reason: str | None = None,
        attached: runner_pb2.Attached | None = None,
    ) -> SessionOpening | None:
        """Move an opening out of one of `from_states`; None when it is in none of them."""
        async with self.sessions.begin() as session:
            sandbox_uid = await session.scalar(
                select(SessionOpening.sandbox_uid).where(SessionOpening.session_id == session_id)
            )
            if sandbox_uid != owner.sandbox_uid or not await self._fence_owner(session, owner):
                return None
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

    async def note_opening_waiting(self, session_id: str, reason: Reason, *, owner: SandboxOwner) -> None:
        """Say what a queued opening waits for; a repeat of the stored reason writes nothing."""
        async with self.sessions.begin() as session:
            sandbox_uid = await session.scalar(
                select(SessionOpening.sandbox_uid).where(SessionOpening.session_id == session_id)
            )
            if sandbox_uid != owner.sandbox_uid or not await self._fence_owner(session, owner):
                return
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
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(Dispatch)
                .where(Dispatch.command_id == command_id, Dispatch.state == DeliveryState.QUEUED_IN_DISPATCHER)
                .with_for_update()
            )
            if row is not None:
                row.state, row.reason, row.updated_at = (
                    DeliveryState.CANCELLED_IN_DISPATCHER,
                    Reason.SENDER,
                    datetime.now(UTC),
                )
                await notify(session, Channel.DISPATCH)
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
        owner: SandboxOwner,
        from_states: Collection[DeliveryState],
        to: DeliveryState,
        reason: str | None = None,
        admission: event_log_pb2.EventEntry | None = None,
    ) -> Dispatch | None:
        """Move a Dispatch out of one of `from_states`; None when it is in none of them, because the
        Sandbox's deletion or a cancel ended it first."""
        async with self.sessions.begin() as session:
            sandbox_uid = await session.scalar(select(Dispatch.sandbox_uid).where(Dispatch.command_id == command_id))
            if sandbox_uid != owner.sandbox_uid or not await self._fence_owner(session, owner):
                return None
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

    async def note_waiting(self, command_id: str, reason: Reason, *, owner: SandboxOwner) -> None:
        """Say what a queued command waits for; a repeat of the stored reason writes nothing."""
        async with self.sessions.begin() as session:
            sandbox_uid = await session.scalar(select(Dispatch.sandbox_uid).where(Dispatch.command_id == command_id))
            if sandbox_uid != owner.sandbox_uid or not await self._fence_owner(session, owner):
                return
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

    async def recover_sandbox(self, owner: SandboxOwner) -> bool:
        """Requeue in-flight work for this Sandbox only, if this owner still holds the DB fence."""
        async with self.sessions.begin() as session:
            if not await self._fence_owner(session, owner):
                return False
            await self._move(
                session,
                Dispatch.sandbox_uid == owner.sandbox_uid,
                Dispatch.state == DeliveryState.DELIVERING_TO_RUNNER,
                to=DeliveryState.QUEUED_IN_DISPATCHER,
                reason=Reason.DISPATCHER_RESTARTED,
            )
            recovered = await session.execute(
                update(SessionOpening)
                .where(
                    SessionOpening.sandbox_uid == owner.sandbox_uid,
                    SessionOpening.state == DeliveryState.DELIVERING_TO_RUNNER,
                )
                .values(
                    state=DeliveryState.QUEUED_IN_DISPATCHER,
                    reason=Reason.DISPATCHER_RESTARTED,
                    updated_at=datetime.now(UTC),
                )
                .returning(SessionOpening.session_id)
            )
            if recovered.first() is not None:
                await notify(session, Channel.DISPATCH)
            return True

    async def _fence_owner(self, session: AsyncSession, owner: SandboxOwner) -> bool:
        """Lock and validate one Sandbox's ownership row before touching its delivery state."""
        current = await session.scalar(
            select(SandboxOwnership).where(SandboxOwnership.sandbox_uid == owner.sandbox_uid).with_for_update()
        )
        return current is not None and (
            current.generation,
            current.lease_uid,
            current.lease_transitions,
            current.holder_identity,
        ) == (owner.generation, owner.lease_uid, owner.lease_transitions, owner.holder_identity)

    async def end_sandbox(self, sandbox_uid: UUID) -> list[Dispatch]:
        """The Sandbox is gone, and its rows are kept only as long as it exists: what never reached its
        runner fails, what did or may have has an outcome nobody can read any more, and then every
        row of the Sandbox is deleted. Returns the rows this ended, as they ended, for the streams
        watching them."""
        async with self.sessions.begin() as session:
            # Match worker settlement's lock order: owner row, then delivery rows.
            await session.scalar(
                select(SandboxOwnership).where(SandboxOwnership.sandbox_uid == sandbox_uid).with_for_update()
            )
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
            await session.execute(delete(SandboxOwnership).where(SandboxOwnership.sandbox_uid == sandbox_uid))
            if deleted.first() is not None:
                await notify(session, Channel.DISPATCH)
            session.expunge_all()
            return rows

    async def list_sandbox_uids(self) -> set[UUID]:
        """The Sandboxes that rows are stored for."""
        async with self.sessions() as session:
            return (
                set(await session.scalars(select(Dispatch.sandbox_uid).distinct()))
                | set(await session.scalars(select(SessionOpening.sandbox_uid).distinct()))
                | set(await session.scalars(select(SandboxOwnership.sandbox_uid)))
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
