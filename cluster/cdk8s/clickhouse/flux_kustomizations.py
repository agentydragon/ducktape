"""Flux Kustomizations for the cluster/k8s/clickhouse slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def clickhouse() -> dict[str, object]:
    name = "clickhouse"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/clickhouse/cluster",
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
            depends_on=[KustomizationSpecDependsOn(name="clickhouse-operator", namespace="ducktape-flux")],
        ),
    )


def clickhouse_namespace() -> dict[str, object]:
    name = "clickhouse-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/clickhouse/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            wait=True,
            health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="clickhouse")],
            depends_on=[],
        ),
    )


def clickhouse_operator() -> dict[str, object]:
    name = "clickhouse-operator"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/clickhouse/operator",
            prune=True,
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
            depends_on=[
                KustomizationSpecDependsOn(name="clickhouse-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # the chart's serviceMonitor.enabled
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/clickhouse/cluster/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, clickhouse())
    path = root / "cluster/k8s/clickhouse/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, clickhouse_namespace())
    path = root / "cluster/k8s/clickhouse/operator/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, clickhouse_operator())
