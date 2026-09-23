"""Flux Kustomizations for the cluster/k8s/dcgm-exporter slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def dcgm_exporter(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    nvidia_device_plugin: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "dcgm-exporter"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="2m",
        depends_on=flux_kustomization_depends_on_many(
            # RuntimeClass "nvidia" + the containerd nvidia runtime.
            nvidia_device_plugin,
            # PodMonitor CRD ships with kube-prometheus-stack in monitoring-stack.
            # PodMonitor
            monitoring_crds,
        ),
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="DaemonSet", name="dcgm-exporter", namespace="dcgm-exporter"
            )
        ],
    )
