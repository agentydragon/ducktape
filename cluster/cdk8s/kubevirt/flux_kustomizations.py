"""Flux Kustomizations for the cluster/k8s/kubevirt slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthCheckExprs
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def cdi_operator(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "cdi-operator"
    return flux_kustomization(chart, name, artifact, timeout="10m")


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
        # The CDI CR has no Ready condition, so `wait` alone passes it before cdi-operator rolls
        # out cdi-apiserver/-deployment/-uploadproxy. The operator enters phase Deployed with
        # Available=True once none of them is degraded, and phase Error on failure
        # (controller-lifecycle-operator-sdk v0.2.7 pkg/sdk/reconciler/reconciler.go,
        # pkg/sdk/cr-status.go MarkCrHealthyMessage; CDI v1.65.0 embeds its api.Status).
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="cdi.kubevirt.io/v1beta1",
                kind="CDI",
                current=(
                    "has(status.phase) && status.phase == 'Deployed' && has(status.conditions) && "
                    "status.conditions.exists(c, c.type == 'Available' && c.status == 'True')"
                ),
                failed="has(status.phase) && status.phase == 'Error'",
            )
        ],
    )


def kubevirt_operator(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "kubevirt-operator"
    return flux_kustomization(chart, name, artifact, timeout="10m")
