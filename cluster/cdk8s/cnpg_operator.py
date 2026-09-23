"""The CloudNativePG operator and its Barman Cloud backup plugin: the `cnpg-system`
Namespace, the shared HelmRepository and both HelmReleases, which install their own CRDs.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
    HelmReleaseSpecUpgradeRemediation,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "cnpg"
NAMESPACE = "cnpg-system"
OUTPUT_DIR = "cluster/k8s/cnpg"
_BARMAN_CLOUD = "plugin-barman-cloud"
# The operator backs two failurePolicy: Fail webhooks (Cluster, Backup, ScheduledBackup),
# so while it is down those writes are rejected outright — and it is the operator
# reconciling every Postgres cluster here. Same treatment as the other blocking-webhook
# backends.
#
# This is the operator Deployment only. Database placement is unaffected: each Cluster
# carries its own affinity (docs/cnpg_conventions.md), and nothing here would put Postgres
# on a control-plane node.
_CRITICAL_VALUES: dict[str, object] = {
    "priorityClassName": "system-cluster-critical",
    "tolerations": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule", "operator": "Exists"}],
}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "initial",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
        ),
    )
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata(NAME, "flux-system"),
        spec=HelmRepositorySpec(interval="24h", url="https://cloudnative-pg.github.io/charts"),
    )
    source_ref = HelmReleaseSpecChartSpecSourceRef(
        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
        name=repository.name,
        namespace=repository.metadata.namespace,
    )
    HelmRelease(
        chart,
        "operator",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(
                crds=HelmReleaseSpecInstallCrds.CREATE_REPLACE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
            ),
            upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(chart="cloudnative-pg", version="0.29.0", source_ref=source_ref)
            ),
            values=_CRITICAL_VALUES,
        ),
    )
    HelmRelease(
        chart,
        "barman-cloud",
        metadata=metadata(
            _BARMAN_CLOUD,
            NAMESPACE,
            annotations={"description": "CloudNativePG Barman Cloud plugin for physical backups and WAL archiving."},
        ),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(
                crds=HelmReleaseSpecInstallCrds.CREATE_REPLACE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
            ),
            upgrade=HelmReleaseSpecUpgrade(
                crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE, remediation=HelmReleaseSpecUpgradeRemediation(retries=3)
            ),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(chart=_BARMAN_CLOUD, version="0.8.0", source_ref=source_ref)
            ),
            values=_CRITICAL_VALUES,
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))


def cnpg(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cert_manager: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=NAME, namespace=NAMESPACE
                ),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=_BARMAN_CLOUD, namespace=NAMESPACE
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name=_BARMAN_CLOUD, namespace=NAMESPACE
                ),
            ],
            depends_on=[flux_kustomization_depends_on(cert_manager)],
        ),
    )
