"""Direct-file contract for ClickHouse's distributed DDL configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_K8S_ROOT_KUSTOMIZATION = "_main/cluster/k8s/kustomization.yaml"


@pytest.fixture(scope="session")
def k8s_dir() -> Path:
    return get_required_path(_K8S_ROOT_KUSTOMIZATION).parent


def test_clickhouse_distributed_ddl_contract(k8s_dir: Path) -> None:
    """Central ClickHouse uses one plaintext native port consistently for ON CLUSTER DDL.

    The Altinity operator generates 9440 secure remote-server entries when
    ``secure: true`` is set, but ClickHouse has no TLS listener unless one is
    configured separately. A 9440 remote entry therefore cannot be recognized
    as local by DDLWorker. Keep the manifest, schema Job, and Flux health check
    pinned to the working port-9000 configuration.
    """
    clickhouse_dir = k8s_dir / "clickhouse"
    installation = yaml.safe_load((clickhouse_dir / "cluster/clickhouse.yaml").read_text())
    configuration = installation["spec"]["configuration"]
    cluster_spec = next(item for item in configuration["clusters"] if item["name"] == "default")
    assert "secure" not in cluster_spec
    assert installation["spec"]["defaults"]["replicasUseFQDN"] == "yes"

    grants = configuration["users"]["aiquota_ingest/grants/query"]
    assert grants == [
        "GRANT INSERT ON aiquota.raw_http_observations",
        "GRANT SELECT(event_id, observed_at, source, quota_windows, token_activity, reset_credits) "
        "ON aiquota.raw_http_observations",
    ]

    cluster_kustomization = yaml.safe_load((clickhouse_dir / "cluster/kustomization.yaml").read_text())
    generated_files = cluster_kustomization["configMapGenerator"][0]["files"]
    assert generated_files == ["system_logs.xml"]

    schema_sql = (clickhouse_dir / "schema/schema.sql").read_text()
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

    schema_job = yaml.safe_load((clickhouse_dir / "schema/schema-job.yaml").read_text())
    assert schema_job["metadata"]["name"] == "clickhouse-aiquota-schema-v7"
    schema_args = schema_job["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--host=clickhouse.clickhouse.svc.cluster.local" in schema_args
    assert "--port=9000" in schema_args
    assert not any(item.startswith("chi-clickhouse-clickhouse-") for item in schema_args)

    schema_flux = yaml.safe_load((clickhouse_dir / "schema/flux-kustomization.yaml").read_text())
    assert schema_flux["spec"]["healthChecks"] == [
        {"apiVersion": "batch/v1", "kind": "Job", "name": "clickhouse-aiquota-schema-v7", "namespace": "clickhouse"}
    ]


if __name__ == "__main__":
    pytest_bazel.main()
