"""Flux Kustomizations for the cluster/k8s/clickhouse slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def clickhouse(chart: Chart, clickhouse_operator: Kustomization) -> Kustomization:
    name = "clickhouse"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./",
            prune=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
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


def clickhouse_operator(chart: Chart, monitoring_crds: Kustomization) -> Kustomization:
    name = "clickhouse-operator"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/clickhouse/operator",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="10m",
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="clickhouse-operator",
                    namespace="clickhouse",
                )
            ],
            # The chart enables ServiceMonitor resources.
            depends_on=[flux_kustomization_depends_on(monitoring_crds)],
        ),
    )
