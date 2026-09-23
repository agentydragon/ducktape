"""The `ducktape-flux` Namespace every generated Flux Kustomization lives in, and the
read-only diagnostics grant on it for trusted agent identities.

`cluster/k8s/flux/ducktape-flux` is applied by the bootstrap `flux-system` Kustomization,
not by a node in the generated Flux chart. Hand-written beside the generated output:
`ducktape-gitrepository.yaml`, the namespace-local source for public Ducktape control
objects, since no cdk8s binding covers GitRepository.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.flux import NAMESPACE
from cluster.cdk8s.generation import write_charts

OUTPUT_DIR = "cluster/k8s/flux/ducktape-flux"
_READER = "ducktape-flux-reader"


def _reader_binding(chart: Chart, name: str, *, description: str, subjects: list[k8s.Subject]) -> None:
    k8s.KubeRoleBinding(
        chart,
        name,
        metadata=k8s.ObjectMeta(name=name, namespace=NAMESPACE, annotations={"description": description}),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_READER),
        subjects=subjects,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAMESPACE, disable_resource_name_hashes=True)
    # A control-plane boundary only: managed workloads retain their own namespaces.
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE, labels={"name": NAMESPACE}))
    # Only the two public control-plane CRDs in this namespace. In particular no access to
    # the controller-only SOPS key Secret, ConfigMaps, Pods, logs, exec, or writes.
    k8s.KubeRole(
        chart,
        "reader",
        metadata=k8s.ObjectMeta(
            name=_READER,
            namespace=NAMESPACE,
            annotations={"description": "Read-only public Ducktape Flux diagnostics."},
        ),
        rules=[
            k8s.PolicyRule(
                api_groups=["kustomize.toolkit.fluxcd.io"], resources=["kustomizations"], verbs=["get", "list", "watch"]
            ),
            k8s.PolicyRule(
                api_groups=["source.toolkit.fluxcd.io"], resources=["gitrepositories"], verbs=["get", "list", "watch"]
            ),
        ],
    )
    _reader_binding(
        chart,
        "public-coder-agent-ducktape-flux-reader",
        description="Binds the public-coder access profile to public Ducktape Flux diagnostics.",
        subjects=[
            k8s.Subject(kind="Group", name="haku:access-profile:public-coder", api_group="rbac.authorization.k8s.io")
        ],
    )
    _reader_binding(
        chart,
        "haku-ducktape-flux-reader",
        description="Binds the Haku OIDC group to public Ducktape Flux diagnostics.",
        subjects=[
            k8s.Subject(kind="Group", name="oidc-ksbx-groups:haku", api_group="rbac.authorization.k8s.io"),
            k8s.Subject(kind="Group", name="haku:access-profile:haku", api_group="rbac.authorization.k8s.io"),
            k8s.Subject(kind="ServiceAccount", name="haku", namespace="haku-sandbox"),
        ],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
