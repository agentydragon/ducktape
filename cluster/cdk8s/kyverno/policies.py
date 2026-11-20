"""The cluster's Kyverno ClusterPolicies and the RBAC aggregated into Kyverno's
controllers so the policies here and elsewhere are admitted.

One chart, and so one file, per policy: //cluster/cdk8s/kyverno synthesizes each
constructor alone before its tests run the `kyverno` CLI against fixtures. Fields
the ClusterPolicy CRD leaves untyped (`preconditions`, `deny.conditions`,
`patchStrategicMerge`, `generate.data`) are plain dicts.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from kyverno_clusterpolicy_crds.io.kyverno import (
    ClusterPolicy,
    ClusterPolicySpec,
    ClusterPolicySpecRules,
    ClusterPolicySpecRulesExclude,
    ClusterPolicySpecRulesExcludeAny,
    ClusterPolicySpecRulesExcludeAnyResources,
    ClusterPolicySpecRulesExcludeAnySubjects,
    ClusterPolicySpecRulesMatchAnyResources,
    ClusterPolicySpecRulesMatchAnyResourcesOperations,
    ClusterPolicySpecRulesMutate,
    ClusterPolicySpecValidationFailureAction,
)

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.kyverno.cluster_policy import Validate, match_resources

OUTPUT_DIR = f"{GENERATED_ROOT}/kyverno/policies"

_CREATE = ClusterPolicySpecRulesMatchAnyResourcesOperations.CREATE
_UPDATE = ClusterPolicySpecRulesMatchAnyResourcesOperations.UPDATE
_CLEANUP_VERBS = ["get", "list", "watch", "delete"]
_AGGREGATE_TO_CLEANUP_CONTROLLER = {"rbac.kyverno.io/aggregate-to-cleanup-controller": "true"}


def _chart(app: App, name: str) -> Chart:
    return Chart(app, name, disable_resource_name_hashes=True)


def _annotations(
    *, title: str, category: str, severity: str, subject: str, description: str, autogen: bool = True
) -> dict[str, str]:
    return {
        **({} if autogen else {"pod-policies.kyverno.io/autogen-controllers": "none"}),
        "policies.kyverno.io/title": title,
        "policies.kyverno.io/category": category,
        "policies.kyverno.io/severity": severity,
        "policies.kyverno.io/subject": subject,
        "policies.kyverno.io/description": description,
    }


def _cluster_policy_chart(app: App, name: str, *, annotations: dict[str, str], spec: ClusterPolicySpec) -> Chart:
    chart = _chart(app, name)
    ClusterPolicy(chart, "policy", metadata=ApiObjectMetadata(name=name, annotations=annotations), spec=spec)
    return chart


def _cleanup_controller_cluster_role_chart(
    app: App, chart_name: str, role_name: str, *, rules: list[k8s.PolicyRule]
) -> Chart:
    chart = _chart(app, chart_name)
    k8s.KubeClusterRole(
        chart, "role", metadata=k8s.ObjectMeta(name=role_name, labels=_AGGREGATE_TO_CLEANUP_CONTROLLER), rules=rules
    )
    return chart


def require_gitops_chart(app: App) -> Chart:
    """Blocks direct kubectl apply for Deployments/StatefulSets/DaemonSets. Allows Flux
    controllers, operators that create child workloads from GitOps-managed CRs, and
    Stakater Reloader (rolling restarts on secret changes)."""
    exempt_service_accounts = [
        # Flux kustomize-controller, helm-controller, and source-controller (for CRD updates)
        ("kustomize-controller", "flux-system"),
        ("helm-controller", "flux-system"),
        ("source-controller", "flux-system"),
        # The haku-state workload pipe: Flux applies Haku's self-authored workloads by
        # impersonating this SA (Kustomization spec.serviceAccountName), so admission
        # attributes the request to it rather than kustomize-controller. It is a GitOps
        # path, Role-bounded to haku-sandbox (see cluster/cdk8s/haku/workloads.md).
        ("haku-state-reconciler", "flux-system"),
        # Kyverno itself (for admission webhooks)
        ("kyverno-admission-controller", "kyverno"),
        # Operators that create child workloads from CRs deployed via GitOps
        ("monitoring-operator", "monitoring"),
        ("grafana-operator", "monitoring"),
        ("seaweedfs-operator", "seaweedfs"),
        # Authentik (creates outpost Deployments from provider CRs)
        ("authentik", "authentik"),
        # Stakater Reloader (triggers rolling restarts on secret changes)
        ("reloader-reloader", "kube-system"),
    ]
    return _cluster_policy_chart(
        app,
        "require-gitops",
        annotations=_annotations(
            title="Require GitOps",
            category="Best Practices",
            severity="medium",
            subject="Deployment, StatefulSet, DaemonSet",
            description=(
                "Enforces that workload resources (Deployments, StatefulSets, DaemonSets) can only be created or "
                "modified by Flux GitOps controllers. Direct kubectl apply/create/patch is blocked."
            ),
        ),
        # background: false because this policy uses subjects for exclusions,
        # which requires admission context (request.userInfo) not available in background scans
        spec=ClusterPolicySpec(
            background=False,
            # Start in Audit mode - change to Enforce after validation
            validation_failure_action=ClusterPolicySpecValidationFailureAction.AUDIT,
            rules=[
                ClusterPolicySpecRules(
                    name="block-direct-workload-changes",
                    match=match_resources(
                        ClusterPolicySpecRulesMatchAnyResources(
                            kinds=["Deployment", "StatefulSet", "DaemonSet"], operations=[_CREATE, _UPDATE]
                        )
                    ),
                    exclude=ClusterPolicySpecRulesExclude(
                        any=[
                            *(
                                ClusterPolicySpecRulesExcludeAny(
                                    subjects=[
                                        ClusterPolicySpecRulesExcludeAnySubjects(
                                            kind="ServiceAccount", name=name, namespace=namespace
                                        )
                                    ]
                                )
                                for name, namespace in exempt_service_accounts
                            ),
                            # Crossplane installs provider Deployments from GitOps-managed packages.
                            ClusterPolicySpecRulesExcludeAny(
                                subjects=[
                                    ClusterPolicySpecRulesExcludeAnySubjects(
                                        kind="ServiceAccount", name="crossplane", namespace="crossplane-system"
                                    )
                                ],
                                resources=ClusterPolicySpecRulesExcludeAnyResources(namespaces=["crossplane-system"]),
                            ),
                            # System namespaces
                            ClusterPolicySpecRulesExcludeAny(
                                resources=ClusterPolicySpecRulesExcludeAnyResources(
                                    namespaces=["kube-system", "kyverno", "flux-system"]
                                )
                            ),
                        ]
                    ),
                    validate=Validate.deny(
                        message=(
                            "Direct resource creation/modification blocked. All changes must go through GitOps "
                            "(commit to git repository). "
                            "Resource: {{request.object.kind}}/{{request.object.metadata.name}} "
                            "User: {{request.userInfo.username}}"
                        )
                    ),
                )
            ],
        ),
    )


def default_revision_history_limit_chart(app: App) -> Chart:
    """Defaults spec.revisionHistoryLimit to 3 on workloads that don't set it.

    The apiserver default is 10, so every Deployment that omits the field keeps up to 10
    old (scaled-to-zero) ReplicaSets. Across ~138 Deployments that left ~660 dead
    ReplicaSets around, inflating kube-state-metrics: it emits 9 kube_replicaset_*
    series per RS and caches every object, which was a large part of why KSM outgrew its
    memory limit and started OOMKilling. Capping history to 3 lets the Deployment
    controller garbage-collect the excess while keeping a few revisions for rollback.

    Only defaults when the field is unset, so it never fights a workload (or its Helm
    chart / Flux source) that sets revisionHistoryLimit explicitly.

    Admission-only (background: false) because the Kyverno background controller has no
    RBAC to update these workloads. It doesn't need to: matching UPDATE means the
    existing backlog clears on each object's next Flux/operator reconcile (an SSA apply
    is an UPDATE admission), after which the controller prunes old ReplicaSets.
    StatefulSets/DaemonSets are covered too (same field, bounds ControllerRevision
    history).
    """
    return _cluster_policy_chart(
        app,
        "default-revision-history-limit",
        annotations=_annotations(
            title="Default revisionHistoryLimit",
            category="Best Practices",
            severity="low",
            subject="Deployment, StatefulSet, DaemonSet",
            description=(
                "Sets spec.revisionHistoryLimit to 3 on Deployments, StatefulSets, and DaemonSets that do not "
                "specify it, to bound accumulated revision history (dead ReplicaSets / ControllerRevisions) and "
                "the kube-state-metrics series and memory that track them."
            ),
        ),
        spec=ClusterPolicySpec(
            admission=True,
            background=False,
            rules=[
                ClusterPolicySpecRules(
                    name="default-revision-history-limit",
                    match=match_resources(
                        ClusterPolicySpecRulesMatchAnyResources(
                            kinds=["Deployment", "StatefulSet", "DaemonSet"], operations=[_CREATE, _UPDATE]
                        )
                    ),
                    preconditions={
                        "all": [
                            # Fire only when the field is unset (null coalesces to the -1 sentinel).
                            {
                                "key": "{{ request.object.spec.revisionHistoryLimit || `-1` }}",
                                "operator": "Equals",
                                "value": -1,
                            }
                        ]
                    },
                    mutate=ClusterPolicySpecRulesMutate(patch_strategic_merge={"spec": {"revisionHistoryLimit": 3}}),
                )
            ],
        ),
    )


def default_disable_service_links_chart(app: App) -> Chart:
    """Disables Kubernetes' legacy Service-to-environment-variable injection by default.

    Pod-only and CREATE-only deliberately: mutating controller templates would create
    Flux/SSA ownership conflicts, while every Pod (including Pods created by custom
    operators) still passes through admission.
    """
    return _cluster_policy_chart(
        app,
        "default-disable-service-links",
        annotations=_annotations(
            autogen=False,
            title="Default Service Links off",
            category="Best Practices",
            severity="medium",
            subject="Pod",
            description=(
                "Sets enableServiceLinks to false on newly created Pods to prevent legacy Service environment "
                "variables from colliding with application settings."
            ),
        ),
        spec=ClusterPolicySpec(
            admission=True,
            background=False,
            rules=[
                ClusterPolicySpecRules(
                    name="default-disable-service-links",
                    match=match_resources(ClusterPolicySpecRulesMatchAnyResources(kinds=["Pod"], operations=[_CREATE])),
                    mutate=ClusterPolicySpecRulesMutate(patch_strategic_merge={"spec": {"enableServiceLinks": False}}),
                )
            ],
        ),
    )


def cleanup_controller_jobs_chart(app: App) -> Chart:
    # Aggregates into the kyverno cleanup controller's ClusterRole. Required before
    # any CleanupPolicy can target Jobs: kyverno validates at policy admission that
    # the cleanup controller holds delete permission for the matched kind. First
    # consumer: gaffer-private's wedged-Job reaper in the thrive-scraper namespace.
    return _cleanup_controller_cluster_role_chart(
        app,
        "clusterrole-cleanup-controller-jobs",
        "kyverno-cleanup-controller-jobs",
        rules=[k8s.PolicyRule(api_groups=["batch"], resources=["jobs"], verbs=_CLEANUP_VERBS)],
    )


def cleanup_controller_workloads_chart(app: App) -> Chart:
    """Consumers: the claude-sandbox janitor (agents/agent-rbac-base/) and the props
    finished-pod reaper (props/app/) — kyverno rejects a CleanupPolicy at admission
    unless the controller can delete every kind it matches."""
    return _cleanup_controller_cluster_role_chart(
        app,
        "clusterrole-cleanup-controller-workloads",
        "kyverno-cleanup-controller-workloads",
        rules=[
            k8s.PolicyRule(api_groups=[""], resources=["pods", "services"], verbs=_CLEANUP_VERBS),
            k8s.PolicyRule(api_groups=["apps"], resources=["deployments", "statefulsets"], verbs=_CLEANUP_VERBS),
            k8s.PolicyRule(api_groups=["batch"], resources=["cronjobs"], verbs=_CLEANUP_VERBS),
        ],
    )


def cleanup_controller_sandboxes_chart(app: App) -> Chart:
    return _cleanup_controller_cluster_role_chart(
        app,
        "clusterrole-cleanup-controller-sandboxes",
        "kyverno-cleanup-controller-sandboxes",
        rules=[
            k8s.PolicyRule(api_groups=["agents.x-k8s.io"], resources=["sandboxes"], verbs=_CLEANUP_VERBS),
            k8s.PolicyRule(
                api_groups=["extensions.agents.x-k8s.io"], resources=["sandboxclaims"], verbs=_CLEANUP_VERBS
            ),
        ],
    )


CHARTS = (
    require_gitops_chart,
    default_revision_history_limit_chart,
    default_disable_service_links_chart,
    cleanup_controller_jobs_chart,
    cleanup_controller_workloads_chart,
    cleanup_controller_sandboxes_chart,
)


def kyverno_policies(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        "kyverno-policies",
        directory,
        depends_on=[
            # Policies require Kyverno CRDs to be installed
            flux_kustomization_depends_on(kyverno)
        ],
        interval="5m",
        timeout="2m",
    )
