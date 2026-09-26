"""Haku's identity and write-capable RBAC in haku-sandbox, and the namespace's quota and limits.

Every Role is a tier-2 `k8s.KubeRole`: rules keep the exact grouping they are reviewed in.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.haku.namespace import NAMESPACE
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "haku-rbac"
OUTPUT_DIR = f"{GENERATED_ROOT}/haku/rbac"
SERVICE_ACCOUNT = "haku"
ADMIN_ROLE = "haku-sandbox-admin"

_RBAC_GROUP = "rbac.authorization.k8s.io"


def _quantities(values: dict[str, str]) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeServiceAccount(
        chart,
        "service-account",
        metadata=k8s.ObjectMeta(
            name=SERVICE_ACCOUNT,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Haku's own compute identity in haku-sandbox — carried by whatever Haku workload runs "
                    "here: the sandbox exec-target pods the provisioning MCP hands out "
                    "(cluster/k8s/haku/workspaces/) and the managed-agent worker "
                    "(haku/runtime/managed_agent/self_hosted/deploy/). Bound to haku-sandbox-admin (full "
                    "CRUD in this namespace) by rolebinding-haku.yaml, so `kubectl` works out of the box. "
                    "Deliberately NOT tied to one runtime — it's Haku operating its own compute; the "
                    "exec-target pods don't run an agent harness. cluster-diagnostics read is a follow-up."
                )
            },
        ),
    )
    # Haku's only write-capable RBAC. Deliberately an explicit resource allowlist with NO
    # httproutes/gateways/networkpolicies -- Haku must never open a public door or loosen its own
    # fences. Invariants + full enforcement inventory: haku/docs/security.md.
    k8s.KubeRole(
        chart,
        "admin-role",
        metadata=k8s.ObjectMeta(
            name=ADMIN_ROLE,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Full CRUD within the haku-sandbox compute sandbox (mirrors claude-sandbox-admin): "
                    "pods/log/exec/attach, services, configmaps, secrets, PVCs, events, plus apps and batch "
                    "workloads. The namespace is network-isolated behind its own mitmproxy; secrets here are "
                    "still expected to hold only read-only credentials Haku may fully use. Security model: "
                    "haku/docs/security.md."
                )
            },
        ),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=[
                    # keep-sorted start
                    "configmaps",
                    "events",
                    "persistentvolumeclaims",
                    "pods",
                    "pods/attach",
                    "pods/exec",
                    "pods/log",
                    "pods/portforward",
                    "secrets",
                    "services",
                    # keep-sorted end
                ],
                verbs=["*"],
            ),
            k8s.PolicyRule(
                api_groups=["apps"], resources=["deployments", "statefulsets", "daemonsets", "replicasets"], verbs=["*"]
            ),
            k8s.PolicyRule(api_groups=["batch"], resources=["jobs", "cronjobs"], verbs=["*"]),
        ],
    )
    # Bind Haku's shared compute identity to haku-sandbox-admin -- full CRUD in haku-sandbox. The
    # direct Haku ServiceAccount serves ordinary pods that carry Kubernetes credentials.
    # Console-launched CLI runners carry none; Console authorizes their exact-session Agent bearer by
    # SARing the deploy-owned access-profile group before the separate proxy executes an allowed
    # request.
    k8s.KubeRoleBinding(
        chart,
        "haku-binding",
        metadata=k8s.ObjectMeta(name="haku", namespace=NAMESPACE),
        role_ref=k8s.RoleRef(api_group=_RBAC_GROUP, kind="Role", name=ADMIN_ROLE),
        subjects=[
            k8s.Subject(kind="ServiceAccount", name=SERVICE_ACCOUNT, namespace=NAMESPACE),
            k8s.Subject(kind="Group", name="haku:access-profile:haku", api_group=_RBAC_GROUP),
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "oidc-binding",
        metadata=k8s.ObjectMeta(
            name="haku-oidc-secret-reader",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Binds haku-sandbox-admin to the OIDC group oidc-ksbx-groups:haku — the group "
                    'kube-apiserver derives from the Authentik haku-client-credentials JWT (groups: ["haku"]). '
                    "This is how the Haku background-agent routine drives its own sandbox namespace."
                )
            },
        ),
        role_ref=k8s.RoleRef(api_group=_RBAC_GROUP, kind="Role", name=ADMIN_ROLE),
        subjects=[k8s.Subject(kind="Group", name="oidc-ksbx-groups:haku", api_group=_RBAC_GROUP)],
    )
    # Shared by everything in haku-sandbox: the managed-agent worker, haku-ui, AND the sandbox warm
    # pool (haku/workspaces.py) -- a claimed box runs a LOCAL Bazel build of haku-state (no RBE;
    # --spawn_strategy=local), so it's sized like the haku-ci runner (req 1 / limit 3 CPU, 2/6Gi)
    # rather than a thin exec target. The pool folding in is why this is bigger than a plain agent
    # namespace; bump further only if warm replicas or concurrent claims grow. The LimitRange (max
    # pod 4 CPU / 8Gi) still caps any single pod.
    k8s.KubeResourceQuota(
        chart,
        "quota",
        metadata=k8s.ObjectMeta(name="haku-sandbox-quota", namespace=NAMESPACE),
        spec=k8s.ResourceQuotaSpec(
            hard=_quantities(
                {
                    "requests.cpu": "8",
                    "requests.memory": "16Gi",
                    "limits.cpu": "16",
                    "limits.memory": "32Gi",
                    "pods": "20",
                    "services": "15",
                    "configmaps": "30",
                    "persistentvolumeclaims": "18",
                    "requests.storage": "140Gi",
                }
            )
        ),
    )
    k8s.KubeLimitRange(
        chart,
        "limits",
        metadata=k8s.ObjectMeta(name="haku-sandbox-limits", namespace=NAMESPACE),
        spec=k8s.LimitRangeSpec(
            limits=[
                k8s.LimitRangeItem(
                    type="Container",
                    max=_quantities({"cpu": "4", "memory": "8Gi"}),
                    min=_quantities({"cpu": "10m", "memory": "16Mi"}),
                    default=_quantities({"cpu": "500m", "memory": "512Mi"}),
                    default_request=_quantities({"cpu": "100m", "memory": "128Mi"}),
                ),
                k8s.LimitRangeItem(type="Pod", max=_quantities({"cpu": "4", "memory": "8Gi"})),
            ]
        ),
    )
    return chart


def haku_rbac(
    flux_chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, root: Path, haku_namespace: Kustomization
) -> Kustomization:
    write_charts(root, OUTPUT_DIR, chart)
    return flux_kustomization(
        flux_chart,
        NAME,
        artifact,
        retry_interval=None,
        wait=None,
        timeout="2m",
        depends_on=[flux_kustomization_depends_on(haku_namespace)],
    )
