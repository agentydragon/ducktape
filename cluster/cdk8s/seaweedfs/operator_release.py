"""The SeaweedFS operator's Helm release, and the chart repository it installs from.

The chart version is also the version of the CRDs the typed bindings are generated from
(`seaweed_*_crd` in MODULE.bazel); keep them in step.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
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
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import namespace

NAME = "seaweedfs-operator"
OUTPUT_DIR = "cluster/k8s/seaweedfs/operator"
_REPOSITORY_NAMESPACE = "flux-system"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    HelmRepository(
        chart,
        "repository",
        metadata=metadata(NAME, _REPOSITORY_NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://seaweedfs.github.io/seaweedfs-operator/"),
    )
    HelmRelease(
        chart,
        "release",
        metadata=metadata(NAME, namespace.NAME),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(
                remediation=HelmReleaseSpecInstallRemediation(retries=3), crds=HelmReleaseSpecInstallCrds.CREATE_REPLACE
            ),
            upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=NAME,
                    version="0.1.42",  # operator v1.0.39 (latest stable as of 2026-09-14)
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=NAME,
                        namespace=_REPOSITORY_NAMESPACE,
                    ),
                    interval="12h",
                )
            ),
            values={
                # The operator controller itself is lightweight; let it sit anywhere a worker
                # can host it (no kimsufi pinning needed for the controller). Operator pods are
                # not data-path, so anti-affinity is unnecessary too. It watches all namespaces
                # by default.
                "replicaCount": 1,
                # Webhooks are disabled by default in the upstream chart; SeaweedFS docs strongly
                # recommend enabling them once the cert-manager integration is verified. Leaving
                # disabled for the trial -- webhook validation provides nicer errors but isn't
                # critical.
                "webhook": {"enabled": False},
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def seaweedfs_operator(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_namespace: Kustomization
) -> Kustomization:
    name = "seaweedfs-operator"
    return flux_kustomization(
        chart,
        name,
        artifact,
        suspend=False,
        depends_on=[flux_kustomization_depends_on(seaweedfs_namespace)],
        timeout="5m",
    )
