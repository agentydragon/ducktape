"""The ingester against a scripted Sandbox Service and a real, migrated history database."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import override
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplane.history_service.conftest import ScriptedSandboxService, subscriber
from agentplane.history_service.ingester import HistoryIngester, claim, logger
from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.session_history.store import HistoryConflictError, Store

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/sandbox_service/session_history:conftest

pytest_plugins = ("agentplane.sandbox_service.session_history.conftest",)

NAMESPACE = "test-sandboxes"
SANDBOX = "test-followed-sandbox"
SANDBOX_UID = "7c1f3a52-0d7e-4e43-9a55-3f0c7d1f2b10"
OWNER = protocol_pb2.ServiceAccount(namespace=NAMESPACE, name="test-followed-sandbox-account")


def entry(cursor: int, text: str = "journal") -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(
        cursor=cursor,
        origin=event_log_pb2.EventOrigin(source_id="test-runner-journal", sequence=cursor),
        event=event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text=f"{text} {cursor}")),
    )


async def session(store: Store, sandbox_service: ScriptedSandboxService) -> UUID:
    """A Session the Sandbox Service created, as its row and its feed record."""
    session_id = uuid4()
    await store.open(
        session_id,
        sandbox_namespace=NAMESPACE,
        sandbox_name=SANDBOX,
        sandbox_uid=UUID(SANDBOX_UID),
        runner_session_id=f"r-{session_id}",
    )
    sandbox_service.add(str(session_id), sandbox=SANDBOX, sandbox_uid=SANDBOX_UID, owner=OWNER)
    return session_id


class Commits:
    """Wakes waiters after every ingester write, committed or refused."""

    def __init__(self) -> None:
        self._changed = asyncio.Event()

    def notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def until(self, holds: Callable[[], Awaitable[bool]]) -> None:
        async with asyncio.timeout(10):
            while True:
                changed = self._changed
                if await holds():
                    return
                await changed.wait()


class ObservedStore(Store):
    def __init__(self, engine: AsyncEngine, commits: Commits) -> None:
        super().__init__(engine)
        self.commits = commits

    @override
    async def append(self, session_id: UUID, entries: Sequence[event_log_pb2.EventEntry]) -> int:
        try:
            return await super().append(session_id, entries)
        finally:
            self.commits.notify()

    @override
    async def record_feed_state(self, session_id: UUID, feed: protocol_pb2.SessionFeedState) -> None:
        try:
            await super().record_feed_state(session_id, feed)
        finally:
            self.commits.notify()


@pytest.fixture
def commits() -> Commits:
    return Commits()


@pytest.fixture
def store(engine: AsyncEngine) -> Store:
    return Store(engine)


@pytest.fixture
def committed(store: Store, commits: Commits) -> Callable[[UUID, int], Awaitable[None]]:
    async def through(session_id: UUID, cursor: int) -> None:
        async def reached() -> bool:
            return (await store.read(session_id, limit=1))[0] >= cursor

        await commits.until(reached)

    return through


@pytest.fixture
async def target(sandbox_service: ScriptedSandboxService) -> AsyncIterator[str]:
    async with sandbox_service.serve() as address:
        yield address


@pytest.fixture
def replica(target: str, db_url: str, tmp_path: Path, commits: Commits) -> Callable[[str], asyncio.Task[None]]:
    """Start one History Service replica's ingester, with its own pool and bearer."""

    async def run(name: str) -> None:
        token_file = tmp_path / f"{name}-token"
        token_file.write_text(name)
        engine = create_async_engine(db_url)
        try:
            async with subscriber(target, token_file) as client:
                await HistoryIngester(
                    ObservedStore(engine, commits), engine, client, concurrency=2, retry_interval_s=0.05
                ).run()
        finally:
            await engine.dispose()

    return lambda name: asyncio.create_task(run(name))


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@asynccontextmanager
async def running(task: asyncio.Task[None]) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await stop(task)


Committed = Callable[[UUID, int], Awaitable[None]]
Replica = Callable[[str], asyncio.Task[None]]


async def test_discovered_session_is_copied_through_its_owners_destination(
    store: Store, sandbox_service: ScriptedSandboxService, replica: Replica, committed: Committed
) -> None:
    async with running(replica("test-replica")):
        # Created after the ingester started: only WatchSessions can name it.
        session_id = await session(store, sandbox_service)
        sandbox_service.append(str(session_id), entry(1), entry(2))
        await committed(session_id, 2)
        sandbox_service.append(str(session_id), entry(3))  # live, after the replayed prefix
        await committed(session_id, 3)
    first = await sandbox_service.follows.get()
    assert first.request.destination.sandbox == protocol_pb2.SandboxDestination(
        owner=OWNER, sandbox=SANDBOX, sandbox_uid=SANDBOX_UID
    )
    page = await store.read_page(session_id)
    assert list(page.entries) == [entry(1), entry(2), entry(3)]
    # Stored as the Sandbox Service ingester stores it, so either can write the same row.
    assert page.feed_state.attached.session_id == f"r-{session_id}"


