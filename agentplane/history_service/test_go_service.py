"""The Python client reads the built Go History Service, whose ingress a runner's journal is pushed to."""

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_bazel

from agentplane.history_service.testing.go_service import (
    WRITER,
    GoHistoryService,
    go_history_service,
    migrate_history_database,
)
from agentplane.proto import event_log_pb2, event_pb2
from agentplane.testing.electric_role import ensure_electric_role
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, control_plane, kubernetes_api
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate

# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg
# gazelle:include_dep @pypi//protobuf


@pytest.fixture(scope="session")
def go_history_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    ensure_electric_role(postgres_admin_url)
    with PostgresDatabaseTemplate(postgres_admin_url, "go_history_template") as template:
        migrate_history_database(template.url)
        yield template


@pytest.fixture(scope="session")
def kubeconfig() -> Iterator[Path]:
    """One control plane for the whole test target; each test enters its own client of it."""
    with control_plane() as path:
        yield path


@pytest.fixture
async def api(kubeconfig: Path) -> AsyncIterator[KubernetesApiServer]:
    async with kubernetes_api(kubeconfig) as server:
        yield server


@pytest.fixture
async def service(
    go_history_database_template: PostgresDatabaseTemplate,
    database_clones: DatabaseClones,
    api: KubernetesApiServer,
    tmp_path: Path,
    unused_tcp_port_factory: Callable[[], int],
) -> AsyncIterator[GoHistoryService]:
    database_url = database_clones.clone(go_history_database_template, drivername="postgresql+asyncpg")
    async with go_history_service(
        database_url, api=api, directory=tmp_path, port_factory=unused_tcp_port_factory
    ) as running:
        yield running


def journal_entry(cursor: int) -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(
        cursor=cursor,
        origin=event_log_pb2.EventOrigin(source_id="test-runner-journal", sequence=cursor),
        event=event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text=f"journal {cursor}")),
    )


async def test_a_pushed_journal_is_folded_and_read_back_through_the_python_client(service: GoHistoryService) -> None:
    session_id = uuid4()
    await service.push(session_id, [journal_entry(1), journal_entry(2), journal_entry(3)])
    summary = await service.folded(session_id, 3)
    assert summary.committed_cursor == 3
    async with service.client() as client:
        events = await client.read_session_events(str(session_id))
        sessions = await client.find_sessions(sandbox_name=WRITER.name)
    assert [entry.cursor for entry in events.entries] == [1, 2, 3]
    assert events.entries[2].event.harness_stderr.text == "journal 3"
    assert [found.session_id for found in sessions] == [str(session_id)]


if __name__ == "__main__":
    pytest_bazel.main()
