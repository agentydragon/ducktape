"""Direct-file contract for ClickHouse's distributed DDL configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from more_itertools import one


@pytest.fixture
def schema_kustomization(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/schema/kustomization.yaml").read_text()))


@pytest.fixture
def schema_job(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/schema/schema-job.yaml").read_text()))


@pytest.fixture
def schema_flux(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "clickhouse/schema/flux-kustomization.yaml").read_text()))


def test_clickhouse_distributed_ddl_contract(
    clickhouse_installation: dict[str, Any],
    clickhouse_host: str,
    schema_kustomization: dict[str, Any],
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
    # This is a least-privilege boundary: the ingest identity must not gain
    # access to columns outside those used by the materialized views.
    assert grants == [
        "GRANT INSERT ON aiquota.raw_http_observations",
        "GRANT SELECT(event_id, observed_at, source, quota_windows, token_activity, reset_credits) "
        "ON aiquota.raw_http_observations",
    ]

    schema_pod_spec = schema_job["spec"]["template"]["spec"]
    schema_container = one(schema_pod_spec["containers"])
    schema_args = schema_container["args"]
    assert f"--host={clickhouse_host}" in schema_args
    assert "--port=9000" in schema_args

    schema_volume = one(volume for volume in schema_pod_spec["volumes"] if volume["name"] == "schema")
    schema_config_map_name = schema_volume["configMap"]["name"]
    schema_generator = one(
        generator
        for generator in schema_kustomization["configMapGenerator"]
        if generator["name"] == schema_config_map_name
    )
    schema_mount = one(mount for mount in schema_container["volumeMounts"] if mount["name"] == "schema")
    query_file_arg = one(arg for arg in schema_args if arg.startswith("--queries-file="))
    query_file = Path(query_file_arg.removeprefix("--queries-file="))
    assert query_file.parent == Path(schema_mount["mountPath"])
    assert query_file.name in schema_generator["files"]

    schema_health_check = one(schema_flux["spec"]["healthChecks"])
    assert schema_health_check == {
        "apiVersion": schema_job["apiVersion"],
        "kind": schema_job["kind"],
        "name": schema_job["metadata"]["name"],
        "namespace": schema_job["metadata"]["namespace"],
    }


if __name__ == "__main__":
    pytest_bazel.main()
