"""Exercise Electric backpressure with a real downstream socket that stops reading."""

from __future__ import annotations

import asyncio
import fcntl
import socket
import struct
import termios
from typing import Any
from urllib.parse import urlencode, urlsplit

import asyncpg
import httpx
import pytest_bazel
from testcontainers.core.network import Network

from third_party.containers import electric_1_8_1, postgres_18, ryuk
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.agentplane_sync.projector import initialize_database
from x.agentplane_sync.shape_test_support import (
    ACTIVE_SHAPE_COLUMNS,
    BOUNDED_HISTORY_CONVERSATION,
    BOUNDED_HISTORY_TAIL_ANCHOR,
    BOUNDED_HISTORY_TAIL_ROWS,
    SHAPE_LIMIT,
    _connect_postgres,
    _messages,
    _read_live_page,
    _read_snapshot,
    _sample_memory,
    _seed_bounded_history,
    _shape_params,
    _wait_electric,
    _write_evidence,
)

MODEL_BYTES = 64 * 1024
WRITE_BATCHES = 48
SAMPLE_EVERY = 8
MAX_STALLED_RSS_GROWTH_KIB = 16 * 1024
MAX_FRESH_SUBSET_RESPONSE_BYTES = 3 * 1024 * 1024
SHAPE_COLUMNS = f"{ACTIVE_SHAPE_COLUMNS},model"


