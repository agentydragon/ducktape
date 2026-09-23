"""Flux Kustomizations for the cluster/k8s/kubevirt slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def cdi_operator(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "cdi-operator"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="cdi-operator", namespace="cdi"
            )
        ],
    )


def cdi(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cdi_operator: Kustomization,
    local_path_provisioner: Kustomization,
) -> Kustomization:
    name = "cdi"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(cdi_operator, local_path_provisioner),
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="cdi-apiserver", namespace="cdi"
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="cdi-deployment", namespace="cdi"
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="cdi-uploadproxy", namespace="cdi"
            ),
        ],
    )


def kubevirt_operator(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "kubevirt-operator"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="virt-operator", namespace="kubevirt"
            )
        ],
    )
