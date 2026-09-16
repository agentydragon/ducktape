"""Applies aiquota's ClickHouse schema (schema.sql) over the HTTP interface.

Run as the `migrate` init container ahead of the API server. Every statement in
schema.sql is `CREATE`/`ALTER ... IF NOT EXISTS`, so re-running it on every
rollout is safe; Kubernetes retries a failing init container until it succeeds,
so there is no retry loop here.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from util.bazel.runfiles import find_path

logger = logging.getLogger(__name__)

_SCHEMA_RUNFILES_PATH = "_main/aiquota/schema.sql"


class MigrateSettings(BaseSettings):
    """Runtime configuration sourced from the `migrate` init container environment."""

    model_config = SettingsConfigDict(env_prefix="AIQUOTA_", extra="ignore")

    clickhouse_url: str
    clickhouse_username: str = "aiquota_ingest"
    clickhouse_password: SecretStr
    clickhouse_database: str = "aiquota"


def statements(schema_sql: str) -> list[str]:
    """Split schema.sql on `;` into individual DDL statements.

    The ClickHouse HTTP interface executes one statement per request; none of
    aiquota's DDL nests a `;` inside a statement.
    """

    return [statement.strip() for statement in schema_sql.split(";") if statement.strip()]


async def apply_schema(
    *,
    url: str,
    username: str,
    password: str,
    database: str,
    schema_sql: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    async with httpx.AsyncClient(auth=(username, password), timeout=30.0, transport=transport) as client:
        for statement in statements(schema_sql):
            response = await client.post(url, params={"database": database}, content=statement.encode())
            response.raise_for_status()


def _read_schema_sql() -> str:
    path = find_path(_SCHEMA_RUNFILES_PATH)
    if path is None:
        raise FileNotFoundError(f"{_SCHEMA_RUNFILES_PATH} not found in runfiles")
    return path.read_text()


async def _async_main() -> None:
    settings = MigrateSettings()
    await apply_schema(
        url=settings.clickhouse_url,
        username=settings.clickhouse_username,
        password=settings.clickhouse_password.get_secret_value(),
        database=settings.clickhouse_database,
        schema_sql=_read_schema_sql(),
    )
    logger.info("aiquota ClickHouse schema applied")


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
