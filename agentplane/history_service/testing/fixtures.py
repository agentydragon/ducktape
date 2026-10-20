"""Fixtures for tests that read or write through the built Go History Service."""

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path

import pytest

from agentplane.history_service.testing.go_service import (
    WRITER,
    GoHistoryService,
    go_history_service,
    migrated_history_template,
)
from agentplane.history_service.testing.history import History, scripted_history
from agentplane.testing.electric_role import ensure_electric_role
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, control_plane, kubernetes_api
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate


@pytest.fixture(scope="session")
def history_kubeconfig() -> Iterator[Path]:
    """One control plane for the whole test target; each test enters its own client of it."""
    with control_plane() as kubeconfig:
        yield kubeconfig


@pytest.fixture
async def history_api(history_kubeconfig: Path) -> AsyncIterator[KubernetesApiServer]:
    async with kubernetes_api(history_kubeconfig) as server:
        yield server


@pytest.fixture(scope="session")
def service_database_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    """The History schema as the Go migrator builds it, once."""
    ensure_electric_role(postgres_admin_url)
    with migrated_history_template(postgres_admin_url, "service_test_template") as template:
        yield template


@pytest.fixture
def service_history_db_url(service_database_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    """The database the History Service runs on; a module whose database is elsewhere overrides it."""
    return database_clones.clone(service_database_template, drivername="postgresql+asyncpg")


@pytest.fixture
def history_writer_namespaces() -> Sequence[str]:
    """The namespaces whose ServiceAccounts may push to the ingress."""
    return (WRITER.namespace,)


@pytest.fixture
async def history_service(
    service_history_db_url: str,
    history_api: KubernetesApiServer,
    history_writer_namespaces: Sequence[str],
    tmp_path: Path,
    unused_tcp_port_factory: Callable[[], int],
) -> AsyncIterator[GoHistoryService]:
    async with go_history_service(
        service_history_db_url,
        api=history_api,
        directory=tmp_path,
        port_factory=unused_tcp_port_factory,
        writer_namespaces=history_writer_namespaces,
    ) as service:
        yield service


@pytest.fixture
def history_on(
    history_api: KubernetesApiServer, tmp_path: Path, unused_tcp_port_factory: Callable[[], int]
) -> Callable[[str], AbstractAsyncContextManager[History]]:
    """The Go History Service and a scripted `History` on a database other than the default one."""

    def start(database_url: str) -> AbstractAsyncContextManager[History]:
        return scripted_history(database_url, api=history_api, directory=tmp_path, port_factory=unused_tcp_port_factory)

    return start