async def _open_stalled_shape_request(
    electric_url: str, snapshot: dict[str, Any], where_clause: str, where_params: tuple[str, ...]
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    parsed = urlsplit(electric_url)
    assert parsed.hostname is not None
    assert parsed.port is not None
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port, limit=4_096)
    raw_socket = writer.get_extra_info("socket")
    assert raw_socket is not None
    raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4_096)
    query = urlencode(
        {
            **_shape_params(
                BOUNDED_HISTORY_CONVERSATION,
                where_clause=where_clause,
                where_params=where_params,
                columns=SHAPE_COLUMNS,
                live="true",
            ),
            "handle": snapshot["handle"],
            "offset": snapshot["offset"],
        }
    )
    writer.write(
        (
            f"GET /v1/shape?{query} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Accept: application/json\r\n"
            "Accept-Encoding: identity\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    return reader, writer


def _stalled_reader_buffers(writer: asyncio.StreamWriter) -> dict[str, int]:
    raw_socket = writer.get_extra_info("socket")
    assert raw_socket is not None
    kernel_queue = fcntl.ioctl(raw_socket.fileno(), termios.FIONREAD, struct.pack("I", 0))
    return {
        "kernelReceiveQueueBytes": struct.unpack("I", kernel_queue)[0],
    }


async def _wait_for_wal_processed(pool: asyncpg.Pool, target_lsn: str) -> dict[str, Any]:
    latest_slots: list[dict[str, Any]] = []
    for attempt in range(240):
        rows = await pool.fetch(
            """SELECT slot_name, active, restart_lsn::text AS restart_lsn,
                      confirmed_flush_lsn::text AS confirmed_flush_lsn,
                      confirmed_flush_lsn >= ($1::text)::pg_lsn AS processed_target
               FROM pg_replication_slots
               WHERE slot_type = 'logical'
               ORDER BY slot_name""",
            target_lsn,
        )
        latest_slots = [dict(row) for row in rows]
        if any(slot["processed_target"] for slot in latest_slots):
            return {"targetLsn": target_lsn, "attempt": attempt + 1, "slots": latest_slots}
        await asyncio.sleep(0.05)
    raise AssertionError({"targetLsn": target_lsn, "slots": latest_slots})


async def _update_tail(pool: asyncpg.Pool | asyncpg.Connection, batch: int) -> tuple[str, str]:
    model = f"batch-{batch:03d}:" + (chr(65 + batch % 26) * (MODEL_BYTES - 10))
    await pool.execute(
        """UPDATE sync_view_row SET revision = revision + 1, model=$1
           WHERE conversation_id=$2 AND anchor >= $3""",
        model,
        BOUNDED_HISTORY_CONVERSATION,
        BOUNDED_HISTORY_TAIL_ANCHOR,
    )
    target_lsn = await pool.fetchval("SELECT pg_current_wal_lsn()::text")
    assert isinstance(target_lsn, str)
    return model, target_lsn


def _latest_models(operations: list[dict[str, Any]]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for operation in operations:
        value = operation.get("value", {})
        row_key = value.get("row_key")
        model = value.get("model")
        if isinstance(row_key, str) and isinstance(model, str):
            latest[row_key] = model
    return latest


async def _open_changes_only_at_now(
    client: httpx.AsyncClient,
    shape_url: str,
    where_clause: str,
    where_params: tuple[str, ...],
) -> dict[str, Any]:
    params = {
        **_shape_params(
            BOUNDED_HISTORY_CONVERSATION,
            where_clause=where_clause,
            where_params=where_params,
            columns=SHAPE_COLUMNS,
        ),
        "log": "changes_only",
        "queryable_columns": SHAPE_COLUMNS,
        "offset": "now",
    }
    response = await client.get(shape_url, params=params)
    assert response.status_code == 200, {"status": response.status_code, "body": response.text[:2000]}
    handle = response.headers.get("electric-handle")
    offset = response.headers.get("electric-offset")
    assert handle, {"headers": dict(response.headers), "body": response.text[:2000]}
    assert offset, {"headers": dict(response.headers), "body": response.text[:2000]}
    messages = _messages(response.content)
    return {
        "conversationId": BOUNDED_HISTORY_CONVERSATION,
        "handle": handle,
        "offset": offset,
        "responseBytes": len(response.content),
        "operationCount": sum(1 for message in messages if message.get("headers", {}).get("operation")),
    }


async def _read_current_subset(
    client: httpx.AsyncClient,
    shape_url: str,
    changes_only: dict[str, Any],
    where_clause: str,
    where_params: tuple[str, ...],
) -> dict[str, Any]:
    params = {
        **_shape_params(
            BOUNDED_HISTORY_CONVERSATION,
            where_clause=where_clause,
            where_params=where_params,
            columns=SHAPE_COLUMNS,
        ),
        "log": "changes_only",
        "queryable_columns": SHAPE_COLUMNS,
        "handle": changes_only["handle"],
        "offset": changes_only["offset"],
    }
    response = await client.post(
        shape_url,
        params=params,
        json={"where": "true = true", "order_by": "anchor ASC", "limit": BOUNDED_HISTORY_TAIL_ROWS},
    )
    assert response.status_code == 200, {"status": response.status_code, "body": response.text[:2000]}
    body = response.json()
    data = body.get("data")
    metadata = body.get("metadata")
    assert isinstance(data, list), body
    assert isinstance(metadata, dict), body
    rows = [message["value"] for message in data if message.get("headers", {}).get("operation")]
    return {"rows": rows, "metadata": metadata, "responseBytes": len(response.content)}


async def _consume_latest_tail(
    client: httpx.AsyncClient,
    shape_url: str,
    snapshot: dict[str, Any],
    *,
    offset: str,
    where_clause: str,
    where_params: tuple[str, ...],
    expected_model: str,
    max_pages: int = 30,
) -> tuple[str, list[dict[str, Any]]]:
    latest_models: dict[str, str] = {}
    pages: list[dict[str, Any]] = []
    for _ in range(max_pages):
        page = await _read_live_page(
            client,
            shape_url,
            snapshot,
            offset=offset,
            where_clause=where_clause,
            where_params=where_params,
            columns=SHAPE_COLUMNS,
        )
        next_offset = page["offset"]
        assert isinstance(next_offset, str), page
        assert next_offset != offset, page
        offset = next_offset
        latest_models.update(_latest_models(page["operations"]))
        pages.append({"responseBytes": page["responseBytes"], "offset": offset})
        if len(latest_models) == BOUNDED_HISTORY_TAIL_ROWS and set(latest_models.values()) == {expected_model}:
            return offset, pages
    raise AssertionError({"expectedModel": expected_model, "latestModels": latest_models, "pages": pages})


async def _prove_changes_only_subset_recovery(
    client: httpx.AsyncClient,
    shape_url: str,
    pool: asyncpg.Pool,
    where_clause: str,
    where_params: tuple[str, ...],
    expected_model: str,
) -> dict[str, Any]:
    changes_only = await _open_changes_only_at_now(client, shape_url, where_clause, where_params)
    assert changes_only["operationCount"] == 0, changes_only
    async with pool.acquire() as connection:
        async with connection.transaction():
            race_model, _ = await _update_tail(connection, WRITE_BATCHES + 1)
            race_xid = await connection.fetchval("SELECT pg_current_xact_id()::text")
            assert isinstance(race_xid, str)
            pre_commit_subset = await _read_current_subset(client, shape_url, changes_only, where_clause, where_params)
            pre_commit_models = {row["row_key"]: row["model"] for row in pre_commit_subset["rows"]}
            assert len(pre_commit_models) == BOUNDED_HISTORY_TAIL_ROWS
            assert set(pre_commit_models.values()) == {expected_model}
            assert pre_commit_subset["responseBytes"] <= MAX_FRESH_SUBSET_RESPONSE_BYTES
            assert race_xid in pre_commit_subset["metadata"].get("xip_list", [])
        race_lsn = await connection.fetchval("SELECT pg_current_wal_lsn()::text")
    assert isinstance(race_lsn, str)
    race_checkpoint = await _wait_for_wal_processed(pool, race_lsn)
    changes_only_offset, race_pages = await _consume_latest_tail(
        client,
        shape_url,
        changes_only,
        offset=changes_only["offset"],
        where_clause=where_clause,
        where_params=where_params,
        expected_model=race_model,
    )
    return {
        "start": changes_only,
        "preCommitSubset": {
            "rowCount": len(pre_commit_models),
            "responseBytes": pre_commit_subset["responseBytes"],
            "xipContainsHeldWrite": True,
        },
        "committedWrite": {
            "walCheckpoint": race_checkpoint,
            "pages": len(race_pages),
            "offset": changes_only_offset,
        },
    }


async def test_stalled_downstream_reader_disconnect_resume_and_restart() -> None:
    outputs = undeclared_outputs_dir() / "electric-stalled-reader"
    outputs.mkdir(parents=True, exist_ok=True)
    where_clause = f"conversation_id = '{BOUNDED_HISTORY_CONVERSATION}' AND anchor >= $1"
    where_params = (str(BOUNDED_HISTORY_TAIL_ANCHOR),)
    evidence: dict[str, Any] = {
        "electricImage": electric_1_8_1.IMAGE.tag,
        "configuredMaxShapes": SHAPE_LIMIT,
        "shapeRows": BOUNDED_HISTORY_TAIL_ROWS,
        "modelBytesPerRow": MODEL_BYTES,
        "writeBatches": WRITE_BATCHES,
        "writtenModelBytes": (WRITE_BATCHES + 1) * BOUNDED_HISTORY_TAIL_ROWS * MODEL_BYTES,
        "maxStalledRssGrowthKiB": MAX_STALLED_RSS_GROWTH_KIB,
        "memorySamples": [],
        "healthyPages": [],
        "walCheckpoints": [],
    }
    for image in (ryuk.IMAGE, postgres_18.IMAGE, electric_1_8_1.IMAGE):
        load_oci_image(image)

    with Network() as network:
        postgres = (
            LoggedContainer(postgres_18.IMAGE.tag, test_name="electric-stalled-reader-postgres")
            .with_network(network)
            .with_network_aliases("postgres")
            .with_exposed_ports(5432)
            .with_env("POSTGRES_USER", "postgres")
            .with_env("POSTGRES_PASSWORD", "postgres")
            .with_command("postgres -c wal_level=logical -c max_wal_senders=10 -c max_replication_slots=10")
        )
        with postgres:
            pool: asyncpg.Pool | None = None
            stalled_writer: asyncio.StreamWriter | None = None
            try:
                dsn = (
                    f"postgresql://postgres:postgres@{postgres.get_container_host_ip()}"
                    f":{postgres.get_exposed_port(5432)}/postgres"
                )
                pool = await _connect_postgres(dsn)
                await initialize_database(pool)
                await _seed_bounded_history(pool)
                electric = (
                    LoggedContainer(electric_1_8_1.IMAGE.tag, test_name="electric-stalled-reader-electric")
                    .with_network(network)
                    .with_network_aliases("electric")
                    .with_exposed_ports(3000)
                    .with_env("DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/postgres?sslmode=disable")
                    .with_env("ELECTRIC_INSECURE", "true")
                    .with_env("ELECTRIC_MAX_SHAPES", str(SHAPE_LIMIT))
                )
                with electric:
                    electric_url = f"http://{electric.get_container_host_ip()}:{electric.get_exposed_port(3000)}"
                    evidence["initialHealth"] = await _wait_electric(electric_url)
                    evidence["memorySamples"].append(
                        {"stage": "ready", "writtenModelBytes": 0, **_sample_memory(electric)}
                    )
                    async with httpx.AsyncClient(timeout=120) as client:
                        shape_url = f"{electric_url}/v1/shape"
                        snapshot = await _read_snapshot(
                            client,
                            shape_url,
                            BOUNDED_HISTORY_CONVERSATION,
                            where_clause=where_clause,
                            where_params=where_params,
                            columns=SHAPE_COLUMNS,
                        )
                        assert snapshot["rowCount"] == BOUNDED_HISTORY_TAIL_ROWS
                        healthy_offset = snapshot["offset"]
                        stalled_reader, stalled_writer = await _open_stalled_shape_request(
                            electric_url, snapshot, where_clause, where_params
                        )

                        expected_model, target_lsn = await _update_tail(pool, 0)
                        evidence["walCheckpoints"].append(await _wait_for_wal_processed(pool, target_lsn))
                        healthy_offset, healthy_pages = await _consume_latest_tail(
                            client,
                            shape_url,
                            snapshot,
                            offset=healthy_offset,
                            where_clause=where_clause,
                            where_params=where_params,
                            expected_model=expected_model,
                        )
                        response_headers = await asyncio.wait_for(stalled_reader.readuntil(b"\r\n\r\n"), timeout=30)
                        assert response_headers.startswith(b"HTTP/1.1 200"), response_headers
                        evidence["stalledResponseHeaders"] = response_headers.decode(errors="replace").splitlines()
                        evidence["stalledBuffersAtStart"] = _stalled_reader_buffers(stalled_writer)
                        evidence["healthyPages"].extend({"batch": 0, **page} for page in healthy_pages)
                        evidence["memorySamples"].append(
                            {
                                "stage": "stalled-response-started",
                                "writtenModelBytes": BOUNDED_HISTORY_TAIL_ROWS * MODEL_BYTES,
                                **_sample_memory(electric),
                            }
                        )

                        for batch in range(1, WRITE_BATCHES + 1):
                            expected_model, target_lsn = await _update_tail(pool, batch)
                            checkpoint = await _wait_for_wal_processed(pool, target_lsn)
                            if batch % SAMPLE_EVERY == 0 or batch == WRITE_BATCHES:
                                evidence["walCheckpoints"].append(checkpoint)
                            healthy_offset, healthy_pages = await _consume_latest_tail(
                                client,
                                shape_url,
                                snapshot,
                                offset=healthy_offset,
                                where_clause=where_clause,
                                where_params=where_params,
                                expected_model=expected_model,
                            )
                            evidence["healthyPages"].extend({"batch": batch, **page} for page in healthy_pages)
                            if batch % SAMPLE_EVERY == 0:
                                evidence["memorySamples"].append(
                                    {
                                        "stage": f"stalled-after-batch-{batch}",
                                        "writtenModelBytes": (batch + 1)
                                        * BOUNDED_HISTORY_TAIL_ROWS
                                        * MODEL_BYTES,
                                        **_sample_memory(electric),
                                    }
                                )
                                _write_evidence(outputs / "stalled-reader-evidence.json", evidence)

                        evidence["stalledBuffersBeforeDisconnect"] = _stalled_reader_buffers(stalled_writer)
                        assert any(evidence["stalledBuffersBeforeDisconnect"].values()), evidence

                        stalled_writer.close()
                        await stalled_writer.wait_closed()
                        stalled_writer = None
                        resumed_offset, resumed_pages = await _consume_latest_tail(
                            client,
                            shape_url,
                            snapshot,
                            offset=snapshot["offset"],
                            where_clause=where_clause,
                            where_params=where_params,
                            expected_model=expected_model,
                            max_pages=(WRITE_BATCHES + 1) * 8,
                        )
                        evidence["resume"] = {
                            "responseBytes": sum(page["responseBytes"] for page in resumed_pages),
                            "pages": len(resumed_pages),
                            "latestRows": BOUNDED_HISTORY_TAIL_ROWS,
                            "offset": resumed_offset,
                        }
                        evidence["memorySamples"].append(
                            {
                                "stage": "after-disconnect-resume",
                                "writtenModelBytes": evidence["writtenModelBytes"],
                                **_sample_memory(electric),
                            }
                        )

                        host_port_before_restart = electric.get_exposed_port(3000)
                        electric.get_wrapped_container().stop(timeout=10)
                        electric.get_wrapped_container().start()
                        host_port_after_restart = electric.get_exposed_port(3000)
                        electric_url = f"http://{electric.get_container_host_ip()}:{host_port_after_restart}"
                        shape_url = f"{electric_url}/v1/shape"
                        evidence["restartHealth"] = await _wait_electric(electric_url)
                        evidence["memorySamples"].append(
                            {
                                "stage": "after-persisted-restart",
                                "writtenModelBytes": evidence["writtenModelBytes"],
                                **_sample_memory(electric),
                            }
                        )
                        await client.aclose()
                        async with httpx.AsyncClient(timeout=120) as restarted_client:
                            restarted = await _read_snapshot(
                                restarted_client,
                                shape_url,
                                BOUNDED_HISTORY_CONVERSATION,
                                where_clause=where_clause,
                                where_params=where_params,
                                columns=SHAPE_COLUMNS,
                            )
                            restarted_models = {row["row_key"]: row["model"] for row in restarted["rows"]}
                            assert len(restarted_models) == BOUNDED_HISTORY_TAIL_ROWS
                            assert set(restarted_models.values()) == {expected_model}
                            evidence["restart"] = {
                                "handleBefore": snapshot["handle"],
                                "handleAfter": restarted["handle"],
                                "reusedPersistedHandle": restarted["handle"] == snapshot["handle"],
                                "operationCount": restarted["rowCount"],
                                "latestRows": len(restarted_models),
                                "responseBytes": restarted["responseBytes"],
                                "hostPortBefore": host_port_before_restart,
                                "hostPortAfter": host_port_after_restart,
                            }
                            evidence["changesOnlySubsetRecovery"] = await _prove_changes_only_subset_recovery(
                                restarted_client,
                                shape_url,
                                pool,
                                where_clause,
                                where_params,
                                expected_model,
                            )

                        rss = [
                            sample["pid1VmRSSKiB"]
                            for sample in evidence["memorySamples"]
                            if sample.get("pid1VmRSSKiB") is not None and sample["stage"].startswith("stalled-")
                        ]
                        evidence["stalledRssGrowthKiB"] = max(rss) - rss[0]
                        stalled_cgroup = [
                            sample["cgroupUsageBytes"]
                            for sample in evidence["memorySamples"]
                            if sample["stage"].startswith("stalled-") and sample.get("cgroupUsageBytes") is not None
                        ]
                        evidence["stalledCgroupGrowthBytes"] = max(stalled_cgroup) - stalled_cgroup[0]
                        assert evidence["writtenModelBytes"] > 4 * MAX_STALLED_RSS_GROWTH_KIB * 1024
                        assert evidence["stalledRssGrowthKiB"] < MAX_STALLED_RSS_GROWTH_KIB, evidence["memorySamples"]
                        evidence["conclusion"] = (
                            "Finite sample: a real socket stopped after HTTP headers while its receive buffers filled; "
                            "each write was observed through Electric's logical-replication checkpoint, and independent "
                            "and resumed readers reached the latest 30 rows. PID RSS stayed within this sample's budget; "
                            "container cgroup growth is recorded but not bounded by this test. The spike's changes-only "
                            "shape started at now and its bounded subset snapshot reconciled a held write through the live stream."
                        )
            finally:
                if stalled_writer is not None:
                    stalled_writer.close()
                    await stalled_writer.wait_closed()
                if pool is not None:
                    await pool.close()
                _write_evidence(outputs / "stalled-reader-evidence.json", evidence)


if __name__ == "__main__":
    pytest_bazel.main()
