"""The PodMonitor that scrapes Prometheus metrics from every Flux controller in
flux-system.

Without it the `gotk_reconcile_duration_seconds` series is not ingested, which leaves the
flux_reconcile_audit skill partly blind. Flux CR Ready-condition metrics come from
kube-state-metrics `customResourceState` in `monitoring/stack.py`.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import PodMonitorSpecSelector

from cluster.cdk8s.providers.prometheus_operator.pod_monitor import Endpoint, PodMonitor

NAME = "flux-monitoring"
# Every controller (kustomize-, source-, helm-, notification-, image-automation- and
# image-reflector-controller) carries this label (per gotk-components.yaml) and exposes
# /metrics on the `http-prom` named port (8080).
_FLUX_LABELS = {"app.kubernetes.io/part-of": "flux"}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    PodMonitor(
        chart,
        "flux-system",
        metadata=ApiObjectMetadata(name="flux-system", namespace="flux-system", labels=_FLUX_LABELS),
        selector=PodMonitorSpecSelector(match_labels=_FLUX_LABELS),
        pod_metrics_endpoints=[Endpoint.plain(port="http-prom")],
    )
    return chart
