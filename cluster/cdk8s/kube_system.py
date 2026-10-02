"""The kube-system unit: the Namespace's labels (Goldilocks recommendations, applied on pod creation),
and the Hubble UI's ingress NetworkPolicy (`hubble_ui.py`)."""

from __future__ import annotations

from cdk8s import App, Chart

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
