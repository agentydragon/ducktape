"""The `ducktape-flux` Namespace every generated Flux Kustomization lives in, the
namespace-local Ducktape GitRepository they and the ArtifactGenerator read, and the
read-only diagnostics grant on it for trusted agent identities.

`cluster/k8s/flux/ducktape-flux` is applied by the bootstrap `flux-system` Kustomization,
not by a node in the generated Flux chart.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from flux_gitrepository_crds.io.fluxcd.toolkit.source import GitRepositorySpecRef

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import NAMESPACE
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.haku import console_config
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT, PARKED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.flux.git_repository import GitRepository

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/flux/ducktape-flux"
SOURCE_NAME = "ducktape"
# This repository and the branch Flux deploys, for every GitRepository that checks it out.
REPOSITORY_URL = "https://github.com/agentydragon/ducktape.git"
BRANCH = "devel"
TF_GITOPS_ROOT = "tf/gitops"  # tofu-controller Terraform modules (terraform.py)
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
    namespaces.namespace(
        chart, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND, agent_readable=None, labels={"name": NAMESPACE}
    )
    # Intentionally sparse, like flux-system's public-repository source: the source roots
    # the ArtifactGenerator consumes, and the paths public Kustomizations read directly,
    # including the suspended ones' under the parked tree.
    GitRepository(
        chart,
        "source",
        metadata=ApiObjectMetadata(name=SOURCE_NAME, namespace=NAMESPACE),
        interval="1m",
        ref=GitRepositorySpecRef(branch=BRANCH),
        sparse_checkout=[
            f"{HAND_WRITTEN_ROOT}/",
            f"{GENERATED_ROOT}/",
            f"{PARKED_ROOT}/",
            "cluster/charts/browsertrix/",
            "haku/x/dispatch/deploy/",
            "haku/runtime/x/managed_agent/self_hosted/deploy/",
            "haku/runtime/x/managed_agent/anthropic_hosted/terraform/",
            "loom/wayback/deploy/",
            "props/deploy/",
            f"{TF_GITOPS_ROOT}/",
        ],
        url=REPOSITORY_URL,
    )
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
            k8s.Subject(kind="Group", name=console_config.PUBLIC_CODER_GROUP, api_group="rbac.authorization.k8s.io")
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
