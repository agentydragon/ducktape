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

from third_party.containers import electric_1_8_1, postgres_18, ryuk
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.agentplane_sync.projector import initialize_database

SMALL_ROW_COUNT = 1_000
LARGE_ROW_COUNT = 20_000
SHAPE_LIMIT = 2
SHAPE_COLUMNS = "conversation_id,row_key,anchor"
ACTIVE_SHAPE_COLUMNS = f"{SHAPE_COLUMNS},revision"
BOUNDED_HISTORY_CONVERSATION = "bounded-history"
BOUNDED_HISTORY_TAIL_ANCHOR = 9_007_199_254_740_993
BOUNDED_HISTORY_TAIL_ROWS = 30
BOUNDED_HISTORY_BATCH_ROWS = 10_000


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
    raise TimeoutError("Electric 1.8.1 did not reach /v1/health active") from last_error


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


def _shape_params(
    conversation_id: str, *, where_clause: str | None = None, columns: str = SHAPE_COLUMNS, live: str = "false"
) -> dict[str, str]:
    return {
        "table": "sync_view_row",
        "where": where_clause or f"conversation_id = '{conversation_id}'",
        "columns": columns,
        "replica": "full",
        "log": "full",
        "live": live,
    }


async def _read_snapshot(
    client: httpx.AsyncClient,
    shape_url: str,
    conversation_id: str,
    *,
    where_clause: str | None = None,
    columns: str = SHAPE_COLUMNS,
) -> dict[str, Any]:
    base_params = _shape_params(conversation_id, where_clause=where_clause, columns=columns)
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


async def _read_stale_handle(
    client: httpx.AsyncClient,
    shape_url: str,
    snapshot: dict[str, Any],
    *,
    where_clause: str | None = None,
    columns: str = SHAPE_COLUMNS,
) -> httpx.Response:
    params = {
        **_shape_params(snapshot["conversationId"], where_clause=where_clause, columns=columns),
        "handle": snapshot["handle"],
        "offset": snapshot["offset"],
    }
    return await client.get(shape_url, params=params)


async def _read_live_page(
    client: httpx.AsyncClient, shape_url: str, snapshot: dict[str, Any], *, offset: str, where_clause: str, columns: str
) -> dict[str, Any]:
    params = {
        **_shape_params(snapshot["conversationId"], where_clause=where_clause, columns=columns, live="true"),
        "handle": snapshot["handle"],
        "offset": offset,
    }
    response = await client.get(shape_url, params=params)
    assert response.status_code == 200, {
        "conversationId": snapshot["conversationId"],
        "handle": snapshot["handle"],
        "offset": offset,
        "status": response.status_code,
        "headers": dict(response.headers),
        "body": response.content[:2000].decode(errors="replace"),
    }
    messages = _messages(response.content)
    event_offsets = [
        message.get("headers", {}).get("offset") for message in messages if message.get("headers", {}).get("operation")
    ]
    return {
        "status": response.status_code,
        "headers": dict(response.headers),
        "responseBytes": len(response.content),
        "offset": response.headers.get("electric-offset") or (event_offsets[-1] if event_offsets else None),
        "messages": messages,
        "operations": [message for message in messages if message.get("headers", {}).get("operation")],
    }


