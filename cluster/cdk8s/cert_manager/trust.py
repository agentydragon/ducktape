"""trust-manager: its Namespace, HelmRepository and HelmRelease.

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
    HelmReleaseSpecInstallRemediation,
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "trust-manager"
NAMESPACE = "cert-manager-trust"
OUTPUT_DIR = "cluster/k8s/cert-manager/trust"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE))
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata("cert-manager", NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://charts.jetstack.io"),
    )
    HelmRelease(
        chart,
        "release",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart="trust-manager",
                    version="0.25.*",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                )
            ),
            values={
                "replicaCount": 1,
                # Backs a failurePolicy: Fail webhook on Bundle, so its absence rejects writes
                # rather than degrading. Same treatment as the other blocking-webhook backends.
                "priorityClassName": "system-cluster-critical",
                "tolerations": [
                    {"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule", "operator": "Exists"}
                ],
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def cert_manager_trust(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cert_manager: Kustomization, kyverno: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "cert-manager-trust",
        artifact,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            cert_manager,
            # Kyverno VWC must be operational before creating resources
            kyverno,
        ),
    )
