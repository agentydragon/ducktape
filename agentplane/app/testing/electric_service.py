"""Real Electric and logical PostgreSQL with the deployment's restricted database role."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

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
    url: str


async def _connect(dsn: str) -> asyncpg.Connection:
    async with asyncio.timeout(60):
        while True:
            try:
                return await asyncpg.connect(dsn)
            except OSError, asyncpg.CannotConnectNowError:
                await asyncio.sleep(0.1)


async def _ready(url: str) -> None:
    async with asyncio.timeout(60), httpx.AsyncClient(base_url=url, timeout=2) as client:
        while True:
            try:
                response = await client.get("/v1/health")
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)


@asynccontextmanager
async def electric_service() -> AsyncIterator[ElectricService]:
    for image in (ryuk.IMAGE, postgres_18.IMAGE, electric_1_8_1.IMAGE):
        await asyncio.to_thread(load_oci_image, image)
    with Network() as network:
        postgres = (
            LoggedContainer(postgres_18.IMAGE.tag, test_name="conversation-postgres")
            .with_network(network)
            .with_network_aliases("postgres")
            .with_exposed_ports(5432)
            .with_env("POSTGRES_USER", "postgres")
            .with_env("POSTGRES_PASSWORD", "postgres")
            .with_command("postgres -c wal_level=logical -c max_wal_senders=10 -c max_replication_slots=10")
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
                .with_env("ELECTRIC_PERSISTENT_STATE", "file")
                .with_env("ELECTRIC_MANUAL_TABLE_PUBLISHING", "true")
                .with_env("ELECTRIC_REPLICATION_STREAM_ID", "agentplane_conversation")
            )
            with electric:
                url = f"http://{electric.get_container_host_ip()}:{electric.get_exposed_port(3000)}"
                await _ready(url)
                yield ElectricService(database_url, url)
