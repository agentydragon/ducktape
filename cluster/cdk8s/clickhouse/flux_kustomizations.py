"""Flux Kustomizations for the cluster/k8s/clickhouse slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on


def clickhouse(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, clickhouse_operator: Kustomization
) -> Kustomization:
    name = "clickhouse"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            decryption=SOPS_DECRYPTION,
            source_ref=artifact_source_ref(artifact),
            timeout="20m",
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="clickhouse-keeper.altinity.com/v1",
                    kind="ClickHouseKeeperInstallation",
                    name="clickhouse-keeper",
                    namespace="clickhouse",
                ),
                KustomizationSpecHealthChecks(
                    api_version="clickhouse.altinity.com/v1",
                    kind="ClickHouseInstallation",
                    name="clickhouse",
                    namespace="clickhouse",
                ),
            ],
            health_check_exprs=[
                KustomizationSpecHealthCheckExprs(
                    api_version="clickhouse-keeper.altinity.com/v1",
                    kind="ClickHouseKeeperInstallation",
                    current="status.status == 'Completed'",
                    failed="status.status == 'Aborted'",
                    in_progress="status.status != 'Completed' && status.status != 'Aborted'",
                ),
                KustomizationSpecHealthCheckExprs(
                    api_version="clickhouse.altinity.com/v1",
                    kind="ClickHouseInstallation",
                    current="status.status == 'Completed'",
                    failed="status.status == 'Aborted'",
                    in_progress="status.status != 'Completed' && status.status != 'Aborted'",
                ),
            ],
            depends_on=[flux_kustomization_depends_on(clickhouse_operator)],
        ),
    )
