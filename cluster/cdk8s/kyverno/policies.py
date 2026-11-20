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


def require_secret_store_conditions_chart(app: App) -> Chart:
    return _cluster_policy_chart(
        app,
        "require-secret-store-conditions",
        annotations=_annotations(
            title="Require ClusterSecretStore Conditions",
            category="Access Control",
            severity="high",
            subject="ClusterSecretStore",
            description=(
                "A ClusterSecretStore with no `spec.conditions` is usable from every namespace in the cluster. "
                "That is not a mild default here: the ESO ServiceAccount holds cluster-wide secret read, so the "
                "store — not RBAC — is the only thing bounding what an ExternalSecret can pull out of the "
                "store's `remoteNamespace`. An unfenced `kubernetes-flux-system-secret-store` means any "
                "namespace can read the GitHub App, the PATs, the AWS Route 53 credentials and `ci-age-key`. "
                "This requires every store to name at least one namespace in `conditions[].namespaces`, which "
                "rejects the degenerate forms as well: an empty `conditions: []` (ESO treats it exactly like an "
                "absent one), a condition carrying no criteria at all, and a bare `namespaceSelector: {}` — an "
                "empty LabelSelector matches every namespace by the standard Kubernetes convention, so that one "
                "reads as a fence while being the widest setting available. Requiring an explicit list also "
                "rules out selector-based fences, which nothing here uses; relax it deliberately if that changes."
            ),
        ),
        spec=ClusterPolicySpec(
            admission=True,
            # background: true so the Kyverno background controller also evaluates stores
            # that already exist. Enforce only sees what is applied from here on, and an
            # object that predates the policy and is never rewritten is never re-admitted —
            # a background scan is the only thing that would report one. Nothing in CI
            # checks this either; it is enforced here and nowhere else. The rule matches on
            # kind alone and reads no request.userInfo, which is what makes background
            # evaluation possible; the policies here that set background: false do so
            # because they need admission-only context.
            background=True,
            # Enforce: every ClusterSecretStore reaching this cluster now declares
            # conditions, including `kubernetes-seaweedfs-secret-store`, which is applied
            # from gaffer-private and was the last holdout (gaffer-private#383). A store
            # that arrives without them is rejected at admission rather than merely
            # reported, so the fence cannot be dropped by a manifest edit.
            validation_failure_action=ClusterPolicySpecValidationFailureAction.ENFORCE,
            rules=[
                ClusterPolicySpecRules(
                    name="require-conditions",
                    match=match_resources(
                        ClusterPolicySpecRulesMatchAnyResources(
                            kinds=["ClusterSecretStore"], operations=[_CREATE, _UPDATE]
                        )
                    ),
                    validate=Validate.deny(
                        message=(
                            "ClusterSecretStore must name the namespaces allowed to use it in "
                            "spec.conditions[].namespaces; without at least one the store is readable from every "
                            "namespace and the ESO ServiceAccount has cluster-wide secret read. "
                            "Store: {{request.object.metadata.name}} (remoteNamespace "
                            "{{request.object.spec.provider.kubernetes.remoteNamespace || 'n/a'}})."
                        ),
                        conditions={
                            "any": [
                                # Flattened, so this covers an absent `conditions`, an empty list,
                                # a condition with no criteria, an empty `namespaces: []`, and a
                                # bare `namespaceSelector: {}` — which matches every namespace and
                                # is therefore the widest setting, not a fence.
                                {
                                    "key": "{{ request.object.spec.conditions[].namespaces[] || `[]` | length(@) }}",
                                    "operator": "Equals",
                                    "value": 0,
                                }
                            ]
                        },
                    ),
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
    default_disable_service_links_chart,
    require_secret_store_conditions_chart,
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
