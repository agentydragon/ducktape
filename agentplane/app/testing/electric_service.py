"""Real Electric and logical PostgreSQL with the deployment's restricted database role."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.core.network import Network

# SQLAlchemy loads the driver selected by the URL scheme at runtime.
# gazelle:include_dep @pypi//psycopg
from agentplane.app.database_migrate import RUNNER
from agentplane.testing.electric_role import ensure_electric_role
from third_party.containers import electric_1_8_1, postgres_18, ryuk
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.postgres import PostgresDatabaseTemplate, migrated_database_template


@dataclass(frozen=True)
class BrowserPostgres:
    """Shard-local PostgreSQL server and migrated template for isolated browser cases."""

    network: Network
    admin_url: str
    template: PostgresDatabaseTemplate

    @contextmanager
    def replication_slot_cleanup(self, database: str) -> Iterator[None]:
        """Drop slots owned by a browser clone after the enclosed Electric process stops."""
        try:
            yield
        finally:
            self._drop_replication_slots(database)

    def _drop_replication_slots(self, database: str) -> None:
        engine = create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
        try:
            deadline = monotonic() + 30
            with engine.connect() as connection:
                while True:
                    slots = connection.execute(
                        text("SELECT slot_name, active FROM pg_replication_slots WHERE database = :database"),
                        {"database": database},
                    ).all()
                    if not any(active for _, active in slots):
                        for slot_name, _ in slots:
                            connection.execute(
                                text("SELECT pg_drop_replication_slot(:slot_name)"), {"slot_name": slot_name}
                            )
                        return
                    if monotonic() >= deadline:
                        raise TimeoutError(f"Electric replication slots stayed active for {database!r}: {slots!r}")
                    sleep(0.1)
        finally:
            engine.dispose()


@contextmanager
def browser_postgres_server() -> Iterator[BrowserPostgres]:
    """Start one logical-WAL PostgreSQL server and migrate its clone template once per shard."""
    _load_postgres_images()
    with Network() as network:
        postgres = _logical_postgres_container(network, test_name="conversation-postgres")
        with postgres:
            address = f"{postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}"
            admin_url = f"postgresql+psycopg://postgres:postgres@{address}/postgres"
            ensure_electric_role(admin_url, ready_timeout_s=60)

            with migrated_database_template(admin_url, "agentplane_browser_template", RUNNER) as template:
                yield BrowserPostgres(network, admin_url, template)


@asynccontextmanager
async def browser_electric_service(postgres: BrowserPostgres, database_name: str) -> AsyncIterator[ElectricService]:
    """Run a fresh Electric process against a fresh clone of the shard's migrated database."""
    with postgres.template.clone(database_name) as database_url:
        app_url = make_url(database_url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        electric_url = f"postgresql://electric:electric@postgres:5432/{database_name}?sslmode=disable"
        with postgres.replication_slot_cleanup(database_name):
            async with _electric_process(
                postgres.network, app_url, electric_url, test_name=f"conversation-electric-{database_name}"
            ) as service:
                yield service


class ElectricService:
    def __init__(self, database_url: str, container: LoggedContainer) -> None:
        self.database_url = database_url
        self._container = container

    @property
    def url(self) -> str:
        """Current host URL; Docker may choose a new published port after restart."""
        return f"http://{self._container.get_container_host_ip()}:{self._container.get_exposed_port(3000)}"

    async def stop(self) -> None:
        """Stop Electric while preserving its configured persistent state."""
        await asyncio.to_thread(self._container.get_wrapped_container().stop)

    async def start(self, *, timeout_s: float = 60) -> None:
        """Start a previously stopped Electric container and wait for its health endpoint."""
        await asyncio.to_thread(self._container.get_wrapped_container().start)
        await self.wait_ready(timeout_s=timeout_s)

    async def wait_ready(self, *, timeout_s: float = 60) -> None:
        """Wait for a running Electric process to pass its health check."""
        await _ready(self.url, timeout_s=timeout_s)

    async def logs(self) -> str:
        """Return logs from every run of the Electric container."""
        raw = await asyncio.to_thread(self._container.get_wrapped_container().logs)
        assert isinstance(raw, bytes)
        return raw.decode(errors="replace")

    async def state(self) -> dict[str, object]:
        """Return Docker's current process and published-port state for diagnostics."""
        container = self._container.get_wrapped_container()
        await asyncio.to_thread(container.reload)
        network = container.attrs.get("NetworkSettings", {})
        return {
            "status": container.status,
            "state": container.attrs.get("State"),
            "ports": network.get("Ports"),
            "url": self.url,
        }


async def _ready(url: str, *, timeout_s: float = 60) -> None:
    async with asyncio.timeout(timeout_s), httpx.AsyncClient(base_url=url, timeout=2) as client:
        while True:
            try:
                response = await client.get("/v1/health")
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)


