"""Measure Electric shape expiry and process memory with a bounded shape cap."""

from __future__ import annotations

import asyncio
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
    LARGE_ROW_COUNT,
    SHAPE_LIMIT,
    SMALL_ROW_COUNT,
    _connect_postgres,
    _read_snapshot,
    _read_stale_handle,
    _sample_memory,
    _seed_history,
    _wait_electric,
    _write_evidence,
)


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
