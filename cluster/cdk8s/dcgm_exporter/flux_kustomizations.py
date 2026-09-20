"""Flux Kustomizations for the cluster/k8s/dcgm-exporter slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def dcgm_exporter(chart: Chart, nvidia_device_plugin: Kustomization, monitoring_crds: Kustomization) -> Kustomization:
    name = "dcgm-exporter"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/dcgm-exporter",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=flux_kustomization_depends_on_many(
                # RuntimeClass "nvidia" + the containerd nvidia runtime.
                nvidia_device_plugin,
                # PodMonitor CRD ships with kube-prometheus-stack in monitoring-stack.
                # PodMonitor
                monitoring_crds,
            ),
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="DaemonSet", name="dcgm-exporter", namespace="dcgm-exporter"
                )
            ],
        ),
    )
