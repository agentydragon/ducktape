"""The PodMonitor that scrapes Prometheus metrics from every Flux controller in
flux-system, and its Flux Kustomization.

Without it the `gotk_reconcile_duration_seconds` series is not ingested, which leaves the
flux_reconcile_audit skill partly blind. Flux CR Ready-condition metrics come from
kube-state-metrics `customResourceState` in `monitoring/stack.py`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.prometheus_operator.pod_monitor import Endpoint, PodMonitor

NAME = "flux-monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/flux-monitoring"
# Every controller (kustomize-, source-, helm-, notification-, image-automation- and
# image-reflector-controller) carries this label (per gotk-components.yaml) and exposes
# /metrics on the `http-prom` named port (8080).
_FLUX_LABELS = {"app.kubernetes.io/part-of": "flux"}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    PodMonitor(
        chart,
        "flux-system",
        metadata=metadata("flux-system", "flux-system", labels=_FLUX_LABELS),
        selector=_FLUX_LABELS,
        pod_metrics_endpoints=[Endpoint.plain(port="http-prom")],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def flux_monitoring(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="2m",
        depends_on=[
            # PodMonitor CRD ships with kube-prometheus-stack in monitoring-stack.
            # PodMonitor
            flux_kustomization_depends_on(monitoring_crds)
        ],
    )
