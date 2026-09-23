"""tofu-controller: its HelmRepository and the HelmRelease, which also installs the
`Terraform` CRD.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
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
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "tofu-controller"
NAMESPACE = "flux-system"
OUTPUT_DIR = "cluster/k8s/tofu-controller"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://flux-iac.github.io/tofu-controller"),
    )
    HelmRelease(
        chart,
        "release",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="15m",
            install=HelmReleaseSpecInstall(
                crds=HelmReleaseSpecInstallCrds.CREATE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
            ),
            upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart="tofu-controller",
                    # renovate: datasource=helm depName=tofu-controller registryUrl=https://flux-iac.github.io/tofu-controller
                    version="0.16.5",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                )
            ),
            values={
                # Terraform CRs in flux-system consume ducktape-flux/ducktape.
                "allowCrossNamespaceRefs": True,
                "runner": {
                    "grpc": {"maxMessageSize": 50},  # MB, default 4 — defense against repo growth
                    "serviceAccount": {"annotations": {"eks.amazonaws.com/role-arn": ""}},  # Not needed for on-prem
                },
                # Security context for Terraform runner pods
                "podSecurityContext": {"runAsNonRoot": True, "runAsUser": 65532, "fsGroup": 65532},
                # Resource limits for runner pods
                "resources": {
                    "limits": {"cpu": "1000m", "memory": "1Gi"},
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                },
                "logLevel": "info",
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def tofu_controller(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cert_manager: Kustomization, kyverno: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m0s",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="10m0s",
            wait=True,
            depends_on=flux_kustomization_depends_on_many(cert_manager, kyverno),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=NAME, namespace=NAMESPACE
                ),
                KustomizationSpecHealthChecks(
                    api_version="apiextensions.k8s.io/v1",
                    kind="CustomResourceDefinition",
                    name="terraforms.infra.contrib.fluxcd.io",
                    namespace="",
                ),
            ],
        ),
    )