async def _seed_bounded_history(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
           SELECT $1, $1 || '-old-' || i::text, 'item', i::bigint, i::bigint
           FROM generate_series(1, 70) AS series(i)""",
        BOUNDED_HISTORY_CONVERSATION,
    )
    await pool.execute(
        """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
           SELECT $1, $1 || '-tail-' || i::text, 'item', $2::bigint + i::bigint, 1::bigint
           FROM generate_series(0, $3::int - 1) AS series(i)""",
        BOUNDED_HISTORY_CONVERSATION,
        BOUNDED_HISTORY_TAIL_ANCHOR,
        BOUNDED_HISTORY_TAIL_ROWS,
    )


async def _insert_older_history_rows(pool: asyncpg.Pool, first: int, last: int) -> list[dict[str, int]]:
    batches: list[dict[str, int]] = []
    for batch_first in range(first, last + 1, BOUNDED_HISTORY_BATCH_ROWS):
        batch_last = min(batch_first + BOUNDED_HISTORY_BATCH_ROWS - 1, last)
        await pool.execute(
            """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
               SELECT $1, $1 || '-old-' || i::text, 'item', i::bigint, i::bigint
               FROM generate_series($2::int, $3::int) AS series(i)""",
            BOUNDED_HISTORY_CONVERSATION,
            batch_first,
            batch_last,
        )
        batches.append(
            {"firstOldAnchor": batch_first, "lastOldAnchor": batch_last, "rows": batch_last - batch_first + 1}
        )
    return batches


async def _seed_interest_window(pool: asyncpg.Pool, conversation_id: str) -> None:
    await pool.execute(
        """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
           SELECT $1, $1 || '-item-' || i::text, 'item', i::bigint, i::bigint
           FROM generate_series(1, $2::int) AS series(i)""",
        conversation_id,
        BOUNDED_HISTORY_TAIL_ROWS,
    )


async def test_electric_bounded_tail_history_growth_and_expiry() -> None:
    outputs = undeclared_outputs_dir() / "electric-bounded-history"
    outputs.mkdir(parents=True, exist_ok=True)
    active_where = f"conversation_id = '{BOUNDED_HISTORY_CONVERSATION}' AND anchor >= {BOUNDED_HISTORY_TAIL_ANCHOR}"
    evidence: dict[str, Any] = {
        "electricImage": electric_1_8_1.IMAGE.tag,
        "configuredMaxShapes": SHAPE_LIMIT,
        "boundedConversation": BOUNDED_HISTORY_CONVERSATION,
        "tailPredicate": active_where,
        "tailRows": BOUNDED_HISTORY_TAIL_ROWS,
        "tailAnchorBase": str(BOUNDED_HISTORY_TAIL_ANCHOR),
        "historyTargets": [100, 10_000, 100_000],
        "batchRows": BOUNDED_HISTORY_BATCH_ROWS,
        "samples": [],
        "growthSteps": [],
        "rotationCycles": [],
    }
    for image in (ryuk.IMAGE, postgres_18.IMAGE, electric_1_8_1.IMAGE):
        load_oci_image(image)

    with Network() as network:
        postgres = (
            LoggedContainer(postgres_18.IMAGE.tag, test_name="electric-bounded-history-postgres")
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
                await _seed_bounded_history(pool)
                assert (
                    await pool.fetchval(
                        "SELECT count(*) FROM sync_view_row WHERE conversation_id=$1", BOUNDED_HISTORY_CONVERSATION
                    )
                    == 100
                )

                electric = (
                    LoggedContainer(electric_1_8_1.IMAGE.tag, test_name="electric-bounded-history-electric")
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
                        shape_url = f"{electric_url}/v1/shape"
                        active_snapshot = await _read_snapshot(
                            client,
                            shape_url,
                            BOUNDED_HISTORY_CONVERSATION,
                            where_clause=active_where,
                            columns=ACTIVE_SHAPE_COLUMNS,
                        )
                        assert active_snapshot["rowCount"] == BOUNDED_HISTORY_TAIL_ROWS, active_snapshot
                        active_tail = active_snapshot["rows"]
                        active_keys = {row["row_key"] for row in active_tail}
                        assert active_keys == {
                            f"{BOUNDED_HISTORY_CONVERSATION}-tail-{index}" for index in range(BOUNDED_HISTORY_TAIL_ROWS)
                        }, active_tail
                        active_anchors = sorted(int(row["anchor"]) for row in active_tail)
                        assert active_anchors == [
                            BOUNDED_HISTORY_TAIL_ANCHOR + index for index in range(BOUNDED_HISTORY_TAIL_ROWS)
                        ], active_tail
                        active_offset = active_snapshot["offset"]
                        assert active_offset, active_snapshot
                        evidence["initialActiveShape"] = {
                            "handle": active_snapshot["handle"],
                            "offset": active_offset,
                            "rowCount": active_snapshot["rowCount"],
                            "responseBytes": active_snapshot["responseBytes"],
                            "rows": active_tail,
                        }
                        evidence["samples"].append({"stage": "after-100-row-tail-snapshot", **_sample_memory(electric)})
                        _write_evidence(outputs / "bounded-history-evidence.json", evidence)

                        prior_history_rows = 100
                        prior_old_rows = prior_history_rows - BOUNDED_HISTORY_TAIL_ROWS
                        marker_key = f"{BOUNDED_HISTORY_CONVERSATION}-tail-0"
                        for target_rows in (10_000, 100_000):
                            target_old_rows = target_rows - BOUNDED_HISTORY_TAIL_ROWS
                            batch_records = await _insert_older_history_rows(pool, prior_old_rows + 1, target_old_rows)
                            observed_rows = await pool.fetchval(
                                "SELECT count(*) FROM sync_view_row WHERE conversation_id=$1",
                                BOUNDED_HISTORY_CONVERSATION,
                            )
                            assert observed_rows == target_rows, {
                                "targetRows": target_rows,
                                "observedRows": observed_rows,
                                "batches": batch_records,
                            }
                            marker_revision = await pool.fetchval(
                                """UPDATE sync_view_row SET revision = revision + 1
                                   WHERE conversation_id=$1 AND row_key=$2 RETURNING revision""",
                                BOUNDED_HISTORY_CONVERSATION,
                                marker_key,
                            )
                            assert marker_revision is not None

                            live_page = await _read_live_page(
                                client,
                                shape_url,
                                active_snapshot,
                                offset=active_offset,
                                where_clause=active_where,
                                columns=ACTIVE_SHAPE_COLUMNS,
                            )
                            operations = live_page["operations"]
                            selected_operations = [
                                message
                                for message in operations
                                if message.get("value", {}).get("row_key") == marker_key
                            ]
                            off_shape_operations = [
                                message
                                for message in operations
                                if int(message.get("value", {}).get("anchor", BOUNDED_HISTORY_TAIL_ANCHOR))
                                < BOUNDED_HISTORY_TAIL_ANCHOR
                            ]
                            assert len(operations) == 1, {
                                "targetRows": target_rows,
                                "offset": active_offset,
                                "livePage": live_page,
                            }
                            assert len(selected_operations) == 1, {
                                "targetRows": target_rows,
                                "markerKey": marker_key,
                                "livePage": live_page,
                            }
                            assert not off_shape_operations, {
                                "targetRows": target_rows,
                                "offShapeOperations": off_shape_operations,
                                "livePage": live_page,
                            }
                            selected_value = selected_operations[0]["value"]
                            assert int(selected_value["revision"]) == marker_revision, {
                                "expectedRevision": marker_revision,
                                "selectedValue": selected_value,
                            }
                            next_offset = live_page["offset"]
                            assert next_offset, live_page
                            selected_update_bytes = sum(
                                len(json.dumps(message, separators=(",", ":")).encode())
                                for message in selected_operations
                            )
                            await asyncio.sleep(1)
                            step = {
                                "historyRows": target_rows,
                                "olderRowsInserted": target_rows - prior_history_rows,
                                "insertBatches": batch_records,
                                "activeShapeHandle": active_snapshot["handle"],
                                "activeShapeRowCount": BOUNDED_HISTORY_TAIL_ROWS,
                                "requestOffset": active_offset,
                                "responseOffset": next_offset,
                                "responseBytes": live_page["responseBytes"],
                                "selectedUpdateBytes": selected_update_bytes,
                                "selectedUpdateRows": [selected_value],
                                "excludedRowsOnWire": len(off_shape_operations),
                                "operationCount": len(operations),
                                "markerRevision": marker_revision,
                                "databaseRows": observed_rows,
                            }
                            evidence["growthSteps"].append(step)
                            evidence["samples"].append(
                                {"stage": f"after-{target_rows}-history-rows", **_sample_memory(electric)}
                            )
                            _write_evidence(outputs / "bounded-history-evidence.json", evidence)
                            active_offset = next_offset
                            active_snapshot["offset"] = next_offset
                            prior_history_rows = target_rows
                            prior_old_rows = target_old_rows

                        for cycle in range(2):
                            rotation_handles = []
                            for suffix in ("a", "b"):
                                conversation_id = f"bounded-rotation-{cycle + 1}-{suffix}"
                                await _seed_interest_window(pool, conversation_id)
                                rotation_snapshot = await _read_snapshot(client, shape_url, conversation_id)
                                assert rotation_snapshot["rowCount"] == BOUNDED_HISTORY_TAIL_ROWS, rotation_snapshot
                                rotation_handles.append(
                                    {
                                        "conversationId": conversation_id,
                                        "handle": rotation_snapshot["handle"],
                                        "rowCount": rotation_snapshot["rowCount"],
                                        "responseBytes": rotation_snapshot["responseBytes"],
                                    }
                                )
                                evidence["samples"].append(
                                    {"stage": f"after-cycle-{cycle + 1}-open-{suffix}", **_sample_memory(electric)}
                                )

                            await asyncio.sleep(75)
                            stale_response = await _read_stale_handle(
                                client,
                                shape_url,
                                active_snapshot,
                                where_clause=active_where,
                                columns=ACTIVE_SHAPE_COLUMNS,
                            )
                            stale_messages = _messages(stale_response.content)
                            stale_controls = [message.get("headers", {}).get("control") for message in stale_messages]
                            assert stale_response.status_code == 409, {
                                "cycle": cycle + 1,
                                "activeHandle": active_snapshot["handle"],
                                "status": stale_response.status_code,
                                "headers": dict(stale_response.headers),
                                "body": stale_response.content.decode(errors="replace"),
                            }
                            assert "must-refetch" in stale_controls, {
                                "cycle": cycle + 1,
                                "activeHandle": active_snapshot["handle"],
                                "controls": stale_controls,
                                "body": stale_response.content.decode(errors="replace"),
                            }
                            evidence["samples"].append(
                                {"stage": f"after-cycle-{cycle + 1}-expiry", **_sample_memory(electric)}
                            )

                            reopened_snapshot = await _read_snapshot(
                                client,
                                shape_url,
                                BOUNDED_HISTORY_CONVERSATION,
                                where_clause=active_where,
                                columns=ACTIVE_SHAPE_COLUMNS,
                            )
                            assert reopened_snapshot["handle"] != active_snapshot["handle"], {
                                "cycle": cycle + 1,
                                "oldHandle": active_snapshot["handle"],
                                "newHandle": reopened_snapshot["handle"],
                            }
                            assert reopened_snapshot["rowCount"] == BOUNDED_HISTORY_TAIL_ROWS, reopened_snapshot
                            evidence["rotationCycles"].append(
                                {
                                    "cycle": cycle + 1,
                                    "rotatedInterests": rotation_handles,
                                    "waitSeconds": 75,
                                    "expiredHandle": active_snapshot["handle"],
                                    "staleStatus": stale_response.status_code,
                                    "staleControls": stale_controls,
                                    "reopenedHandle": reopened_snapshot["handle"],
                                    "reopenedRows": reopened_snapshot["rowCount"],
                                    "reopenedResponseBytes": reopened_snapshot["responseBytes"],
                                }
                            )
                            evidence["samples"].append(
                                {"stage": f"after-cycle-{cycle + 1}-reopen", **_sample_memory(electric)}
                            )
                            _write_evidence(outputs / "bounded-history-evidence.json", evidence)
                            active_snapshot = reopened_snapshot
                            active_offset = reopened_snapshot["offset"]

                        evidence["finalDatabaseRows"] = await pool.fetchval(
                            "SELECT count(*) FROM sync_view_row WHERE conversation_id=$1", BOUNDED_HISTORY_CONVERSATION
                        )
                        assert evidence["finalDatabaseRows"] == 100_000
            finally:
                if pool is not None:
                    await pool.close()
                _write_evidence(outputs / "bounded-history-evidence.json", evidence)


async def test_electric_shape_capacity_and_memory() -> None:
    outputs = undeclared_outputs_dir() / "electric-shape-capacity"
    outputs.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, Any] = {
        "electricImage": electric_1_8_1.IMAGE.tag,
        "configuredMaxShapes": SHAPE_LIMIT,
        "historySizes": {"capacity-small": SMALL_ROW_COUNT, "capacity-large": LARGE_ROW_COUNT},
        "samples": [],
    }
    for image in (ryuk.IMAGE, postgres_18.IMAGE, electric_1_8_1.IMAGE):
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
                    LoggedContainer(electric_1_8_1.IMAGE.tag, test_name="electric-shape-capacity-electric")
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