@asynccontextmanager
async def _electric_process(
    network: Network,
    database_url: str,
    electric_database_url: str,
    *,
    test_name: str,
    electric_storage_dir: Path | None = None,
) -> AsyncIterator[ElectricService]:
    await asyncio.to_thread(load_oci_image, electric_1_8_1.IMAGE)
    electric = (
        LoggedContainer(electric_1_8_1.IMAGE.tag, test_name=test_name)
        .with_network(network)
        .with_exposed_ports(3000)
        .with_env("DATABASE_URL", electric_database_url)
        .with_env("ELECTRIC_INSECURE", "true")
        .with_env("ELECTRIC_STORAGE", "fast_file")
        .with_env("ELECTRIC_STORAGE_DIR", "/var/lib/electric")
        .with_env("ELECTRIC_PERSISTENT_STATE", "file")
        .with_env("ELECTRIC_MANUAL_TABLE_PUBLISHING", "true")
        .with_env("ELECTRIC_REPLICATION_STREAM_ID", "agentplane_conversation")
    )
    if electric_storage_dir is not None:
        await asyncio.to_thread(electric_storage_dir.mkdir, parents=True, exist_ok=True)
        # The upstream image runs as UID 1000; Kubernetes supplies this through the
        # deployment's fsGroup, while a Docker bind mount preserves the test process UID.
        await asyncio.to_thread(os.chmod, electric_storage_dir, 0o777)
        electric.with_volume_mapping(str(electric_storage_dir), "/var/lib/electric", mode="rw")
    with electric:
        service = ElectricService(database_url, electric)
        await service.wait_ready()
        yield service


def _load_postgres_images() -> None:
    for image in (ryuk.IMAGE, postgres_18.IMAGE):
        load_oci_image(image)


def _logical_postgres_container(
    network: Network, *, test_name: str, postgres_settings: tuple[str, ...] = ()
) -> LoggedContainer:
    command = " ".join(
        (
            "postgres",
            "-c wal_level=logical",
            "-c max_wal_senders=10",
            "-c max_replication_slots=10",
            *(f"-c {setting}" for setting in postgres_settings),
        )
    )
    return (
        LoggedContainer(postgres_18.IMAGE.tag, test_name=test_name)
        .with_network(network)
        .with_network_aliases("postgres")
        .with_exposed_ports(5432)
        .with_env("POSTGRES_USER", "postgres")
        .with_env("POSTGRES_PASSWORD", "postgres")
        .with_command(command)
    )


@asynccontextmanager
async def electric_service(
    *, postgres_settings: tuple[str, ...] = (), electric_storage_dir: Path | None = None
) -> AsyncIterator[ElectricService]:
    await asyncio.to_thread(_load_postgres_images)
    with Network() as network:
        postgres = _logical_postgres_container(
            network, test_name="conversation-postgres", postgres_settings=postgres_settings
        )
        with postgres:
            address = f"{postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}"
            admin_url = f"postgresql+psycopg://postgres:postgres@{address}/postgres"
            await asyncio.to_thread(ensure_electric_role, admin_url, ready_timeout_s=60)
            database_url = (
                make_url(admin_url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
            )
            await asyncio.to_thread(RUNNER.apply, database_url)
            async with _electric_process(
                network,
                database_url,
                "postgresql://electric:electric@postgres:5432/postgres?sslmode=disable",
                test_name="conversation-electric",
                electric_storage_dir=electric_storage_dir,
            ) as service:
                yield service
