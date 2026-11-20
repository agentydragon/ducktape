"""The kube-system Namespace and disruption budgets for bootstrap controllers."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "kube-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/kube-system"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAME,
        vpa=Vpa.INITIAL,
        # Kyverno's default resourceFilters exclude kube-system, so label-driven
        # diagnostics readers cannot create RoleBindings here. Keep both generic
        # agent and public-coder opt-ins absent until we intentionally add an
        # explicit binding or a narrowly scoped resource-filter exception.
    )
    # Talos, bootstrap Helm and the upstream snapshot-controller kustomize own the Deployments.
    # Flux applies their eviction budgets without a Deployment change or a Cilium Helm upgrade.
    for name, labels in (
        ("coredns", {"k8s-app": "kube-dns"}),
        ("cilium-operator", {"io.cilium/app": "operator", "name": "cilium-operator"}),
        ("snapshot-controller", {"app.kubernetes.io/name": "snapshot-controller"}),
    ):
        k8s.KubePodDisruptionBudget(
            chart,
            f"{name}-disruption-budget",
            metadata=k8s.ObjectMeta(name=name, namespace=NAME),
            spec=k8s.PodDisruptionBudgetSpec(
                min_available=k8s.IntOrString.from_number(1), selector=k8s.LabelSelector(match_labels=labels)
            ),
        )
    return chart


def kube_system(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        wait=None,
        # Kyverno's failurePolicy: Fail webhooks admit the Namespace.
        depends_on=[flux_kustomization_depends_on(kyverno)],
    )
