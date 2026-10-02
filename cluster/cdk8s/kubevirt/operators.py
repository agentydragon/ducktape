"""virt-operator and cdi-operator, installed from their pinned upstream release manifests: each
directory's `kustomization.yaml` names the release URL and patches it
(`cluster/cdk8s/kubevirt/README.md`)."""

from __future__ import annotations

from cdk8s import App, Chart, Yaml

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Json6902Patch, Kustomization, PatchTarget, RenderedDirectory, flux_kustomization
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

VIRT_OPERATOR_DIR = f"{GENERATED_ROOT}/kubevirt/operator"
CDI_OPERATOR_DIR = f"{GENERATED_ROOT}/kubevirt/cdi-operator"
# MODULE.bazel's `kubevirt_operator_bundle` and `cdi_operator_bundle` pin these same assets, by
# SHA-256, for the CRD bindings; keep each pair of URLs equal.
VIRT_OPERATOR_RELEASE = "https://github.com/kubevirt/kubevirt/releases/download/v1.8.2/kubevirt-operator.yaml"
CDI_OPERATOR_RELEASE = (
    "https://github.com/kubevirt/containerized-data-importer/releases/download/v1.65.0/cdi-operator.yaml"
)
# The namespaces the release manifests create.
KUBEVIRT_NAMESPACE = "kubevirt"
_CDI_NAMESPACE = "cdi"


def _on_hil(deployment: str, namespace: str) -> Json6902Patch:
    """Schedules an operator Deployment in the `hil` region only. JSON6902 `add` replaces the
    upstream `kubernetes.io/os: linux` nodeSelector, which a strategic merge would keep."""
    return Json6902Patch(
        patch=Yaml.stringify(
            [
                {
                    "op": "add",
                    "path": "/spec/template/spec/nodeSelector",
                    "value": {"topology.kubernetes.io/region": "hil"},
                }
            ]
        ),
        target=PatchTarget(kind="Deployment", name=deployment, namespace=namespace),
    )


VIRT_OPERATOR_ON_HIL = _on_hil("virt-operator", KUBEVIRT_NAMESPACE)
CDI_OPERATOR_ON_HIL = _on_hil("cdi-operator", _CDI_NAMESPACE)


def virt_operator_namespace_patch(app: App) -> Chart:
    chart = Chart(app, "namespace-patch", disable_resource_name_hashes=True)
    namespaces.namespace_patch(
        chart,
        "namespace",
        name=KUBEVIRT_NAMESPACE,
        vpa=Vpa.DISABLED,
        # Upstream sets `enforce: privileged`; audit and warn match it.
        labels={"pod-security.kubernetes.io/audit": "privileged", "pod-security.kubernetes.io/warn": "privileged"},
    )
    return chart


def cdi_operator_namespace_patch(app: App) -> Chart:
    chart = Chart(app, "namespace-patch", disable_resource_name_hashes=True)
    namespaces.namespace_patch(chart, "namespace", name=_CDI_NAMESPACE, vpa=Vpa.DISABLED)
    return chart


def kubevirt_operator(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, "kubevirt-operator", directory, timeout="10m")


def cdi_operator(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, "cdi-operator", directory, timeout="10m")
