"""Exercise Electric backpressure with a real downstream socket that stops reading."""

from __future__ import annotations

import asyncio
import json
import socket
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
WRITE_BATCHES = 32
SAMPLE_EVERY = 4
SHAPE_COLUMNS = f"{ACTIVE_SHAPE_COLUMNS},model"


async def _open_stalled_shape_request(
    electric_url: str, snapshot: dict[str, Any], where_clause: str, where_params: tuple[str, ...]
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    parsed = urlsplit(electric_url)
    assert parsed.hostname is not None and parsed.port is not None
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
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


async def _update_tail(pool: asyncpg.Pool, batch: int) -> str:
    model = f"batch-{batch:03d}:" + (chr(65 + batch % 26) * (MODEL_BYTES - 10))
    await pool.execute(
        """UPDATE sync_view_row SET revision = revision + 1, model=$1
           WHERE conversation_id=$2 AND anchor >= $3""",
        model,
        BOUNDED_HISTORY_CONVERSATION,
        BOUNDED_HISTORY_TAIL_ANCHOR,
    )
    return model


def _latest_models(operations: list[dict[str, Any]]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for operation in operations:
        value = operation.get("value", {})
        row_key = value.get("row_key")
        model = value.get("model")
        if isinstance(row_key, str) and isinstance(model, str):
            latest[row_key] = model
    return latest


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
        "memorySamples": [],
        "healthyPages": [],
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
                    evidence["memorySamples"].append({"stage": "ready", **_sample_memory(electric)})
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

                        expected_model = await _update_tail(pool, 0)
                        first_page = await _read_live_page(
                            client,
                            shape_url,
                            snapshot,
                            offset=healthy_offset,
                            where_clause=where_clause,
                            where_params=where_params,
                            columns=SHAPE_COLUMNS,
                        )
                        healthy_offset = first_page["offset"]
                        response_headers = await asyncio.wait_for(stalled_reader.readuntil(b"\r\n\r\n"), timeout=30)
                        assert response_headers.startswith(b"HTTP/1.1 200"), response_headers
                        evidence["stalledResponseHeaders"] = response_headers.decode(errors="replace").splitlines()
                        evidence["healthyPages"].append(
                            {"batch": 0, "responseBytes": first_page["responseBytes"], "offset": healthy_offset}
                        )
                        evidence["memorySamples"].append(
                            {"stage": "stalled-response-started", **_sample_memory(electric)}
                        )

                        for batch in range(1, WRITE_BATCHES + 1):
                            expected_model = await _update_tail(pool, batch)
                            page = await _read_live_page(
                                client,
                                shape_url,
                                snapshot,
                                offset=healthy_offset,
                                where_clause=where_clause,
                                where_params=where_params,
                                columns=SHAPE_COLUMNS,
                            )
                            healthy_offset = page["offset"]
                            models = _latest_models(page["operations"])
                            assert len(models) == BOUNDED_HISTORY_TAIL_ROWS, page
                            assert set(models.values()) == {expected_model}
                            evidence["healthyPages"].append(
                                {"batch": batch, "responseBytes": page["responseBytes"], "offset": healthy_offset}
                            )
                            if batch % SAMPLE_EVERY == 0:
                                evidence["memorySamples"].append(
                                    {"stage": f"stalled-after-batch-{batch}", **_sample_memory(electric)}
                                )
                                _write_evidence(outputs / "stalled-reader-evidence.json", evidence)

                        stalled_writer.close()
                        await stalled_writer.wait_closed()
                        stalled_writer = None
                        resumed = await _read_live_page(
                            client,
                            shape_url,
                            snapshot,
                            offset=snapshot["offset"],
                            where_clause=where_clause,
                            where_params=where_params,
                            columns=SHAPE_COLUMNS,
                        )
                        resumed_models = _latest_models(resumed["operations"])
                        assert len(resumed_models) == BOUNDED_HISTORY_TAIL_ROWS, resumed
                        assert set(resumed_models.values()) == {expected_model}
                        evidence["resume"] = {
                            "responseBytes": resumed["responseBytes"],
                            "operationCount": len(resumed["operations"]),
                            "latestRows": len(resumed_models),
                            "offset": resumed["offset"],
                        }
                        evidence["memorySamples"].append({"stage": "after-disconnect-resume", **_sample_memory(electric)})

                        electric.get_wrapped_container().stop(timeout=10)
                        electric.get_wrapped_container().start()
                        evidence["restartHealth"] = await _wait_electric(electric_url)
                        evidence["memorySamples"].append({"stage": "after-persisted-restart", **_sample_memory(electric)})
                        restarted = await _read_snapshot(
                            client,
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
                            "rowCount": restarted["rowCount"],
                            "responseBytes": restarted["responseBytes"],
                        }

                        rss = [
                            sample["pid1VmRSSKiB"]
                            for sample in evidence["memorySamples"]
                            if sample.get("pid1VmRSSKiB") is not None and sample["stage"].startswith("stalled-")
                        ]
                        evidence["stalledRssRangeKiB"] = max(rss) - min(rss)
                        assert evidence["stalledRssRangeKiB"] < 96 * 1024, evidence["memorySamples"]
                        evidence["conclusion"] = (
                            "Finite sample: one unread response did not accumulate one application response per later write; "
                            "the independent reader and resumed old offset both reached the latest 30 rows."
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
