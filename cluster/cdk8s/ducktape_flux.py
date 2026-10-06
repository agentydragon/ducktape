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
from flux_gitrepository_crds.io.fluxcd.toolkit.source import GitRepositorySpecRef, GitRepositorySpecSecretRef

from cluster.cdk8s import agent_access_profiles as access, namespaces
from cluster.cdk8s.flux import NAMESPACE
from cluster.cdk8s.forgejo import secret_copy
from cluster.cdk8s.generation import write_charts
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
_FINANCE_READER_SECRET = "finance-agent-flux-read-credentials"
_FINANCE_SOURCE = "finance-agent"
_SPEND_POLICY_APPLIER = "plaid-spend-config-applier"
_FINANCE_SPEND_CONFIG_NAMESPACE = "finance-spend-config"
_SPEND_POLICY_SECRET = "plaid-spend-policy"


def _reader_binding(chart: Chart, name: str, *, description: str, subjects: list[k8s.Subject]) -> None:
    k8s.KubeRoleBinding(
        chart,
        name,
        metadata=k8s.ObjectMeta(name=name, namespace=NAMESPACE, annotations={"description": description}),
        role_ref=access.role_ref("ducktape-flux-read"),
        subjects=subjects,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAMESPACE, disable_resource_name_hashes=True)
    # A control-plane boundary only: managed workloads retain their own namespaces.
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND, labels={"name": NAMESPACE})
    # Isolate Finance Flux's Secret-create capability from namespaces containing
    # Ducktape-owned workloads. Finance cannot create namespaces or other resources.
    namespaces.namespace(
        chart,
        "finance-spend-config-namespace",
        name=_FINANCE_SPEND_CONFIG_NAMESPACE,
        vpa=Vpa.DISABLED,
        labels={"name": _FINANCE_SPEND_CONFIG_NAMESPACE},
        annotations={
            "description": "Isolated target for the private Finance spend-policy Secret; namespace and access are Ducktape-owned."
        },
    )
    spend_policy_role = "plaid-spend-config-applier"
    k8s.KubeRole(
        chart,
        "finance-spend-config-applier-role",
        metadata=k8s.ObjectMeta(
            name=spend_policy_role,
            namespace=_FINANCE_SPEND_CONFIG_NAMESPACE,
            annotations={
                "description": (
                    "Allows Finance Flux to create Secrets of any name only in the isolated finance-spend-config "
                    "namespace and get, patch, or update only the named spend-policy Secret; no deletes or other "
                    "resource kinds."
                )
            },
        ),
        rules=[
            # RBAC cannot name-restrict create; the dedicated namespace is the boundary.
            k8s.PolicyRule(api_groups=[""], resources=["secrets"], verbs=["create"]),
            k8s.PolicyRule(
                api_groups=[""],
                resources=["secrets"],
                resource_names=[_SPEND_POLICY_SECRET],
                verbs=["get", "patch", "update"],
            ),
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "finance-spend-config-applier-binding",
        metadata=k8s.ObjectMeta(name=spend_policy_role, namespace=_FINANCE_SPEND_CONFIG_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=spend_policy_role),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_SPEND_POLICY_APPLIER, namespace=NAMESPACE)],
    )
    # Flux's private Finance source gets a distinct repo-scoped read-only credential.
    # The ExternalSecret can read only its named source Secret in `forgejo`.
    reader = secret_copy.reader(chart, NAMESPACE)
    secret_copy.secret_copy(chart, _FINANCE_READER_SECRET, reader=reader)
    GitRepository(
        chart,
        "finance-agent-source",
        metadata=ApiObjectMetadata(name=_FINANCE_SOURCE, namespace=NAMESPACE),
        interval="5m",
        ref=GitRepositorySpecRef(branch="main"),
        sparse_checkout=["config/plaid-spend/"],
        secret_ref=GitRepositorySpecSecretRef(name=_FINANCE_READER_SECRET),
        url="http://forgejo-http.forgejo:3000/finance-agent/finance-agent.git",
    )

    k8s.KubeServiceAccount(
        chart,
        "plaid-spend-config-applier",
        metadata=k8s.ObjectMeta(name=_SPEND_POLICY_APPLIER, namespace=NAMESPACE),
        automount_service_account_token=False,
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
            "agentplane/crds/manifests/",
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
        subjects=[access.PUBLIC_CODER.k8s()],
    )
    _reader_binding(
        chart,
        "haku-ducktape-flux-reader",
        description="Binds the Haku OIDC group to public Ducktape Flux diagnostics.",
        subjects=[subject.k8s() for subject in access.HAKU_IDENTITIES],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
