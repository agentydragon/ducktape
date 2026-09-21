"""Measure Electric memory while older database history grows outside a fixed tail shape."""

from __future__ import annotations

import asyncio
import json
from typing import Any

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
    BOUNDED_HISTORY_BATCH_ROWS,
    BOUNDED_HISTORY_CONVERSATION,
    BOUNDED_HISTORY_TAIL_ANCHOR,
    BOUNDED_HISTORY_TAIL_ROWS,
    SHAPE_LIMIT,
    _connect_postgres,
    _insert_older_history_rows,
    _messages,
    _read_live_page,
    _read_snapshot,
    _read_stale_handle,
    _sample_memory,
    _seed_bounded_history,
    _seed_interest_window,
    _wait_electric,
    _write_evidence,
)


async def test_electric_bounded_tail_history_growth_and_expiry() -> None:
    outputs = undeclared_outputs_dir() / "electric-bounded-history"
    outputs.mkdir(parents=True, exist_ok=True)
    active_where = f"conversation_id = '{BOUNDED_HISTORY_CONVERSATION}' AND anchor >= $1"
    active_where_params = (str(BOUNDED_HISTORY_TAIL_ANCHOR),)
    evidence: dict[str, Any] = {
        "electricImage": electric_1_8_1.IMAGE.tag,
        "configuredMaxShapes": SHAPE_LIMIT,
        "boundedConversation": BOUNDED_HISTORY_CONVERSATION,
        "tailPredicate": active_where,
        "tailParams": list(active_where_params),
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
                            where_params=active_where_params,
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
                                where_params=active_where_params,
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
                                where_params=active_where_params,
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
                                where_params=active_where_params,
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


if __name__ == "__main__":
    pytest_bazel.main()
