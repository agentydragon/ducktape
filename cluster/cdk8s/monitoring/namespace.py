"""The `monitoring` Namespace."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/namespace"


def chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "initial",
                "rbac.ducktape.io/agent-readable-logs": "true",
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/audit": "privileged",
                "pod-security.kubernetes.io/warn": "privileged",
            },
        ),
    )
    return chart


def monitoring_namespace(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, "monitoring-namespace", directory, depends_on=[])
