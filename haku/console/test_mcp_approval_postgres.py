"""Postgres-backed MCP approval ledger tests."""

from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.oci import load_oci_image
from util.testing.postgres import force_drop_database_sync


@pytest.fixture(scope="session", autouse=True)
def _preload_postgres_images() -> None:
    load_oci_image(RYUK)
    load_oci_image(POSTGRES_18)


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"


@pytest.fixture
def db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> Generator[str]:
    db_name = re.sub(r"[^a-z0-9_]", "_", request.node.name.lower())[:45].rstrip("_") or "haku_console_test"
    admin_engine = create_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    yield postgres_admin_url.rsplit("/", 1)[0] + f"/{db_name}"

    force_drop_database_sync(postgres_admin_url, db_name)


def _catalog(tmp_path: Path) -> Path:
    path = tmp_path / "mcp_servers.yaml"
    path.write_text(
        """
servers:
  - id: smoke
    title: Smoke server
    server_url: mock://smoke
""",
        encoding="utf-8",
    )
    return path


def _csrf(client: Any) -> str:
    token = client.get("/api/capabilities/csrf").json()["csrf_token"]
    assert isinstance(token, str)
    return token


def test_postgres_store_runs_alembic_and_persists_typed_ledger(make_client, tmp_path: Path, db_url: str) -> None:
    with make_client(
        mcp_approval_catalog_path=_catalog(tmp_path),
        mcp_approval_database_url=SecretStr(db_url),
        csrf_secret=SecretStr("csrf"),
    ) as client:
        submitted = client.post(
            "/api/approvals/tool-calls",
            json={
                "server_id": "smoke",
                "tool_name": "echo",
                "client_request_id": "pg-req",
                "arguments": {"hello": "world"},
            },
        ).json()
        approved = client.post(
            f"/api/approvals/{submitted['approval_id']}/decision",
            headers={"X-CSRF-Token": _csrf(client)},
            json={"decision": "approve"},
        ).json()["tool_call"]
        replay = client.post(
            "/api/approvals/tool-calls",
            json={
                "server_id": "smoke",
                "tool_name": "echo",
                "client_request_id": "pg-req",
                "arguments": {"hello": "world"},
            },
        ).json()

    assert approved["status"] == "ok"
    assert approved["result"]["arguments"] == {"hello": "world"}
    assert replay["tool_call_id"] == submitted["tool_call_id"]

    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            columns = {
                row["column_name"]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_name = 'mcp_tool_calls'
                        """
                    )
                )
                .mappings()
                .all()
            }
            row = cast(
                dict[str, Any],
                conn.execute(
                    text(
                        """
                        SELECT server_id, tool_name, status, arguments_json, result_json
                        FROM mcp_tool_calls
                        WHERE tool_call_id = :tool_call_id
                        """
                    ),
                    {"tool_call_id": submitted["tool_call_id"]},
                )
                .mappings()
                .one(),
            )
    finally:
        engine.dispose()

    assert version == "0001"
    assert "record" not in columns
    assert {"server_id", "tool_name", "status", "arguments_json", "result_json"} <= columns
    assert row["server_id"] == "smoke"
    assert row["tool_name"] == "echo"
    assert row["status"] == "ok"
    assert row["arguments_json"] == {"hello": "world"}
    assert row["result_json"]["mock"] is True


if __name__ == "__main__":
    pytest_bazel.main()
