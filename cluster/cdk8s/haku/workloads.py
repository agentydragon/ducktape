"""haku-state-workloads: the Flux Kustomization that applies Haku-authored manifests (haku-state
`k8s/`) into haku-sandbox, the constrained identity it impersonates, and that identity's Role.

Hand-written beside the output: `gitrepository.yaml` (no source.toolkit.fluxcd.io
GitRepository binding yet).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.haku.namespace import NAMESPACE

NAME = "haku-workloads"
OUTPUT_DIR = "cluster/k8s/haku/workloads"

_FLUX_NAMESPACE = "flux-system"
_RECONCILER = "haku-state-reconciler"
_DEPLOYER_ROLE = "haku-state-workload-deployer"
_WRITE_VERBS = ["get", "list", "watch", "create", "update", "patch", "delete"]


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeServiceAccount(
        chart,
        "reconciler",
        metadata=k8s.ObjectMeta(
            name=_RECONCILER,
            namespace=_FLUX_NAMESPACE,
            annotations={
                "description": (
                    "Impersonation identity for the haku-state-workloads Kustomization. Lives in "
                    "flux-system (kustomize-controller impersonates SAs in the Kustomization's own "
                    "namespace); its only grant is the haku-state-workload-deployer Role in haku-sandbox."
                )
            },
        ),
        automount_service_account_token=False,
    )
    k8s.KubeRole(
        chart,
        "deployer-role",
        metadata=k8s.ObjectMeta(
            name=_DEPLOYER_ROLE,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "What Flux may apply when reconciling Haku's haku-state k8s/ dir — the workload "
                    "subset of haku-sandbox-admin: Deployments/StatefulSets/DaemonSets/ReplicaSets, "
                    "Services/ConfigMaps/PVCs, Jobs/CronJobs, plus its own Flux image automation "
                    "(image.toolkit ImageRepository/ImagePolicy/ImageUpdateAutomation + source.toolkit "
                    "GitRepository). Those are bounded — the controllers that reconcile them only ever "
                    "touch Haku's own images + haku-state. Deliberately NOT secrets (no plaintext-git "
                    "secrets), NOT Gateway-API routes (Kyverno denies those too), and NOT "
                    "notification.toolkit Receivers (a cross-namespace force-reconcile primitive — kept "
                    "operator-owned, see cluster/k8s/haku/ui-image-webhook). So Haku's GitOps path can "
                    "run + ship its own workloads but never widen its own perimeter."
                )
            },
        ),
        rules=[
            k8s.PolicyRule(
                api_groups=[""], resources=["services", "configmaps", "persistentvolumeclaims"], verbs=_WRITE_VERBS
            ),
            k8s.PolicyRule(
                api_groups=["apps"],
                resources=["deployments", "statefulsets", "daemonsets", "replicasets"],
                verbs=_WRITE_VERBS,
            ),
            k8s.PolicyRule(api_groups=["batch"], resources=["jobs", "cronjobs"], verbs=_WRITE_VERBS),
            # Haku's own image automation (bounded -- see the description). NOT Receivers.
            k8s.PolicyRule(
                api_groups=["image.toolkit.fluxcd.io"],
                resources=["imagerepositories", "imagepolicies", "imageupdateautomations"],
                verbs=_WRITE_VERBS,
            ),
            k8s.PolicyRule(api_groups=["source.toolkit.fluxcd.io"], resources=["gitrepositories"], verbs=_WRITE_VERBS),
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "deployer-binding",
        metadata=k8s.ObjectMeta(name=_DEPLOYER_ROLE, namespace=NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_DEPLOYER_ROLE),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_RECONCILER, namespace=_FLUX_NAMESPACE)],
    )
    flux_kustomization(
        chart,
        "haku-state-workloads",
        namespace=_FLUX_NAMESPACE,
        description=(
            "Reconciles Haku-authored workload manifests (haku-state k8s/) into haku-sandbox. "
            "Impersonates the constrained haku-state-reconciler SA, so it may apply only what that SA's "
            "Role allows (Deployments/Services/ConfigMaps/Jobs/CronJobs/PVCs in haku-sandbox — no "
            "Secrets, no routes). Until Haku seeds k8s/ this is NotReady (path not found); that is "
            "expected pre-first-run."
        ),
        spec=KustomizationSpec(
            interval="5m",
            retry_interval="1m",
            timeout="5m",
            path="./k8s",
            prune=True,
            # Don't gate on workload health -- these are Haku's own workloads; their readiness is
            # Haku's concern, not the pipe's.
            wait=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="haku-state"
            ),
            # Force everything into haku-sandbox regardless of what the manifests declare, so Haku
            # can't target another namespace via this pipe.
            target_namespace=NAMESPACE,
            # Apply as the constrained SA (kustomize-controller impersonates
            # system:serviceaccount:flux-system:haku-state-reconciler). RBAC + Kyverno are the
            # fence; this just executes a subset of what Haku itself could do.
            service_account_name=_RECONCILER,
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
