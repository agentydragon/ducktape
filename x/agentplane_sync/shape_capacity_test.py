"""Measure Electric shape expiry and process memory with a bounded shape cap."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, cast

import asyncpg
import httpx
import pytest_bazel
from testcontainers.core.network import Network

from third_party.containers import electric_1_8, postgres_18, ryuk
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.agentplane_sync.projector import initialize_database

SMALL_ROW_COUNT = 1_000
LARGE_ROW_COUNT = 20_000
SHAPE_LIMIT = 2
SHAPE_COLUMNS = "conversation_id,row_key,anchor"


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def _messages(body: bytes) -> list[dict[str, Any]]:
    if not body:
        return []
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        result: list[dict[str, Any]] = []
        for line in body.splitlines():
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            result.extend(parsed if isinstance(parsed, list) else [parsed])
        return result
    if isinstance(decoded, list):
        return cast(list[dict[str, Any]], decoded)
    if isinstance(decoded, dict) and isinstance(decoded.get("data"), list):
        return cast(list[dict[str, Any]], decoded["data"])
    return [decoded]


async def _connect_postgres(dsn: str) -> asyncpg.Pool:
    last_error: BaseException | None = None
    for _ in range(120):
        try:
            connection = await asyncpg.connect(dsn, timeout=2)
            await connection.close()
            return await asyncpg.create_pool(dsn, min_size=1, max_size=8)
        except (asyncpg.PostgresError, OSError, TimeoutError) as error:
            last_error = error
            await asyncio.sleep(0.1)
    raise TimeoutError("PostgreSQL did not accept connections") from last_error


async def _wait_electric(url: str) -> dict[str, Any]:
    last_error: BaseException | None = None
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(180):
            try:
                response = await client.get(f"{url}/v1/health")
                if response.status_code == 200 and response.json().get("status") == "active":
                    return cast(dict[str, Any], response.json())
            except httpx.HTTPError as error:
                last_error = error
            await asyncio.sleep(0.1)
    raise TimeoutError("Electric 1.8.0 did not reach /v1/health active") from last_error


async def _seed_history(pool: asyncpg.Pool, conversation_id: str, count: int) -> None:
    await pool.execute(
        """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
           SELECT $1, $1 || '-item-' || i::text, 'item', i::bigint, i::bigint
           FROM generate_series(1, $2::int) AS series(i)""",
        conversation_id,
        count,
    )


def _sample_memory(electric: LoggedContainer) -> dict[str, Any]:
    container = electric.get_wrapped_container()
    stats = container.stats(stream=False)
    memory = stats.get("memory_stats", {})
    memory_stats = memory.get("stats", {})
    sample: dict[str, Any] = {
        "cgroupUsageBytes": memory.get("usage"),
        "cgroupLimitBytes": memory.get("limit"),
        "cgroupStats": memory_stats,
    }
    proc_status = container.exec_run(["cat", "/proc/1/status"])
    sample["pid1StatusExitCode"] = proc_status.exit_code
    if proc_status.exit_code == 0:
        status = proc_status.output.decode(errors="replace")
        sample["pid1VmRSSKiB"] = _proc_status_value(status, "VmRSS")
        sample["pid1VmHWMKiB"] = _proc_status_value(status, "VmHWM")
        cmdline = container.exec_run(["cat", "/proc/1/cmdline"])
        sample["pid1Command"] = cmdline.output.decode(errors="replace").replace("\x00", " ").strip()
    return sample


def _proc_status_value(status: str, key: str) -> int | None:
    match = re.search(rf"^{re.escape(key)}:\s+(\d+)\s+kB$", status, re.MULTILINE)
    return int(match.group(1)) if match else None


def _shape_params(conversation_id: str) -> dict[str, str]:
    return {
        "table": "sync_view_row",
        "where": f"conversation_id = '{conversation_id}'",
        "columns": SHAPE_COLUMNS,
        "queryable_columns": "conversation_id,row_key",
        "replica": "full",
        "log": "full",
        "live": "false",
    }


async def _read_snapshot(client: httpx.AsyncClient, shape_url: str, conversation_id: str) -> dict[str, Any]:
    base_params = _shape_params(conversation_id)
    offset = "-1"
    handle: str | None = None
    all_messages: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    for page_number in range(100):
        params = {**base_params, "offset": offset}
        if handle is not None:
            params["handle"] = handle
        response = await client.get(shape_url, params=params)
        assert response.status_code == 200, {
            "conversationId": conversation_id,
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": response.content[:2000].decode(errors="replace"),
        }
        if handle is None:
            handle = response.headers.get("electric-handle")
            assert handle, {"conversationId": conversation_id, "headers": dict(response.headers)}
        next_offset = response.headers.get("electric-offset")
        messages = _messages(response.content)
        all_messages.extend(messages)
        controls = [message.get("headers", {}).get("control") for message in messages]
        pages.append(
            {
                "page": page_number,
                "requestOffset": offset,
                "responseOffset": next_offset,
                "status": response.status_code,
                "rowCount": sum(1 for message in messages if message.get("headers", {}).get("operation")),
                "controlMessages": [control for control in controls if control],
                "responseBytes": len(response.content),
            }
        )
        if "up-to-date" in controls:
            assert next_offset, {"conversationId": conversation_id, "pages": pages}
            return {
                "conversationId": conversation_id,
                "handle": handle,
                "offset": next_offset,
                "rows": [message["value"] for message in all_messages if message.get("headers", {}).get("operation")],
                "pages": pages,
                "rowCount": sum(page["rowCount"] for page in pages),
                "responseBytes": sum(page["responseBytes"] for page in pages),
            }
        assert next_offset, {"conversationId": conversation_id, "offset": offset, "pages": pages}
        assert next_offset != offset, {
            "conversationId": conversation_id,
            "offset": offset,
            "nextOffset": next_offset,
            "pages": pages,
        }
        offset = next_offset
    raise AssertionError(
        {"conversationId": conversation_id, "message": "shape snapshot did not reach up-to-date", "pages": pages}
    )


async def _read_stale_handle(client: httpx.AsyncClient, shape_url: str, snapshot: dict[str, Any]) -> httpx.Response:
    params = {**_shape_params(snapshot["conversationId"]), "handle": snapshot["handle"], "offset": snapshot["offset"]}
    return await client.get(shape_url, params=params)


async def test_electric_shape_capacity_and_memory() -> None:
    outputs = undeclared_outputs_dir() / "electric-shape-capacity"
    outputs.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, Any] = {
        "electricImage": electric_1_8.IMAGE.tag,
        "configuredMaxShapes": SHAPE_LIMIT,
        "historySizes": {"capacity-small": SMALL_ROW_COUNT, "capacity-large": LARGE_ROW_COUNT},
        "samples": [],
    }
    for image in (ryuk.IMAGE, postgres_18.IMAGE, electric_1_8.IMAGE):
        load_oci_image(image)

    with Network() as network:
        postgres = (
            LoggedContainer(postgres_18.IMAGE.tag, test_name="electric-shape-capacity-postgres")
            .with_network(network)
            .with_network_aliases("postgres")
            .with_exposed_ports(5432)
            .with_env("POSTGRES_USER", "postgres")
            .with_env("POSTGRES_PASSWORD", "postgres")
            .with_command("postgres -c wal_level=logical -c max_wal_senders=10 -c max_replication_slots=10")
        )
        with postgres:
            pool: asyncpg.Pool | None = None
            try:
                dsn = (
                    f"postgresql://postgres:postgres@{postgres.get_container_host_ip()}"
                    f":{postgres.get_exposed_port(5432)}/postgres"
                )
                pool = await _connect_postgres(dsn)
                await initialize_database(pool)
                await _seed_history(pool, "capacity-small", SMALL_ROW_COUNT)
                await _seed_history(pool, "capacity-large", LARGE_ROW_COUNT)
                await _seed_history(pool, "capacity-extra", SMALL_ROW_COUNT)
                evidence["databaseRows"] = {
                    conversation_id: await pool.fetchval(
                        "SELECT count(*) FROM sync_view_row WHERE conversation_id=$1", conversation_id
                    )
                    for conversation_id in ("capacity-small", "capacity-large", "capacity-extra")
                }

                electric = (
                    LoggedContainer(electric_1_8.IMAGE.tag, test_name="electric-shape-capacity-electric")
                    .with_network(network)
                    .with_network_aliases("electric")
                    .with_exposed_ports(3000)
                    .with_env("DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/postgres?sslmode=disable")
                    .with_env("ELECTRIC_INSECURE", "true")
                    .with_env("ELECTRIC_MAX_SHAPES", str(SHAPE_LIMIT))
                )
                with electric:
                    electric_url = f"http://{electric.get_container_host_ip()}:{electric.get_exposed_port(3000)}"
                    evidence["health"] = await _wait_electric(electric_url)
                    evidence["samples"].append({"stage": "ready", **_sample_memory(electric)})
                    async with httpx.AsyncClient(timeout=60) as client:
                        snapshots: dict[str, dict[str, Any]] = {}
                        for conversation_id in ("capacity-small", "capacity-large", "capacity-extra"):
                            snapshot = await _read_snapshot(client, f"{electric_url}/v1/shape", conversation_id)
                            expected_count = evidence["databaseRows"][conversation_id]
                            assert snapshot["rowCount"] == expected_count, {
                                "conversationId": conversation_id,
                                "expectedRows": expected_count,
                                "snapshot": snapshot,
                            }
                            assert len({row["row_key"] for row in snapshot["rows"]}) == expected_count
                            snapshots[conversation_id] = snapshot
                            evidence.setdefault("snapshots", {})[conversation_id] = {
                                key: value for key, value in snapshot.items() if key != "rows"
                            }
                            evidence["samples"].append(
                                {"stage": f"after-{conversation_id}", **_sample_memory(electric)}
                            )
                            _write_evidence(outputs / "shape-capacity-evidence.json", evidence)

                        stale_snapshot = snapshots["capacity-small"]
                        evidence["expiryCheck"] = {
                            "waitSeconds": 75,
                            "reason": "Pinned Electric MAX_SHAPES expiry manager runs periodically (about once per minute)",
                            "oldestShape": stale_snapshot["conversationId"],
                            "oldHandle": stale_snapshot["handle"],
                        }
                        _write_evidence(outputs / "shape-capacity-evidence.json", evidence)
                        await asyncio.sleep(75)
                        stale_response = await _read_stale_handle(client, f"{electric_url}/v1/shape", stale_snapshot)
                        evidence["expiryCheck"].update(
                            {
                                "staleStatus": stale_response.status_code,
                                "staleHeaders": dict(stale_response.headers),
                                "staleBody": stale_response.content.decode(errors="replace"),
                            }
                        )
                        evidence["samples"].append({"stage": "after-expiry", **_sample_memory(electric)})
                        _write_evidence(outputs / "shape-capacity-evidence.json", evidence)
                        assert stale_response.status_code == 409, {
                            "configuredMaxShapes": SHAPE_LIMIT,
                            "snapshot": {key: value for key, value in stale_snapshot.items() if key != "rows"},
                            "response": evidence["expiryCheck"],
                        }

                        fresh_snapshot = await _read_snapshot(
                            client, f"{electric_url}/v1/shape", stale_snapshot["conversationId"]
                        )
                        assert fresh_snapshot["handle"] != stale_snapshot["handle"], {
                            "staleHandle": stale_snapshot["handle"],
                            "freshHandle": fresh_snapshot["handle"],
                        }
                        assert fresh_snapshot["rowCount"] == SMALL_ROW_COUNT
                        evidence["freshResnapshot"] = {
                            "oldHandle": stale_snapshot["handle"],
                            "newHandle": fresh_snapshot["handle"],
                            "rowCount": fresh_snapshot["rowCount"],
                            "responseBytes": fresh_snapshot["responseBytes"],
                            "pages": fresh_snapshot["pages"],
                        }
                        evidence["samples"].append({"stage": "after-fresh-resnapshot", **_sample_memory(electric)})
                        _write_evidence(outputs / "shape-capacity-evidence.json", evidence)
            finally:
                if pool is not None:
                    await pool.close()
                _write_evidence(outputs / "shape-capacity-evidence.json", evidence)


if __name__ == "__main__":
    pytest_bazel.main()