async def test_restart_resumes_from_committed_cursor(
    store: Store, sandbox_service: ScriptedSandboxService, replica: Replica, committed: Committed
) -> None:
    session_id = await session(store, sandbox_service)
    sandbox_service.append(str(session_id), entry(1), entry(2))
    async with running(replica("test-replica-before")):
        await committed(session_id, 2)
    # The runner journal buffers what the History Service missed while down.
    sandbox_service.append(str(session_id), entry(3), entry(4))
    while not sandbox_service.follows.empty():
        sandbox_service.follows.get_nowait()
    async with running(replica("test-replica-after")):
        await committed(session_id, 4)
    resumed = await sandbox_service.follows.get()
    assert (resumed.bearer, resumed.request.follow.after_cursor) == ("Bearer test-replica-after", 2)
    assert list((await store.read_page(session_id)).entries) == [entry(cursor) for cursor in range(1, 5)]


async def test_replay_of_entries_another_writer_committed_is_accepted(
    store: Store, sandbox_service: ScriptedSandboxService, replica: Replica, committed: Committed
) -> None:
    session_id = await session(store, sandbox_service)
    sandbox_service.append(str(session_id), entry(1), entry(2), entry(3))
    sandbox_service.replay_gate = asyncio.Event()
    async with running(replica("test-replica")):
        await sandbox_service.follows.get()
        # E.g. the Sandbox Service ingester, during a handover, commits under the follow.
        await store.append(session_id, [entry(1), entry(2)])
        sandbox_service.replay_gate.set()
        await committed(session_id, 3)
    assert list((await store.read_page(session_id)).entries) == [entry(1), entry(2), entry(3)]


class ConflictLogged(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.seen = asyncio.Event()

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info and record.exc_info[0] is HistoryConflictError:
            self.seen.set()


async def test_conflicting_replay_is_rejected_loudly(
    store: Store, sandbox_service: ScriptedSandboxService, replica: Replica
) -> None:
    session_id = await session(store, sandbox_service)
    sandbox_service.append(str(session_id), entry(1), entry(2))
    sandbox_service.replay_gate = asyncio.Event()
    conflict = ConflictLogged()
    logger.addHandler(conflict)
    try:
        async with running(replica("test-replica")):
            await sandbox_service.follows.get()
            await store.append(session_id, [entry(1), entry(2, "diverged")])
            sandbox_service.replay_gate.set()
            async with asyncio.timeout(10):
                await conflict.seen.wait()
    finally:
        logger.removeHandler(conflict)
    assert list((await store.read_page(session_id)).entries) == [entry(1), entry(2, "diverged")]


async def test_claim_admits_one_holder_and_passes_on_release(db_url: str) -> None:
    session_id = uuid4()
    first, second = create_async_engine(db_url), create_async_engine(db_url)
    try:
        async with claim(first, session_id) as held:
            assert held
            async with claim(second, session_id) as contended:
                assert not contended
            async with claim(second, uuid4()) as other_session:
                assert other_session
        async with claim(second, session_id) as handed_over:
            assert handed_over
    finally:
        await first.dispose()
        await second.dispose()


async def test_one_replica_follows_a_session_until_it_stops(
    store: Store, sandbox_service: ScriptedSandboxService, replica: Replica, committed: Committed
) -> None:
    sandbox_service.lease_s = 30  # no planned renewal: a follow holds its claim throughout
    session_id = await session(store, sandbox_service)
    sandbox_service.append(str(session_id), entry(1))
    replicas = {name: replica(name) for name in ("test-replica-a", "test-replica-b")}
    try:
        await committed(session_id, 1)
        holder = (await sandbox_service.follows.get()).bearer.removeprefix("Bearer ")
        sandbox_service.append(str(session_id), entry(2))
        await committed(session_id, 2)
        assert sandbox_service.follows.empty()  # both watch the Session; the other is refused the claim
        await stop(replicas.pop(holder))
        sandbox_service.append(str(session_id), entry(3))
        await committed(session_id, 3)
        [successor] = replicas
        handover = await sandbox_service.follows.get()
        assert (handover.bearer, handover.request.follow.after_cursor) == (f"Bearer {successor}", 2)
    finally:
        for task in replicas.values():
            await stop(task)
    assert list((await store.read_page(session_id)).entries) == [entry(1), entry(2), entry(3)]


async def test_seal_is_recorded_as_the_incarnations_end(
    store: Store, sandbox_service: ScriptedSandboxService, replica: Replica, commits: Commits
) -> None:
    session_id = await session(store, sandbox_service)
    sandbox_service.append(str(session_id), entry(1), entry(2))
    sandbox_service.seal(str(session_id))

    async def sealed() -> bool:
        return (await store.read_page(session_id)).feed_state.HasField("sealed")

    async with running(replica("test-replica")):
        await commits.until(sealed)
    page = await store.read_page(session_id)
    assert page.last_cursor == 2
    assert page.feed_state.sealed.cursor == 2


if __name__ == "__main__":
    pytest_bazel.main()
