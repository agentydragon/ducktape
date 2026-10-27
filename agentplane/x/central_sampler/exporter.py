"""Replay committed sampler evidence into History without coupling sampling to its availability."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import grpc
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from agentplane.grpc.calls import bearer, dial
from agentplane.history_service import runner_ingress_pb2, runner_ingress_pb2_grpc
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import event_log_pb2
from agentplane.x.central_sampler.store import OUTBOX_CHANNEL, ExportCheckpoint, Session, Store

logger = logging.getLogger(__name__)

PAGE_SIZE = 16
PAGES_PER_TURN = 16
CONCURRENCY = 8
RETRY_SECONDS = 1


async def checkpoint(store: Store, session_id: str, destination: str) -> int | None:
    async with store.sessions() as db:
        cursor: int | None = await db.scalar(
            select(ExportCheckpoint.cursor).where(
                ExportCheckpoint.session_id == session_id, ExportCheckpoint.destination == destination
            )
        )
        return cursor


async def committed(store: Store, session_id: str, destination: str, cursor: int) -> None:
    async with store.sessions.begin() as db:
        await db.execute(
            insert(ExportCheckpoint)
            .values(session_id=session_id, destination=destination, cursor=cursor)
            .on_conflict_do_update(
                index_elements=[ExportCheckpoint.session_id, ExportCheckpoint.destination],
                set_={"cursor": func.greatest(ExportCheckpoint.cursor, cursor)},
            )
        )


async def export_session(store: Store, session: Session, target: str, token_file: Path) -> None:
    async with dial(target) as channel:
        await _export_session(store, session.id, target, token_file, channel)


async def _export_session(
    store: Store, session_id: str, target: str, token_file: Path, channel: grpc.aio.Channel
) -> None:
    """Drain a finite quantum, retaining each stream across bounded, acknowledged pages.

    Each stream covers its announced snapshot. New commits get a fresh announcement on the same
    channel so History eventually records the final model/setup/harness snapshot, too. Reconnects
    reconcile History's cursor before replaying immutable evidence, never provider/tool operations.
    """
    pages = 0
    for _ in range(PAGES_PER_TURN):
        session = await store.get_session(session_id)
        snapshot = store.describe(session)
        call = runner_ingress_pb2_grpc.RunnerIngressStub(channel).Push(metadata=await bearer(token_file), timeout=30)
        try:
            await call.write(
                runner_ingress_pb2.PushFrame(
                    sampler_announce=runner_ingress_pb2.SamplerAnnouncement(
                        session=snapshot.attached, executor=snapshot.executor
                    )
                )
            )
            first = await call.read()
            if not isinstance(first, runner_ingress_pb2.Acknowledgement):
                raise ConnectionError("History ended before its initial acknowledgement")
            current = await store.get_session(session_id)
            if first.sealed or first.cursor > current.cursor:
                raise ValueError("History acknowledgement conflicts with the sampler journal")
            after = first.cursor
            while after < session.cursor and pages < PAGES_PER_TURN:
                entries = await store.events(session_id, after, limit=min(PAGE_SIZE, session.cursor - after))
                if not entries:
                    raise ValueError("Sampler journal is missing committed evidence")
                through = entries[-1].cursor

                # Concurrent send/read prevents flow-control deadlock when History flushes before
                # the page finishes. Awaiting both bounds memory and applies transport backpressure.
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(_send_page(call, entries))
                    acknowledgement = tasks.create_task(
                        _acknowledge_page(store, session_id, target, call, after, through)
                    )
                after = acknowledgement.result()
                pages += 1
            # The initial acknowledgement precedes recording an already-covered announcement.
            # Half-close and await EOF so a no-entry refresh cannot cancel that snapshot commit.
            await call.done_writing()
            while (ack := await call.read()) is not grpc.aio.EOF:
                if not isinstance(ack, runner_ingress_pb2.Acknowledgement):
                    raise ConnectionError("History returned an invalid acknowledgement")
                if ack.sealed or not first.cursor <= ack.cursor <= after:
                    raise ValueError("History acknowledgement is outside the sent prefix")
            await committed(store, session_id, target, after)
        finally:
            call.cancel()
        current = await store.get_session(session_id)
        if pages == PAGES_PER_TURN or (after == current.cursor and session.cursor == current.cursor):
            return


async def _send_page(
    call: grpc.aio.StreamStreamCall[runner_ingress_pb2.PushFrame, runner_ingress_pb2.Acknowledgement],
    entries: list[event_log_pb2.EventEntry],
) -> None:
    for entry in entries:
        await call.write(runner_ingress_pb2.PushFrame(entry=entry))
    await call.write(runner_ingress_pb2.PushFrame(heartbeat=runner_ingress_pb2.Heartbeat()))


async def _acknowledge_page(
    store: Store,
    session_id: str,
    target: str,
    call: grpc.aio.StreamStreamCall[runner_ingress_pb2.PushFrame, runner_ingress_pb2.Acknowledgement],
    after: int,
    through: int,
) -> int:
    while after < through:
        ack = await call.read()
        if not isinstance(ack, runner_ingress_pb2.Acknowledgement):
            raise ConnectionError("History ended before acknowledging the batch")
        if ack.sealed or not after <= ack.cursor <= through:
            raise ValueError("History acknowledgement is outside the sent prefix")
        after = ack.cursor
        await committed(store, session_id, target, after)
    return after


async def pending_sessions(store: Store, target: str, *, exclude: set[str], after: str, limit: int) -> list[Session]:
    async with store.sessions() as db:
        return list(
            await db.scalars(
                select(Session)
                .outerjoin(
                    ExportCheckpoint,
                    and_(ExportCheckpoint.session_id == Session.id, ExportCheckpoint.destination == target),
                )
                .where(
                    or_(ExportCheckpoint.cursor.is_(None), ExportCheckpoint.cursor < Session.cursor),
                    Session.id.not_in(exclude),
                )
                # Rotate the bounded selection so a hot Session cannot starve later IDs.
                .order_by(case((Session.id > after, 0), else_=1), Session.id)
                .limit(limit)
            )
        )


async def _drain(store: Store, target: str, token_file: Path, wakeups: WakeupListener) -> None:
    changed = asyncio.Event()
    running: dict[asyncio.Task[Exception | None], str] = {}
    retries: dict[str, float] = {}
    after = ""
    loop = asyncio.get_running_loop()
    async with dial(target) as channel, asyncio.TaskGroup() as tasks:
        with wakeups.changes[OUTBOX_CHANNEL].subscribe(changed):
            waiter = tasks.create_task(changed.wait())
            try:
                while True:
                    changed.clear()
                    for task in tuple(running):
                        if task.done():
                            session_id = running.pop(task)
                            # Workers report transport failures without cancelling other exports.
                            if task.result() is not None:
                                retries[session_id] = loop.time() + RETRY_SECONDS
                    retries = {session_id: until for session_id, until in retries.items() if until > loop.time()}
                    if len(running) < CONCURRENCY:
                        sessions = await pending_sessions(
                            store,
                            target,
                            exclude=set(running.values()) | retries.keys(),
                            after=after,
                            limit=CONCURRENCY - len(running),
                        )
                        for session in sessions:
                            after = session.id
                            task = tasks.create_task(_export(store, session.id, target, token_file, channel))
                            running[task] = session.id
                    if waiter.done():
                        waiter = tasks.create_task(changed.wait())
                    timeout = min(retries.values()) - loop.time() if retries else None
                    # A disconnected LISTEN connection invalidates the query immediately; recovery
                    # re-registration wakes it again. There is no periodic scan while connected.
                    await asyncio.wait([waiter, *running], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                for task in running:
                    task.cancel()


async def _export(
    store: Store, session_id: str, target: str, token_file: Path, channel: grpc.aio.Channel
) -> Exception | None:
    try:
        await _export_session(store, session_id, target, token_file, channel)
    except Exception as error:
        logger.exception("History export failed; retaining outbox for replay", extra={"session_id": session_id})
        return error
    return None


async def run_exporter(store: Store, target: str, token_file: Path) -> None:
    wakeups = WakeupListener(
        store.database_url, channels=(OUTBOX_CHANNEL,), application_name="central-sampler-history-export"
    )
    while True:
        try:
            # LISTEN before querying: commits during startup and reconnect remain in the outbox.
            async with wakeups.listener.listen():
                await _drain(store, target, token_file, wakeups)
        except Exception:
            logger.exception("Cannot watch sampler History outbox")
            # Recovery backoff only; ordinary evidence delivery never waits on a timer.
            await asyncio.sleep(RETRY_SECONDS)


@asynccontextmanager
async def exporting(store: Store, target: str | None, token_file: Path | None) -> AsyncIterator[None]:
    if target is None:
        yield
        return
    if token_file is None:
        raise ValueError("History token file is required")
    task = asyncio.create_task(run_exporter(store, target, token_file))
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# Generated protobuf stubs require a direct typing dependency.
# gazelle:include_dep @pypi//protobuf

# gazelle:include_dep @pypi//grpcio
