"""The budget Namespace and the `budget-namespace` Flux Kustomization owning it."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

OUTPUT_DIR = f"{GENERATED_ROOT}/forgejo/budget-namespace"


def chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name="budget",
            labels={"goldilocks.fairwinds.com/enabled": "true", "goldilocks.fairwinds.com/vpa-update-mode": "auto"},
            annotations={
                # The suspended parked/budget Kustomization may still have this namespace in
                # its inventory. Keep that stale inventory from pruning the active namespace.
                "kustomize.toolkit.fluxcd.io/prune": "disabled"
            },
        ),
    )
    return chart


def budget_namespace(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    name = "budget-namespace"
    return flux_kustomization(
        chart, name, directory, retry_interval=None, wait=None, interval="1h", prune=False, timeout="1m"
    )
