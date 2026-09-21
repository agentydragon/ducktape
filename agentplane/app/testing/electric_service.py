"""Real Electric and logical PostgreSQL with the deployment's restricted database role."""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import httpx
from testcontainers.core.network import Network

from agentplane.app.database_migrate import RUNNER
from third_party.containers import electric_1_8_1, postgres_18, ryuk
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer


@dataclass(frozen=True)
class ElectricService:
    database_url: str
    _url: Callable[[], str]
    _stop: Callable[[], Awaitable[None]]
    _start: Callable[[float], Awaitable[None]]
    _wait_ready: Callable[[float], Awaitable[None]]
    _logs: Callable[[], Awaitable[str]]
    _state: Callable[[], Awaitable[dict[str, object]]]

    async def stop(self) -> None:
        """Stop Electric while preserving its configured persistent state."""
        await self._stop()

    @property
    def url(self) -> str:
        """Current host URL; Docker may choose a new published port after restart."""
        return self._url()

    async def start(self, *, timeout_s: float = 60) -> None:
        """Start a previously stopped Electric container and wait for its health endpoint."""
        await self._start(timeout_s)

    async def logs(self) -> str:
        """Return logs from every run of the Electric container."""
        return await self._logs()

    async def wait_ready(self, *, timeout_s: float = 60) -> None:
        """Wait for a running Electric process to pass its health check."""
        await self._wait_ready(timeout_s)

    async def state(self) -> dict[str, object]:
        """Return Docker's current process and published-port state for diagnostics."""
        return await self._state()


async def _connect(dsn: str) -> asyncpg.Connection:
    async with asyncio.timeout(60):
        while True:
            try:
                return await asyncpg.connect(dsn)
            except OSError, asyncpg.CannotConnectNowError:
                await asyncio.sleep(0.1)


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
async def electric_service(
    *, postgres_settings: tuple[str, ...] = (), electric_storage_dir: Path | None = None
) -> AsyncIterator[ElectricService]:
    for image in (ryuk.IMAGE, postgres_18.IMAGE, electric_1_8_1.IMAGE):
        await asyncio.to_thread(load_oci_image, image)
    with Network() as network:
        postgres_command = " ".join(
            (
                "postgres",
                "-c wal_level=logical",
                "-c max_wal_senders=10",
                "-c max_replication_slots=10",
                *(f"-c {setting}" for setting in postgres_settings),
            )
        )
        postgres = (
            LoggedContainer(postgres_18.IMAGE.tag, test_name="conversation-postgres")
            .with_network(network)
            .with_network_aliases("postgres")
            .with_exposed_ports(5432)
            .with_env("POSTGRES_USER", "postgres")
            .with_env("POSTGRES_PASSWORD", "postgres")
            .with_command(postgres_command)
        )
        with postgres:
            address = f"{postgres.get_container_host_ip()}:{postgres.get_exposed_port(5432)}"
            connection = await _connect(f"postgresql://postgres:postgres@{address}/postgres")
            try:
                # The CNPG managed role exists before the application's migration Job runs.
                # Grants, replica identity and publication must come from the real migration.
                await connection.execute("CREATE ROLE electric LOGIN REPLICATION PASSWORD 'electric'")
            finally:
                await connection.close()
            database_url = f"postgresql+asyncpg://postgres:postgres@{address}/postgres"
            await asyncio.to_thread(RUNNER.apply, database_url)
            electric = (
                LoggedContainer(electric_1_8_1.IMAGE.tag, test_name="conversation-electric")
                .with_network(network)
                .with_exposed_ports(3000)
                .with_env("DATABASE_URL", "postgresql://electric:electric@postgres:5432/postgres?sslmode=disable")
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

                def url() -> str:
                    return f"http://{electric.get_container_host_ip()}:{electric.get_exposed_port(3000)}"

                await _ready(url())

                async def stop() -> None:
                    await asyncio.to_thread(electric.get_wrapped_container().stop)

                async def start(timeout_s: float) -> None:
                    await asyncio.to_thread(electric.get_wrapped_container().start)
                    await _ready(url(), timeout_s=timeout_s)

                async def logs() -> str:
                    raw = await asyncio.to_thread(electric.get_wrapped_container().logs)
                    assert isinstance(raw, bytes)
                    return raw.decode(errors="replace")

                async def wait_ready(timeout_s: float) -> None:
                    await _ready(url(), timeout_s=timeout_s)

                async def state() -> dict[str, object]:
                    container = electric.get_wrapped_container()
                    await asyncio.to_thread(container.reload)
                    network = container.attrs.get("NetworkSettings", {})
                    return {
                        "status": container.status,
                        "state": container.attrs.get("State"),
                        "ports": network.get("Ports"),
                        "url": url(),
                    }

                yield ElectricService(database_url, url, stop, start, wait_ready, logs, state)
