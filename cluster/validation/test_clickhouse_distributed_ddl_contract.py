"""Direct-file contract for ClickHouse's distributed DDL configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from more_itertools import one


@pytest.fixture
def clickhouse_installation(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/cluster/clickhouse.yaml").read_text()))


@pytest.fixture
def cluster_kustomization(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/cluster/kustomization.yaml").read_text()))


@pytest.fixture
def schema_sql(k8s_dir: Path) -> str:
    return (k8s_dir / "clickhouse/schema/schema.sql").read_text()


@pytest.fixture
def schema_job(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/schema/schema-job.yaml").read_text()))


@pytest.fixture
def schema_flux(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/schema/flux-kustomization.yaml").read_text()))


def test_clickhouse_distributed_ddl_contract(
    clickhouse_installation: dict[str, Any],
    cluster_kustomization: dict[str, Any],
    schema_sql: str,
    schema_job: dict[str, Any],
    schema_flux: dict[str, Any],
) -> None:
    """Central ClickHouse uses one plaintext native port consistently for ON CLUSTER DDL.

    The Altinity operator generates 9440 secure remote-server entries when
    ``secure: true`` is set, but ClickHouse has no TLS listener unless one is
    configured separately. A 9440 remote entry therefore cannot be recognized
    as local by DDLWorker. Keep the manifest, schema Job, and Flux health check
    pinned to the working port-9000 configuration.
    """
    configuration = clickhouse_installation["spec"]["configuration"]
    cluster_spec = one(item for item in configuration["clusters"] if item["name"] == "default")
    assert "secure" not in cluster_spec
    assert clickhouse_installation["spec"]["defaults"]["replicasUseFQDN"] == "yes"

    grants = configuration["users"]["aiquota_ingest/grants/query"]
    assert grants == [
        "GRANT INSERT ON aiquota.raw_http_observations",
        "GRANT SELECT(event_id, observed_at, source, quota_windows, token_activity, reset_credits) "
        "ON aiquota.raw_http_observations",
    ]

    assert cluster_kustomization["configMapGenerator"][0]["files"] == ["system_logs.xml"]

    for statement in (
        "CREATE DATABASE IF NOT EXISTS aiquota ON CLUSTER default;",
        "CREATE TABLE IF NOT EXISTS aiquota.raw_http_observations ON CLUSTER default",
        "CREATE TABLE IF NOT EXISTS aiquota.aiquota_windows ON CLUSTER default",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS aiquota.aiquota_windows_mv ON CLUSTER default",
        "CREATE TABLE IF NOT EXISTS aiquota.token_activity_daily ON CLUSTER default",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS aiquota.token_activity_daily_mv ON CLUSTER default",
        "CREATE TABLE IF NOT EXISTS aiquota.reset_credits ON CLUSTER default",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS aiquota.reset_credits_mv ON CLUSTER default",
    ):
        assert statement in schema_sql

    assert schema_job["metadata"]["name"] == "clickhouse-aiquota-schema-v7"
    schema_args = schema_job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--host=clickhouse.clickhouse.svc.cluster.local" in schema_args
    assert "--port=9000" in schema_args
    assert not any(item.startswith("chi-clickhouse-clickhouse-") for item in schema_args)

    assert schema_flux["spec"]["healthChecks"] == [
        {"apiVersion": "batch/v1", "kind": "Job", "name": "clickhouse-aiquota-schema-v7", "namespace": "clickhouse"}
    ]


if __name__ == "__main__":
    pytest_bazel.main()
