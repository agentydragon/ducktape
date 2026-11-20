"""virt-operator and cdi-operator, installed from their pinned upstream release manifests: each
directory's `kustomization.yaml` names the release URL and patches it
(`cluster/cdk8s/kubevirt/README.md`)."""

from __future__ import annotations

from cdk8s import App, Chart, Yaml
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Json6902Patch, PatchTarget
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
# TODO(https://github.com/agentydragon/ducktape/issues/18159): Simplify these availability and placement patches.
VIRT_OPERATOR_AVAILABILITY = Json6902Patch(
    patch=Yaml.stringify(
        [
            {
                "op": "add",
                "path": "/spec/template/spec/affinity/podAntiAffinity/requiredDuringSchedulingIgnoredDuringExecution",
                "value": [
                    {
                        "labelSelector": {"matchLabels": {"kubevirt.io": "virt-operator"}},
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ],
            },
            {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": "virt-operator"},
            {
                # The metrics readiness probe uses the same serving certificate on a separate port.
                "op": "add",
                "path": "/spec/template/spec/containers/0/startupProbe",
                "value": {"tcpSocket": {"port": 8444}, "periodSeconds": 5, "failureThreshold": 60},
            },
        ]
    ),
    target=PatchTarget(kind="Deployment", name="virt-operator", namespace=KUBEVIRT_NAMESPACE),
)


def virt_operator_chart(app: App) -> Chart:
    chart = Chart(app, "virt-operator", disable_resource_name_hashes=True)
    # The upstream release has two admission-serving replicas and owns their Deployment.
    k8s.KubePodDisruptionBudget(
        chart,
        "disruption-budget",
        metadata=k8s.ObjectMeta(name="virt-operator", namespace=KUBEVIRT_NAMESPACE),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(1),
            selector=k8s.LabelSelector(match_labels={"kubevirt.io": "virt-operator"}),
        ),
    )
    return chart


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
