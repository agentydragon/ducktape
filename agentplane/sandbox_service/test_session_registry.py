"""The Session registry in its own migrated database, mirrored into a separate history database."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.postgres import PostgresContainer

from agentplane.sandbox_service.commands.database_migrate import RUNNER
from agentplane.sandbox_service.commands.db import Session
from agentplane.sandbox_service.session_history.store import HistoryConflictError, HistoryNotFoundError, Store
from agentplane.sandbox_service.session_registry import OpenReservation, SessionRegistry
from util.testing.postgres import create_database_sync, force_drop_database_sync

# gazelle:include_dep //agentplane/sandbox_service/session_history:conftest

pytest_plugins = ("agentplane.sandbox_service.session_history.conftest",)


@pytest.fixture
def commands_url(postgres_container: PostgresContainer) -> Iterator[str]:
    admin = f"postgresql+psycopg://postgres:postgres@{postgres_container.get_container_host_ip()}:{postgres_container.get_exposed_port(5432)}/postgres"
    name = f"commands_{uuid4().hex}"
    url = (
        make_url(create_database_sync(admin, name))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )
    RUNNER.apply(url)
    yield url
    force_drop_database_sync(admin, name)


@pytest.fixture
async def commands_engine(commands_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(commands_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def history(engine: AsyncEngine) -> Store:
    return Store(engine)


@pytest.fixture
def registry(commands_engine: AsyncEngine, history: Store) -> SessionRegistry:
    return SessionRegistry(commands_engine, history)


async def test_reservation_is_stable_across_retries_and_replicas(
    registry: SessionRegistry, commands_engine: AsyncEngine, history: Store, engine: AsyncEngine
) -> None:
    replica = SessionRegistry(commands_engine, Store(engine))
    uid = uuid4()

    async def reserve(
        on: SessionRegistry,
        *,
        key: str = "open-1",
        caller_name: str = "app",
        sandbox_uid: UUID = uid,
        payload: bytes = b"spec",
        launch: str = "frozen",
    ) -> OpenReservation:
        return await on.reserve(
            caller_namespace="testing",
            caller_name=caller_name,
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=sandbox_uid,
            open_key=key,
            open_request=payload,
            launch_spec=lambda candidate: f"{launch} {candidate}".encode(),
        )

    first, second = await asyncio.gather(reserve(registry), reserve(replica))
    assert first == second
    assert first.launch_spec == f"frozen {first.session_id}".encode()
    assert await reserve(replica, launch="changed default should be ignored") == first
    assert await history.read(first.session_id) == (0, [])  # the history row ingesters append to
    assert (
        await registry.runner_id(first.session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid)
        == f"r-{first.session_id}"
    )
    assert (
        await replica.session_id(
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=uid,
            runner_session_id=f"r-{first.session_id}",
        )
        == first.session_id
    )
    with pytest.raises(HistoryNotFoundError):
        await registry.runner_id(
            first.session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uuid4()
        )
    assert await reserve(registry, key="open-2") != first
    assert await reserve(registry, caller_name="other") != first
    assert await reserve(registry, sandbox_uid=uuid4()) != first
    with pytest.raises(HistoryConflictError, match="different inputs"):
        await reserve(registry, payload=b"different caller request")
    assert await reserve(replica) == first  # a bad retry does not change the reservation
    with pytest.raises(ValueError, match="required"):
        await reserve(registry, key="")

    async def lookup(*, caller_name: str = "app", sandbox_uid: UUID = uid, key: str = "open-1") -> UUID | None:
        return await replica.lookup_open(
            caller_namespace="testing",
            caller_name=caller_name,
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=sandbox_uid,
            open_key=key,
        )

    assert await lookup() == first.session_id
    assert await lookup(caller_name="not-this-caller") is None
    assert await lookup(sandbox_uid=uuid4()) is None
    assert await lookup(key="absent") is None


async def test_open_keeps_imported_id_and_fences_duplicate_locator(registry: SessionRegistry, history: Store) -> None:
    session_id, uid = uuid4(), uuid4()
    for _ in range(2):
        await registry.open(
            session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="s-1"
        )
    with pytest.raises(HistoryConflictError, match="belongs to another"):
        await registry.open(
            uuid4(), sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="s-1"
        )
    # A nullable legacy UID must not permit another ID for the same legacy locator.
    await registry.open(
        uuid4(), sandbox_namespace="testing", sandbox_name="gone", sandbox_uid=None, runner_session_id="legacy"
    )
    with pytest.raises(HistoryConflictError, match="belongs to another"):
        await registry.open(
            uuid4(), sandbox_namespace="testing", sandbox_name="gone", sandbox_uid=None, runner_session_id="legacy"
        )
    assert await history.read(session_id) == (0, [])


async def test_import_copies_registry_rows_and_positions_continue_past_them(
    registry: SessionRegistry, history: Store
) -> None:
    uid = uuid4()
    reserved = Session(
        id=uuid4(),
        sandbox_namespace="testing",
        sandbox_name="worker",
        sandbox_uid=uid,
        runner_session_id="r-reserved",
        caller_namespace="testing",
        caller_name="app",
        open_key="reserved-before-the-move",
        open_request=b"original inputs",
        launch_spec=b"frozen launch",
        change_position=41,
    )
    legacy = Session(
        id=uuid4(),
        sandbox_namespace="testing",
        sandbox_name="gone",
        sandbox_uid=None,
        runner_session_id="legacy-runner",
        change_position=7,
    )
    for record in (reserved, legacy):
        await history.mirror(record)
    assert await registry.import_history() == 2
    assert await registry.import_history() == 0
    assert [
        (change.session_id, change.position) for change in await registry.read_changes(after_position=0, limit=10)
    ] == [(str(legacy.id), 7), (str(reserved.id), 41)]
    retried = await registry.reserve(
        caller_namespace="testing",
        caller_name="app",
        sandbox_namespace="testing",
        sandbox_name="worker",
        sandbox_uid=uid,
        open_key="reserved-before-the-move",
        open_request=b"original inputs",
        launch_spec=lambda _: b"today's defaults",
    )
    assert retried == OpenReservation(reserved.id, b"frozen launch")
    created = uuid4()
    await registry.open(
        created, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="r-created"
    )
    [change] = await registry.read_changes(after_position=41, limit=10)
    assert change.session_id == str(created)


async def test_import_refuses_a_row_that_differs_from_its_history_twin(
    registry: SessionRegistry, history: Store
) -> None:
    session_id = uuid4()
    await history.open(
        session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=None, runner_session_id="r-twin"
    )
    with pytest.raises(HistoryConflictError, match="differs"):
        await registry.open(
            session_id, sandbox_namespace="testing", sandbox_name="moved", sandbox_uid=None, runner_session_id="r-twin"
        )
    with pytest.raises(HistoryConflictError, match="differs"):
        await registry.import_history()


if __name__ == "__main__":
    pytest_bazel.main()
