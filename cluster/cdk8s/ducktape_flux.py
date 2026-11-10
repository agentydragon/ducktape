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
# Clone timeout for the `GitRepository` objects this generator writes for this repository. A bare clone
# is ~519 MB and takes ~60 seconds, which is exactly the `GitRepository.spec.timeout` default, so leaving
# it unset makes every fetch a race the source usually loses. One value because all four sources of this
# repository clone the same history; the full measurement and reasoning are on the `source`
# GitRepository below, and the hand-written bootstrap source carries the same 5m in
# cluster/k8s/flux/flux-system/gotk-sync.yaml.
CLONE_TIMEOUT = "5m"
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
        # The `github` Receiver (cluster/generated/flux-webhook) reconciles this source within seconds of
        # every push, so polling is only the net for a lost delivery. flux-system's source carries the
        # same interval (cluster/k8s/flux/flux-system/gotk-sync.yaml); keep the two in step.
        interval="30m",
        # `CLONE_TIMEOUT`, because a clone of this repository does not fit in the 60s that
        # `GitRepository.spec.timeout` defaults to.
        #
        # Measured on 2026-10-15: a bare clone is ~519 MB and takes ~60 seconds, so under the
        # default this source raced its own deadline once a minute and usually lost. On the losing
        # side `source-controller` reports `GitOperationFailed: failed to checkout and determine
        # revision: ... context deadline exceeded` and keeps serving the last artifact it managed
        # to store, which stops 164 generated artifacts and 74 direct Kustomizations from advancing
        # with no PR at fault and nothing else saying so.
        #
        # Not fixed by narrowing `sparseCheckout`: that filters the checked-out working tree, not
        # what git transfers, so the whole history crosses the wire whatever the list says. A
        # partial/shallow clone is what would help and this CRD (source-controller v1.9.5) offers
        # no field for it. The history is the cost, and most of it is paths no longer in the tree:
        # ~233 MB `augur` and ~121 MB `adgn` are deleted, and 74 revisions of MODULE.bazel.lock
        # alone are ~130 MB. Slimming that is the durable fix; this line is what makes the clone
        # fit until someone does.
        #
        # 5m is an order of magnitude over the measurement while still bounding one attempt.
        # Nothing set a clone timeout before this one; the 2m-30m timeouts elsewhere in the tree are
        # `Kustomization.spec.timeout`, how long an apply may take, which is a different knob
        # answering a different question.
        timeout=CLONE_TIMEOUT,
        ref=GitRepositorySpecRef(branch=BRANCH),
        sparse_checkout=[
            f"{HAND_WRITTEN_ROOT}/",
            f"{GENERATED_ROOT}/",
            f"{PARKED_ROOT}/",
            # sparseCheckout accepts directories, so this brings the encrypted shared-credential
            # subtree into the source artifact. ai-session-archive's ArtifactGenerator still
            # copies only session-backup.yaml into its own Kustomize directory; omitting this
            # source would make that exact-file copy fail.
            "secrets/shared/",
            "agentplane/crds/manifests/",
            "cluster/charts/browsertrix/",
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
        "haku-ducktape-flux-reader",
        description="Binds the Haku OIDC group to public Ducktape Flux diagnostics.",
        subjects=[subject.k8s() for subject in access.HAKU_IDENTITIES],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
