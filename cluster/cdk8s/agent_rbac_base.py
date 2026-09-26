"""agent-rbac-base: the claude-sandbox namespace (its quota, limits, admin Role, bindings and
janitor) and the shared agent-facing ClusterRoles other directories bind. Permissions and
bindings: cluster/docs/agent_rbac.md.

Every Role is a tier-2 `k8s.KubeRole`/`k8s.KubeClusterRole`: rules keep the exact grouping they
are reviewed in.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from kyverno_cleanuppolicy_crds.io.kyverno import (
    CleanupPolicy,
    CleanupPolicySpec,
    CleanupPolicySpecConditions,
    CleanupPolicySpecConditionsAll,
    CleanupPolicySpecConditionsAllOperator,
    CleanupPolicySpecMatch,
    CleanupPolicySpecMatchAny,
    CleanupPolicySpecMatchAnyResources,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "agent-rbac-base"
NAMESPACE = "claude-sandbox"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/agent-rbac-base"

_RBAC_GROUP = "rbac.authorization.k8s.io"
_READ = ["get", "list", "watch"]
_SANDBOX_USERS = "oidc-ksbx-groups:kubectl-sandbox-users"


def _quantities(values: dict[str, str]) -> dict[str, k8s.Quantity]:
    return {key: k8s.Quantity.from_string(value) for key, value in values.items()}


def _read(api_group: str, *resources: str) -> k8s.PolicyRule:
    return k8s.PolicyRule(api_groups=[api_group], resources=list(resources), verbs=_READ)


def _cluster_role(chart: Chart, name: str, *rules: k8s.PolicyRule) -> None:
    k8s.KubeClusterRole(chart, name, metadata=k8s.ObjectMeta(name=name), rules=list(rules))


def _add_sandbox(chart: Chart) -> None:
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={"name": NAMESPACE, "environment": "development", "goldilocks.fairwinds.com/enabled": "true"},
        ),
    )
    k8s.KubeResourceQuota(
        chart,
        "quota",
        metadata=k8s.ObjectMeta(name="claude-sandbox-quota", namespace=NAMESPACE),
        spec=k8s.ResourceQuotaSpec(
            hard=_quantities(
                {
                    "requests.cpu": "8",
                    "requests.memory": "16Gi",
                    "limits.cpu": "8",
                    "limits.memory": "16Gi",
                    "pods": "50",
                    "services": "30",
                    "configmaps": "30",
                    "persistentvolumeclaims": "30",
                    "requests.storage": "50Gi",
                }
            )
        ),
    )
    k8s.KubeLimitRange(
        chart,
        "limits",
        metadata=k8s.ObjectMeta(name="claude-sandbox-limits", namespace=NAMESPACE),
        spec=k8s.LimitRangeSpec(
            limits=[
                # Default limits for containers without specified limits
                k8s.LimitRangeItem(
                    type="Container",
                    max=_quantities({"cpu": "4", "memory": "8Gi"}),
                    min=_quantities({"cpu": "10m", "memory": "16Mi"}),
                    default=_quantities({"cpu": "500m", "memory": "512Mi"}),
                    default_request=_quantities({"cpu": "100m", "memory": "128Mi"}),
                ),
                k8s.LimitRangeItem(type="Pod", max=_quantities({"cpu": "8", "memory": "16Gi"})),
            ]
        ),
    )
    k8s.KubeRole(
        chart,
        "admin-role",
        metadata=k8s.ObjectMeta(name="claude-sandbox-admin", namespace=NAMESPACE),
        rules=[
            # Full access within claude-sandbox namespace (including secrets)
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
            k8s.PolicyRule(
                api_groups=["postgresql.cnpg.io"], resources=["clusters", "publications", "subscriptions"], verbs=["*"]
            ),
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "admin-binding",
        metadata=k8s.ObjectMeta(name="claude-sandbox-admin", namespace=NAMESPACE),
        role_ref=k8s.RoleRef(api_group=_RBAC_GROUP, kind="Role", name="claude-sandbox-admin"),
        subjects=[k8s.Subject(kind="Group", name=_SANDBOX_USERS, api_group=_RBAC_GROUP)],
    )
    k8s.KubeRoleBinding(
        chart,
        "ollama-consumer-binding",
        metadata=k8s.ObjectMeta(name="claude-ollama-consumer", namespace=NAMESPACE),
        role_ref=k8s.RoleRef(api_group=_RBAC_GROUP, kind="ClusterRole", name="ollama-api-consumer"),
        subjects=[k8s.Subject(kind="Group", name=_SANDBOX_USERS, api_group=_RBAC_GROUP)],
    )
    # The sandbox is ephemeral by contract: agents get full CRUD and a shared quota, and nothing
    # here is GitOps-managed, so forgotten experiments otherwise linger and pin the quota forever.
    # Reap anything older than 7 days. CronJob is included deliberately -- an orphaned CronJob is
    # self-renewing litter that keeps spawning fresh Jobs. Owned children (ReplicaSets, Job pods) go
    # via cascade when their parent is reaped. Delete RBAC comes from the two aggregated
    # ClusterRoles in kyverno/policies/ (cleanup-controller-{jobs,workloads}).
    CleanupPolicy(
        chart,
        "janitor",
        metadata=metadata("sandbox-janitor", NAMESPACE),
        spec=CleanupPolicySpec(
            schedule="20 * * * *",
            match=CleanupPolicySpecMatch(
                any=[
                    CleanupPolicySpecMatchAny(
                        resources=CleanupPolicySpecMatchAnyResources(
                            kinds=["Pod", "Deployment", "StatefulSet", "Job", "CronJob", "Service"]
                        )
                    )
                ]
            ),
            conditions=CleanupPolicySpecConditions(
                all=[
                    CleanupPolicySpecConditionsAll(
                        key="{{ time_since('', '{{ target.metadata.creationTimestamp }}', '') }}",
                        operator=CleanupPolicySpecConditionsAllOperator.GREATER_THAN,
                        value="168h",
                    )
                ]
            ),
        ),
    )


def _add_cluster_roles(chart: Chart) -> None:
    _cluster_role(
        chart,
        "cluster-diagnostics-reader",
        _read(
            "",
            "nodes",
            "namespaces",
            "pods",
            "pods/status",
            "services",
            "endpoints",
            "persistentvolumeclaims",
            "persistentvolumes",
            "resourcequotas",
            "limitranges",
            "events",
        ),
        _read("apps", "deployments", "replicasets", "statefulsets", "daemonsets"),
        _read("batch", "jobs", "cronjobs"),
        _read("autoscaling", "horizontalpodautoscalers"),
        _read("autoscaling.k8s.io", "verticalpodautoscalers"),
        _read("policy", "poddisruptionbudgets"),
        _read("networking.k8s.io", "ingresses", "ingressclasses", "networkpolicies"),
        _read("storage.k8s.io", "storageclasses", "volumeattachments"),
        _read("discovery.k8s.io", "endpointslices"),
        _read("scheduling.k8s.io", "priorityclasses"),
        _read("coordination.k8s.io", "leases"),
        _read("apiextensions.k8s.io", "customresourcedefinitions"),
        _read("cert-manager.io", "certificates", "certificaterequests", "issuers", "clusterissuers"),
        _read(_RBAC_GROUP, "roles", "rolebindings", "clusterroles", "clusterrolebindings"),
        # SubjectAccessReview / LocalSubjectAccessReview answer "could <subject> do <action>?"
        # directly from the apiserver's authorizer -- the authoritative complement to reading the
        # Role/binding graph above (e.g. "why is this ServiceAccount's pods/exec denied?"). Unlike
        # create on nodes/proxy below, this "create" grants nothing: a review persists no object and
        # changes no state -- it is a read-only authorization probe. It reveals only what the RBAC
        # objects readable above already imply, so it stays secret-free and adds no ability to act.
        k8s.PolicyRule(
            api_groups=["authorization.k8s.io"],
            resources=["subjectaccessreviews", "localsubjectaccessreviews"],
            verbs=["create"],
        ),
        _read("admissionregistration.k8s.io", "mutatingwebhookconfigurations", "validatingwebhookconfigurations"),
        _read("source.toolkit.fluxcd.io", "gitrepositories", "helmrepositories", "ocirepositories", "helmcharts"),
        _read("helm.toolkit.fluxcd.io", "helmreleases"),
        # patch is required to set reconcile.fluxcd.io/requestedAt for manual reconciliation
        # triggers. RBAC cannot restrict which fields are patched -- the annotation-only
        # constraint is enforced by the restrict-agent-kustomization-patch Kyverno ClusterPolicy in
        # kyverno/policies.py.
        k8s.PolicyRule(
            api_groups=["kustomize.toolkit.fluxcd.io"], resources=["kustomizations"], verbs=[*_READ, "patch"]
        ),
        _read("metrics.k8s.io", "pods", "nodes"),
        # nodes/proxy allows GET requests proxied through the apiserver to the kubelet HTTP API.
        # Enables: /stats/summary (resource usage), /metrics, /pods, /logs/<filename> (node system
        # logs). get-only: dangerous kubelet operations (exec, run, attach, portforward) use POST ->
        # require the "create" verb, which is intentionally not granted here.
        k8s.PolicyRule(api_groups=[""], resources=["nodes/proxy"], verbs=["get"]),
        _read(
            "monitoring.coreos.com",
            "prometheuses",
            "alertmanagers",
            "servicemonitors",
            "podmonitors",
            "prometheusrules",
        ),
        _read("gateway.networking.k8s.io", "gateways", "httproutes", "tlsroutes", "grpcroutes"),
        _read("postgresql.cnpg.io", "clusters"),
        _read("infra.contrib.fluxcd.io", "terraforms"),
        _read("image.toolkit.fluxcd.io", "imagepolicies", "imagerepositories", "imageupdateautomations"),
        _read("notification.toolkit.fluxcd.io", "receivers", "alerts", "providers"),
        _read("cilium.io", "ciliumnetworkpolicies", "ciliumclusterwidenetworkpolicies"),
        _read("kyverno.io", "clusterpolicies", "policies", "policyreports", "clusterpolicyreports"),
        _read("dns.cav.enablers.ob", "clusterzones", "clusterrrsets"),
        _read("trust.cert-manager.io", "bundles"),
        _read("external-secrets.io", "externalsecrets"),
        # pods/log and configmaps are granted via namespaced RoleBindings to logs-configmaps-reader
        # in monitoring, kube-system, flux-system, grocy-sf, grocy-vallejo, airlock, authentik.
    )
    # Pod logs explicitly classified as safe for durable agent access. Additive to
    # agent-readable-namespace-metadata; grants only the Kubernetes log subresource. A Namespace
    # with the agent-readable-logs label receives both bindings.
    _cluster_role(
        chart, "agent-readable-namespace-logs", k8s.PolicyRule(api_groups=[""], resources=["pods/log"], verbs=["get"])
    )
    # Read-only namespace state explicitly classified as safe for durable agent access.
    #
    # Deliberately excludes Secrets, External Secrets/SecretStores, pod logs, and every write or
    # exec-like subresource. ConfigMaps and controller CRs are included only when the GitOps-owned
    # Namespace opts in, asserting that its non-secret configuration and workload metadata are not
    # sensitive.
    _cluster_role(
        chart,
        "agent-readable-namespace-metadata",
        _read("", "pods", "services", "configmaps", "persistentvolumeclaims", "events"),
        _read("apps", "deployments", "replicasets", "statefulsets", "daemonsets"),
        _read("batch", "jobs", "cronjobs"),
        _read("autoscaling", "horizontalpodautoscalers"),
        _read("autoscaling.k8s.io", "verticalpodautoscalers"),
        _read("policy", "poddisruptionbudgets"),
        # Flux image promotion state. These objects expose image names, tags, revisions, and
        # policy status, but not registry credentials or Secret data.
        _read("image.toolkit.fluxcd.io", "imagerepositories", "imagepolicies", "imageupdateautomations"),
        _read("networking.k8s.io", "ingresses", "networkpolicies"),
        _read("gateway.networking.k8s.io", "gateways", "httproutes", "tlsroutes", "grpcroutes"),
    )
    _cluster_role(chart, "logs-configmaps-reader", _read("", "pods/log", "configmaps"))
    _cluster_role(
        chart,
        "namespace-diagnostics-reader",
        _read("", "pods", "pods/log", "services", "configmaps", "persistentvolumeclaims", "events"),
        _read("apps", "deployments", "replicasets", "statefulsets"),
        _read("batch", "jobs", "cronjobs"),
    )
    _cluster_role(chart, "secrets-reader", _read("", "secrets"))


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _add_sandbox(chart)
    _add_cluster_roles(chart)
    return chart


def claude_rbac(
    flux_chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, root: Path, kyverno_policies: Kustomization
) -> Kustomization:
    write_charts(root, OUTPUT_DIR, chart)
    # TODO: migrate this live Flux object name to agent-rbac-base in a staged
    # change. Renaming it directly would delete the old Kustomization and may prune
    # its inventory before the replacement owns the same RBAC resources.
    name = "claude-rbac"
    return flux_kustomization(
        flux_chart,
        name,
        artifact,
        retry_interval=None,
        wait=None,
        timeout="2m",
        depends_on=[flux_kustomization_depends_on(kyverno_policies)],
        health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name=NAMESPACE)],
        description=(
            "Lightweight base for agent RBAC. Claude sandbox namespace + shared "
            "ClusterRoles. Must not depend on service or database kustomizations."
        ),
    )
