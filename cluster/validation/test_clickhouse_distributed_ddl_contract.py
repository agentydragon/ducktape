"""Contract between the hand-written ClickHouseInstallation and the generated
`clickhouse-client` containers that run distributed DDL against it."""

from __future__ import annotations

from typing import Any, cast

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from more_itertools import one

from cluster.cdk8s import aiquota_constructs, clickhouse_schema_constructs


def _native_ddl_containers() -> list[dict[str, Any]]:
    """The schema Job's container and aiquota's `migrate` init container, synthesized in memory."""
    schema_objects = cast(
        list[dict[str, Any]], Cdk8sTesting.synth(clickhouse_schema_constructs.chart(Cdk8sTesting.app()))
    )
    aiquota_objects = cast(list[dict[str, Any]], Cdk8sTesting.synth(aiquota_constructs.chart(Cdk8sTesting.app())))
    job = one(obj for obj in schema_objects if obj["kind"] == "Job")
    deployment = one(obj for obj in aiquota_objects if obj["kind"] == "Deployment")
    return [
        one(job["spec"]["template"]["spec"]["containers"]),
        one(deployment["spec"]["template"]["spec"]["initContainers"]),
    ]


def test_clickhouse_distributed_ddl_contract(clickhouse_installation: dict[str, Any], clickhouse_host: str) -> None:
    """Central ClickHouse uses one plaintext native port consistently for ON CLUSTER DDL.

    The Altinity operator generates 9440 secure remote-server entries when
    ``secure: true`` is set, but ClickHouse has no TLS listener unless one is
    configured separately. A 9440 remote entry therefore cannot be recognized
    as local by DDLWorker. Keep the manifest and the schema Job/aiquota migrate
    init container pinned to the working port-9000 config.
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

    for container in _native_ddl_containers():
        assert f"--host={clickhouse_host}" in container["args"]
        assert "--port=9000" in container["args"]


if __name__ == "__main__":
    pytest_bazel.main()
