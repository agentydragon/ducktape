"""ServiceMonitors for the Cilium agent and Hubble's flow metrics in kube-system."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsScheme,
    ServiceMonitorSpecNamespaceSelector,
    ServiceMonitorSpecSelector,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "cilium-monitoring"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/cilium"


def _labels(name: str) -> dict[str, str]:
    return {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": NAMESPACE}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ServiceMonitor(
        chart,
        "cilium-agent",
        metadata=metadata("cilium-agent", NAMESPACE, labels=_labels("cilium-agent")),
        spec=ServiceMonitorSpec(
            namespace_selector=ServiceMonitorSpecNamespaceSelector(match_names=["kube-system"]),
            selector=ServiceMonitorSpecSelector(match_labels={"app.kubernetes.io/name": "cilium-agent"}),
            endpoints=[
                ServiceMonitorSpecEndpoints(
                    port="metrics", path="/metrics", scheme=ServiceMonitorSpecEndpointsScheme.HTTP, scrape_timeout="10s"
                )
            ],
        ),
    )
    # Hubble flow metrics, enabled by `hubble.metrics` in
    # cluster/terraform/main/cilium-values.yaml. The Cilium chart creates the
    # headless `hubble-metrics` Service only when that list is non-empty, so this
    # produces no targets until the corresponding Helm upgrade has been applied.
    #
    # `k8s-app: hubble` also matches hubble-peer/relay/ui; the `hubble-metrics` port
    # name is what narrows the targets to the metrics Service.
    ServiceMonitor(
        chart,
        "hubble",
        metadata=metadata("hubble", NAMESPACE, labels=_labels("hubble")),
        spec=ServiceMonitorSpec(
            namespace_selector=ServiceMonitorSpecNamespaceSelector(match_names=["kube-system"]),
            selector=ServiceMonitorSpecSelector(match_labels={"k8s-app": "hubble"}),
            endpoints=[
                ServiceMonitorSpecEndpoints(
                    port="hubble-metrics",
                    path="/metrics",
                    scheme=ServiceMonitorSpecEndpointsScheme.HTTP,
                    scrape_timeout="10s",
                )
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def cilium_monitoring(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="2m",
        depends_on=[
            # ServiceMonitor
            flux_kustomization_depends_on(monitoring_crds)
        ],
    )
