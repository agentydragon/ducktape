"""Real Electric over a logical PostgreSQL history database, with the deployment's restricted role."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from tenacity import AsyncRetrying, Retrying, retry_if_exception_type, stop_after_delay, wait_fixed
from testcontainers.core import testcontainers_config
from testcontainers.core.network import Network

# SQLAlchemy loads the driver selected by the URL scheme at runtime.
# gazelle:include_dep @pypi//psycopg
from agentplane.history_service.testing.go_service import migrate_history_database, migrated_history_template
from agentplane.testing.electric_role import ensure_electric_role
from devinfra.oci.oci import load_oci_image
from third_party.containers import electric_1_8_1, postgres_18, ryuk
from util.db_url import render_postgresql_url
from util.testing.container_logs import LoggedContainer
from util.testing.postgres import PostgresDatabaseTemplate


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
            with engine.connect() as connection:
                for attempt in Retrying(
                    stop=stop_after_delay(30),
                    wait=wait_fixed(0.1),
                    retry=retry_if_exception_type(AssertionError),
                    reraise=True,
                ):
                    with attempt:
                        slots = connection.execute(
                            text("SELECT slot_name, active FROM pg_replication_slots WHERE database = :database"),
                            {"database": database},
                        ).all()
                        assert not any(active for _, active in slots), (
                            f"Electric replication slots stayed active for {database!r}: {slots!r}"
                        )
                for slot_name, _ in slots:
                    connection.execute(text("SELECT pg_drop_replication_slot(:slot_name)"), {"slot_name": slot_name})
        finally:
            engine.dispose()


@contextmanager
def browser_postgres_server() -> Iterator[BrowserPostgres]:
    """Start one logical-WAL PostgreSQL server and migrate its clone template once per shard."""
    postgres_image = _load_postgres_images()
    with Network() as network:
        postgres = _logical_postgres_container(network, postgres_image=postgres_image, test_name="history-postgres")
        with postgres:
            address = f"{postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}"
            admin_url = f"postgresql+psycopg://postgres:postgres@{address}/postgres"
            ensure_electric_role(admin_url, ready_timeout_s=60)

            with migrated_history_template(admin_url, "agentplane_browser_history_template") as template:
                yield BrowserPostgres(network, admin_url, template)


@asynccontextmanager
async def browser_electric_service(postgres: BrowserPostgres, database_name: str) -> AsyncIterator[ElectricService]:
    """Run a fresh Electric process against a fresh clone of the shard's migrated database."""
    with postgres.template.clone(database_name) as database_url:
        history_url = render_postgresql_url(database_url, "postgresql+asyncpg")
        electric_url = f"postgresql://electric:electric@postgres:5432/{database_name}?sslmode=disable"
        with postgres.replication_slot_cleanup(database_name):
            async with _electric_process(
                postgres.network, history_url, electric_url, test_name=f"history-electric-{database_name}"
            ) as service:
                yield service


class ElectricService:
    def __init__(self, database_url: str, container: LoggedContainer) -> None:
        """`database_url` is the history database, whose fold Electric replicates."""
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

    async def memory_snapshot(self) -> dict[str, object]:
        """Docker cgroup accounting and host `ps` RSS, kept separate from file cache."""
        container = self._container.get_wrapped_container()
        stats, processes = await asyncio.gather(
            asyncio.to_thread(container.stats, stream=False),
            asyncio.to_thread(container.top, ps_args="-eo pid,rss,comm"),
        )
        return {"docker_memory_stats": stats["memory_stats"], "processes": processes}


async def _ready(url: str, *, timeout_s: float = 60) -> None:
    async with httpx.AsyncClient(base_url=url, timeout=2) as client:
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(timeout_s),
            wait=wait_fixed(0.1),
            retry=retry_if_exception_type((httpx.TransportError, AssertionError)),
            reraise=True,
        ):
            with attempt:
                response = await client.get("/v1/health")
                assert response.status_code == 200, f"Electric is not healthy yet: {response.status_code=}"


@asynccontextmanager
async def _electric_process(
    network: Network,
    database_url: str,
    electric_database_url: str,
    *,
    test_name: str,
    electric_storage_dir: Path | None = None,
) -> AsyncIterator[ElectricService]:
    electric_image = await asyncio.to_thread(load_oci_image, electric_1_8_1.LAYOUT_RLOCATION)
    electric = (
        LoggedContainer(electric_image, test_name=test_name)
        .with_network(network)
        .with_exposed_ports(3000)
        .with_env("DATABASE_URL", electric_database_url)
        .with_env("ELECTRIC_INSECURE", "true")
        .with_env("ELECTRIC_STORAGE", "fast_file")
        .with_env("ELECTRIC_STORAGE_DIR", "/var/lib/electric")
        .with_env("ELECTRIC_PERSISTENT_STATE", "file")
        .with_env("ELECTRIC_MANUAL_TABLE_PUBLISHING", "true")
        .with_env("ELECTRIC_REPLICATION_STREAM_ID", "agentplane_history")
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


def _load_postgres_images() -> str:
    load_oci_image(ryuk.LAYOUT_RLOCATION, image_ref=testcontainers_config.ryuk_image)
    return load_oci_image(postgres_18.LAYOUT_RLOCATION)


def _logical_postgres_container(
    network: Network, *, postgres_image: str, test_name: str, postgres_settings: tuple[str, ...] = ()
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
        LoggedContainer(postgres_image, test_name=test_name)
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
    postgres_image = await asyncio.to_thread(_load_postgres_images)
    with Network() as network:
        postgres = _logical_postgres_container(
            network, postgres_image=postgres_image, test_name="history-postgres", postgres_settings=postgres_settings
        )
        with postgres:
            address = f"{postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}"
            admin_url = f"postgresql+psycopg://postgres:postgres@{address}/postgres"
            await asyncio.to_thread(ensure_electric_role, admin_url, ready_timeout_s=60)
            database_url = render_postgresql_url(admin_url, "postgresql+asyncpg")
            await asyncio.to_thread(migrate_history_database, database_url)
            async with _electric_process(
                network,
                database_url,
                "postgresql://electric:electric@postgres:5432/postgres?sslmode=disable",
                test_name="history-electric",
                electric_storage_dir=electric_storage_dir,
            ) as service:
                yield service
