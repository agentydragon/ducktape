"""The Flux Kustomization applying the cluster's scrape targets and alert rules that no
application's own unit carries. Each module builds its own chart; `generate_manifests.py` writes
them all into `OUTPUT_DIR`."""

from __future__ import annotations

from cdk8s import Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "platform-monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/platform-monitoring"


def platform_monitoring(chart: Chart, directory: RenderedDirectory, monitoring_crds: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        # The PodMonitor, ServiceMonitor and PrometheusRule CRDs.
        depends_on=flux_kustomization_depends_on_many(monitoring_crds),
    )
