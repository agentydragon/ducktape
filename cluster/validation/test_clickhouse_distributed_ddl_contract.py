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


@pytest.fixture
def aiquota_kustomization(k8s_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / "aiquota/kustomization.yaml").read_text()))


@pytest.fixture
def aiquota_deployment(k8s_dir: Path) -> dict[str, Any]:
    for document in yaml.safe_load_all((k8s_dir / "aiquota/deployment.yaml").read_text()):
        if document["kind"] == "Deployment":
            return cast(dict[str, Any], document)
    raise AssertionError("no Deployment document in aiquota/deployment.yaml")


def _assert_native_ddl_container(
    container: dict[str, Any], pod_spec: dict[str, Any], kustomization: dict[str, Any], *, host: str, volume_name: str
) -> None:
    """A `clickhouse-client --queries-file=...` container's args, volume, and configMapGenerator agree.

    Same port-9000 rationale as the module docstring: the Altinity operator
    generates 9440 secure remote-server entries when ``secure: true`` is set,
    but ClickHouse has no TLS listener unless one is configured separately, so
    a 9440 remote entry cannot be recognized as local by DDLWorker.
    """

    args = container["args"]
    assert f"--host={host}" in args
    assert "--port=9000" in args

    volume = one(v for v in pod_spec["volumes"] if v["name"] == volume_name)
    config_map_name = volume["configMap"]["name"]
    generator = one(g for g in kustomization["configMapGenerator"] if g["name"] == config_map_name)
    mount = one(m for m in container["volumeMounts"] if m["name"] == volume_name)
    query_file_arg = one(arg for arg in args if arg.startswith("--queries-file="))
    query_file = Path(query_file_arg.removeprefix("--queries-file="))
    assert query_file.parent == Path(mount["mountPath"])
    assert query_file.name in generator["files"]


def test_clickhouse_distributed_ddl_contract(
    clickhouse_installation: dict[str, Any],
    clickhouse_host: str,
    schema_kustomization: dict[str, Any],
    schema_job: dict[str, Any],
    schema_flux: dict[str, Any],
    aiquota_kustomization: dict[str, Any],
    aiquota_deployment: dict[str, Any],
) -> None:
    """Central ClickHouse uses one plaintext native port consistently for ON CLUSTER DDL.

    The Altinity operator generates 9440 secure remote-server entries when
    ``secure: true`` is set, but ClickHouse has no TLS listener unless one is
    configured separately. A 9440 remote entry therefore cannot be recognized
    as local by DDLWorker. Keep the manifest, schema Job/aiquota migrate init
    container, and Flux health check pinned to the working port-9000 config.
    """
    configuration = clickhouse_installation["spec"]["configuration"]
    cluster_spec = one(item for item in configuration["clusters"] if item["name"] == "default")
    assert "secure" not in cluster_spec
    assert clickhouse_installation["spec"]["defaults"]["replicasUseFQDN"] == "yes"

    grants = configuration["users"]["aiquota_ingest/grants/query"]
    # This is a least-privilege boundary: the identity must not gain access to
    # columns outside those used by the materialized views, nor DDL outside its
    # own database (aiquota's migrate init container owns aiquota.* schema).
    assert grants == [
        "GRANT INSERT ON aiquota.raw_http_observations",
        "GRANT SELECT(event_id, observed_at, source, quota_windows, token_activity, reset_credits) "
        "ON aiquota.raw_http_observations",
        "GRANT SELECT, INSERT ON aiquota.aiquota_windows",
        "GRANT SELECT, INSERT ON aiquota.token_activity_daily",
        "GRANT SELECT, INSERT ON aiquota.reset_credits",
        "GRANT CREATE, DROP TABLE, DROP VIEW ON aiquota.*",
        "GRANT ALTER ADD COLUMN ON aiquota.*",
        "GRANT CLUSTER ON *.*",
    ]

    schema_pod_spec = schema_job["spec"]["template"]["spec"]
    _assert_native_ddl_container(
        one(schema_pod_spec["containers"]),
        schema_pod_spec,
        schema_kustomization,
        host=clickhouse_host,
        volume_name="schema",
    )

    schema_health_check = one(schema_flux["spec"]["healthChecks"])
    assert schema_health_check == {
        "apiVersion": schema_job["apiVersion"],
        "kind": schema_job["kind"],
        "name": schema_job["metadata"]["name"],
        "namespace": schema_job["metadata"]["namespace"],
    }

    aiquota_pod_spec = aiquota_deployment["spec"]["template"]["spec"]
    migrate_container = one(c for c in aiquota_pod_spec["initContainers"] if c["name"] == "migrate")
    _assert_native_ddl_container(
        migrate_container, aiquota_pod_spec, aiquota_kustomization, host=clickhouse_host, volume_name="schema"
    )


if __name__ == "__main__":
    pytest_bazel.main()
