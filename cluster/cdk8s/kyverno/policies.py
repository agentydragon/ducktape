"""The cluster's Kyverno ClusterPolicies and the RBAC aggregated into Kyverno's
controllers so the policies here and elsewhere are admitted.

One chart, and so one file, per policy: //cluster/validation/kyverno runs the
`kyverno` CLI against each file alone. Fields the ClusterPolicy CRD leaves untyped
(`preconditions`, `deny.conditions`, `patchStrategicMerge`, `generate.data`) are
plain dicts.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from kyverno_clusterpolicy_crds.io.kyverno import (
    ClusterPolicySpecRules,
    ClusterPolicySpecRulesExclude,
    ClusterPolicySpecRulesExcludeAny,
    ClusterPolicySpecRulesExcludeAnyResources,
    ClusterPolicySpecRulesExcludeAnySubjects,
    ClusterPolicySpecRulesGenerate,
    ClusterPolicySpecRulesMatch,
    ClusterPolicySpecRulesMatchAny,
    ClusterPolicySpecRulesMatchAnyResources,
    ClusterPolicySpecRulesMatchAnyResourcesOperations,
    ClusterPolicySpecRulesMatchAnyResourcesSelector,
    ClusterPolicySpecRulesMatchAnyResourcesSelectorMatchExpressions,
    ClusterPolicySpecRulesMatchAnySubjects,
    ClusterPolicySpecRulesMutate,
    ClusterPolicySpecRulesValidateCelExpressions,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.kyverno import proxy_injection
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.kyverno.cluster_policy import (
    ClusterPolicy,
    Validate,
    ValidationFailureAction,
    match_resources,
)

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


def require_gitops_chart(app: App) -> Chart:
    """Blocks direct kubectl apply for Deployments/StatefulSets/DaemonSets. Allows Flux
    controllers, operators that create child workloads from GitOps-managed CRs, and
    Stakater Reloader (rolling restarts on secret changes)."""
    chart = _chart(app, "require-gitops")
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
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name="require-gitops",
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
        ),
        # background: false because this policy uses subjects for exclusions,
        # which requires admission context (request.userInfo) not available in background scans
        background=False,
        # Start in Audit mode - change to Enforce after validation
        validation_failure_action=ValidationFailureAction.AUDIT,
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
                ).to_spec(),
            )
        ],
    )
    return chart


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
    chart = _chart(app, "default-revision-history-limit")
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name="default-revision-history-limit",
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
        ),
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
    )
    return chart


def default_disable_service_links_chart(app: App) -> Chart:
    """Disables Kubernetes' legacy Service-to-environment-variable injection by default.

    Pod-only and CREATE-only deliberately: mutating controller templates would create
    Flux/SSA ownership conflicts, while every Pod (including Pods created by custom
    operators) still passes through admission.
    """
    chart = _chart(app, "default-disable-service-links")
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name="default-disable-service-links",
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
        ),
        admission=True,
        background=False,
        rules=[
            ClusterPolicySpecRules(
                name="default-disable-service-links",
                match=match_resources(ClusterPolicySpecRulesMatchAnyResources(kinds=["Pod"], operations=[_CREATE])),
                mutate=ClusterPolicySpecRulesMutate(patch_strategic_merge={"spec": {"enableServiceLinks": False}}),
            )
        ],
    )
    return chart


def default_vpa_requests_only_chart(app: App) -> Chart:
    """Defaults goldilocks' VPA resource policy to controlledValues: RequestsOnly on
    every auto-mode namespace.

    VPA scales a container's limits *in proportion to its requests*. A workload whose
    steady-state usage is far below its cold-start need therefore gets its limit dragged
    down with its request, and CFS quota is a hard rate limiter regardless of node load
    — so the container is throttled even on an idle node. On 2026-08-09 that admitted
    both LiteLLM replicas at a 150m CPU limit against a declared 1. Cold start (imports,
    model registration, a Prisma migration) could not finish inside the startup probe
    budget, the probe killed every attempt, and the loop fed itself: a container stuck
    in startup burns no CPU, so the recommendation never recovered. Both replicas
    crashlooped for six hours, the Service lost all backends, and every in-cluster model
    call failed with an immediate EPERM. An audit afterwards found 15 containers running
    a collapsed CPU limit, one of them (paperless) also behind a startup probe.

    RequestsOnly keeps VPA doing the part it is good at — right-sizing requests from
    observed usage — and leaves every limit exactly as the manifest declares.

    Defaulted here rather than per-namespace because goldilocks has no cluster-level
    setting for it: its precedence is workload annotation -> namespace annotation -> a
    hardcoded UpdateModeOff, and its only global flags are
    OnByDefault/DryRun/Include/ExcludeNamespaces/IgnoreControllerKind. Without this
    policy the annotation has to be repeated in every namespace, and a namespace added
    later silently gets the collapsing behaviour — which is exactly how the 15 above
    accumulated.

    Mutating the namespace (goldilocks' *input*) rather than the VPA object it writes:
    goldilocks stays the only writer of the VPA, so there is no controller-versus-webhook
    rewrite loop.

    The +() anchor adds the annotation only when absent, so it never fights a namespace
    that sets its own policy. No namespace currently sets one: a workload whose limit
    comes from a chart preset should declare an explicit resources block instead of
    opting back in to RequestsAndLimits, which only defers the collapse to whichever
    dimension the preset is loosest on.

    Admission-only (background: false), matching default-revision-history-limit:
    matching UPDATE means existing namespaces pick this up on their next Flux reconcile
    (an SSA apply is an UPDATE admission), after which goldilocks rebuilds each VPA with
    the new resourcePolicy.
    """
    chart = _chart(app, "default-vpa-requests-only")
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name="default-vpa-requests-only",
            annotations=_annotations(
                title="Default VPA to RequestsOnly",
                category="Best Practices",
                severity="high",
                subject="Namespace",
                description=(
                    "Adds a goldilocks vpa-resource-policy annotation setting controlledValues: RequestsOnly to "
                    "namespaces running goldilocks VPA in auto mode, so VPA never scales a container's limits down "
                    "in proportion to its requests. Only applied when the namespace does not already declare its "
                    "own policy."
                ),
            ),
        ),
        admission=True,
        background=False,
        rules=[
            ClusterPolicySpecRules(
                name="default-vpa-requests-only",
                match=match_resources(
                    ClusterPolicySpecRulesMatchAnyResources(
                        kinds=["Namespace"],
                        operations=[_CREATE, _UPDATE],
                        selector=ClusterPolicySpecRulesMatchAnyResourcesSelector(
                            match_labels={"goldilocks.fairwinds.com/vpa-update-mode": "auto"}
                        ),
                    )
                ),
                mutate=ClusterPolicySpecRulesMutate(
                    patch_strategic_merge={
                        "metadata": {
                            "annotations": {
                                # Add-if-absent: never overwrites a namespace's own policy.
                                "+(goldilocks.fairwinds.com/vpa-resource-policy)": (
                                    '{"containerPolicies": [{"containerName": "*", '
                                    '"controlledValues": "RequestsOnly"}]}'
                                )
                            }
                        }
                    }
                ),
            )
        ],
    )
    return chart


def restrict_agent_kustomization_patch_chart(app: App) -> Chart:
    chart = _chart(app, "restrict-agent-kustomization-patch")
    kustomization_updates = ClusterPolicySpecRulesMatchAnyResources(
        kinds=["kustomize.toolkit.fluxcd.io/v1/Kustomization"], operations=[_UPDATE]
    )
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name="restrict-agent-kustomization-patch",
            annotations=_annotations(
                title="Restrict Agent Kustomization Patches",
                category="Access Control",
                severity="medium",
                subject="Kustomization",
                description=(
                    "Restricts agent identities (claude-code-web user, kubectl-sandbox-users group) to only "
                    "patching the reconcile.fluxcd.io/requestedAt annotation on Flux Kustomization resources. "
                    "Changes to spec or any other annotation are blocked.\n"
                ),
            ),
        ),
        admission=True,
        background=False,
        validation_failure_action=ValidationFailureAction.ENFORCE,
        rules=[
            ClusterPolicySpecRules(
                name="only-reconcile-annotation",
                match=ClusterPolicySpecRulesMatch(
                    any=[
                        ClusterPolicySpecRulesMatchAny(
                            resources=kustomization_updates,
                            subjects=[ClusterPolicySpecRulesMatchAnySubjects(kind="User", name="claude-code-web")],
                        ),
                        ClusterPolicySpecRulesMatchAny(
                            resources=kustomization_updates,
                            subjects=[
                                ClusterPolicySpecRulesMatchAnySubjects(
                                    kind="Group", name="oidc-ksbx-groups:kubectl-sandbox-users"
                                )
                            ],
                        ),
                    ]
                ),
                validate=Validate.cel(
                    message=(
                        "Agent service accounts may only patch the reconcile.fluxcd.io/requestedAt annotation. "
                        "Changes to spec or other annotations are not permitted.\n"
                    ),
                    expressions=[
                        ClusterPolicySpecRulesValidateCelExpressions(
                            expression="object.spec == oldObject.spec", message="Changes to spec are not permitted."
                        ),
                        ClusterPolicySpecRulesValidateCelExpressions(
                            expression=(
                                "(!has(object.metadata.annotations) ||\n\n"
                                " object.metadata.annotations.all(k,\n"
                                "   k == 'reconcile.fluxcd.io/requestedAt' ||\n"
                                "   (has(oldObject.metadata.annotations) &&\n"
                                "    k in oldObject.metadata.annotations &&\n"
                                "    object.metadata.annotations[k] == oldObject.metadata.annotations[k])))\n"
                                "&& (!has(oldObject.metadata.annotations) ||\n\n"
                                " oldObject.metadata.annotations.all(k,\n"
                                "   k == 'reconcile.fluxcd.io/requestedAt' ||\n"
                                "   (has(object.metadata.annotations) &&\n"
                                "    k in object.metadata.annotations &&\n"
                                "    oldObject.metadata.annotations[k] == object.metadata.annotations[k])))\n"
                            ),
                            message="Only the reconcile.fluxcd.io/requestedAt annotation may be changed.",
                        ),
                    ],
                ).to_spec(),
            )
        ],
    )
    return chart


def restrict_agent_gateway_routes_chart(app: App) -> Chart:
    chart = _chart(app, "restrict-agent-gateway-routes")
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name="restrict-agent-gateway-routes",
            annotations=_annotations(
                title="Restrict Agent Gateway API Routes",
                category="Access Control",
                severity="high",
                subject="HTTPRoute, TLSRoute, TCPRoute, UDPRoute, GRPCRoute, Gateway",
                description=(
                    "Blocks creation/modification of any Gateway API route (HTTPRoute, TLSRoute, TCPRoute, "
                    "UDPRoute, GRPCRoute) or Gateway in the agent-writable sandbox namespaces (claude-sandbox, "
                    "haku-sandbox). The public `cluster-gateway` listeners use `allowedRoutes.namespaces.from: All`, "
                    "so without this fence an agent that gained `httproutes`/`gateways` RBAC could publish a route "
                    "that attaches to the public gateway and bypasses the operator-owned Authentik proxy. Agents "
                    "have no legitimate need to create routes — their sandbox Roles already omit these resources — "
                    "so a blanket deny in those namespaces closes the latent hole structurally without touching any "
                    "existing route (none live in a sandbox namespace). This denylist is preferred over "
                    "per-listener `allowedRoutes` selectors because the public gateway's listener-level "
                    "`allowedRoutes` is affected by Cilium bug #42159 (see cluster/docs/plan.md), and the Selector "
                    "approach would require labeling the built-in `default` and `flux-system` namespaces that host "
                    "critical routes. For Haku this is an enforcement inventory entry in haku/docs/security.md."
                ),
            ),
        ),
        admission=True,
        # background: false because the policy scopes by namespace only (no subject
        # match), so it does not need request.userInfo; admission-time enforcement is
        # what matters for the GitOps + agent threat model.
        background=False,
        validation_failure_action=ValidationFailureAction.ENFORCE,
        rules=[
            ClusterPolicySpecRules(
                name="deny-routes-in-agent-namespaces",
                match=match_resources(
                    ClusterPolicySpecRulesMatchAnyResources(
                        # Bare kind names (not Group/Version/Kind-pinned) so the deny matches
                        # every served apiVersion — an agent can't evade by submitting a route
                        # at v1beta1/v1alpha2 instead of v1. These kind names are unique to the
                        # gateway.networking.k8s.io group, so no disambiguation is needed.
                        kinds=["HTTPRoute", "GRPCRoute", "TLSRoute", "TCPRoute", "UDPRoute", "Gateway"],
                        operations=[_CREATE, _UPDATE],
                        namespaces=["claude-sandbox", "haku-sandbox"],
                    )
                ),
                validate=Validate.deny(
                    message=(
                        "Gateway API routes and Gateways may not be created in agent sandbox namespaces. Public "
                        "ingress is operator-owned: route through the Authentik proxy (HTTPRoutes in the "
                        "`authentik` namespace) instead. "
                        "Resource: {{request.object.kind}}/{{request.object.metadata.name}}"
                    )
                ).to_spec(),
            )
        ],
    )
    return chart


def require_secret_store_conditions_chart(app: App) -> Chart:
    chart = _chart(app, "require-secret-store-conditions")
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name="require-secret-store-conditions",
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
        ),
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
        validation_failure_action=ValidationFailureAction.ENFORCE,
        rules=[
            ClusterPolicySpecRules(
                name="require-conditions",
                match=match_resources(
                    ClusterPolicySpecRulesMatchAnyResources(kinds=["ClusterSecretStore"], operations=[_CREATE, _UPDATE])
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
                ).to_spec(),
            )
        ],
    )
    return chart


def generate_agent_diagnostics_readers_chart(app: App) -> Chart:
    """Grants approved agent identities access in explicitly opted-in namespaces. The
    namespace labels are GitOps-owned data classifications and access grants, not
    merely descriptive metadata."""
    chart = _chart(app, "generate-agent-diagnostics-readers")
    subjects = [
        {"kind": "Group", "name": "oidc-ksbx-groups:haku", "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "Group", "name": "haku:access-profile:haku", "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "ServiceAccount", "name": "haku", "namespace": "haku-sandbox"},
        {"kind": "Group", "name": "oidc-ksbx-groups:kubectl-sandbox-users", "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "Group", "name": "haku:access-profile:public-coder", "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "ServiceAccount", "name": "claude-ai", "namespace": "agentplane-staging"},
    ]

    def namespaces_labeled(label: str) -> ClusterPolicySpecRulesMatchAny:
        return ClusterPolicySpecRulesMatchAny(
            resources=ClusterPolicySpecRulesMatchAnyResources(
                kinds=["Namespace"],
                selector=ClusterPolicySpecRulesMatchAnyResourcesSelector(match_labels={label: "true"}),
            )
        )

    def role_binding(access: str, cluster_role: str) -> ClusterPolicySpecRulesGenerate:
        return ClusterPolicySpecRulesGenerate(
            generate_existing=True,
            synchronize=True,
            api_version="rbac.authorization.k8s.io/v1",
            kind="RoleBinding",
            name=access,
            namespace="{{request.object.metadata.name}}",
            data={
                "metadata": {"labels": {"rbac.ducktape.io/managed-by": "kyverno", "rbac.ducktape.io/access": access}},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": cluster_role},
                "subjects": subjects,
            },
        )

    metadata_label = "rbac.ducktape.io/agent-readable-metadata"
    logs_label = "rbac.ducktape.io/agent-readable-logs"
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name="generate-agent-diagnostics-readers",
            annotations=_annotations(
                title="Generate agent diagnostics readers",
                category="Access Control",
                severity="high",
                subject="Namespace, RoleBinding",
                description=(
                    "Generates namespaced, read-only metadata and optional pod-log RoleBindings when a "
                    "GitOps-managed Namespace opts in."
                ),
            ),
        ),
        background=True,
        rules=[
            # Either label grants the common metadata baseline. The logs label means
            # metadata plus logs, so a namespace needs only one classification label.
            ClusterPolicySpecRules(
                name="generate-agent-readable-metadata",
                match=ClusterPolicySpecRulesMatch(
                    any=[namespaces_labeled(metadata_label), namespaces_labeled(logs_label)]
                ),
                generate=role_binding("agent-readable-metadata", "agent-readable-namespace-metadata"),
            ),
            ClusterPolicySpecRules(
                name="generate-agent-readable-logs",
                match=ClusterPolicySpecRulesMatch(any=[namespaces_labeled(logs_label)]),
                generate=role_binding("agent-readable-logs", "agent-readable-namespace-logs"),
            ),
        ],
    )
    return chart


def ignore_cnpg_jobs_for_reloader_chart(app: App) -> Chart:
    """Keeps Reloader from restarting Jobs owned by CloudNativePG.

    Reloader treats Jobs as reloadable workloads, but its Job strategy is destructive: it
    deletes the old Job and creates a new one. That is suitable for an ordinary one-shot
    Job, but not for a CNPG bootstrap or recovery Job, which is an operator-owned step in
    the Cluster reconciliation state machine. In particular, deleting a CNPG initdb Job
    can remove its Pod while CNPG is still waiting for that Job to complete.

    This policy is deliberately CREATE-only. It puts the opt-out on a CNPG Job before
    Reloader can observe it, and puts it back on any Job that Reloader attempts to delete
    and recreate. It does not mutate UPDATEs, so it cannot compete with CNPG's
    reconciliation of the Job or with Kubernetes' Job controller. Only the Job's
    top-level metadata is changed: Reloader reads workload annotations there, and
    mutating the Pod template is unnecessary.

    Both labels are required. `app.kubernetes.io/managed-by` identifies CNPG's objects,
    while `cnpg.io/jobRole` limits this to CNPG's role-bearing Jobs and avoids changing
    unrelated Jobs that happen to use the managed-by label.
    """
    chart = _chart(app, "ignore-cnpg-jobs-for-reloader")
    ClusterPolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(
            name="ignore-cnpg-jobs-for-reloader",
            annotations=_annotations(
                autogen=False,
                title="Exclude CloudNativePG Jobs from Reloader",
                category="Reliability",
                severity="medium",
                subject="Job",
                description=(
                    "Prevents Reloader from deleting and recreating CloudNativePG-owned Jobs. CNPG bootstrap and "
                    "recovery Jobs are part of the operator reconciliation state machine, so replacing them can "
                    "remove their Pod before the operation completes. The exclusion is applied at Job creation and "
                    "is reapplied if a Job is recreated."
                ),
            ),
        ),
        admission=True,
        background=False,
        mutate_existing_on_policy_update=False,
        rules=[
            ClusterPolicySpecRules(
                name="exclude-cnpg-jobs-from-reloader",
                match=match_resources(
                    ClusterPolicySpecRulesMatchAnyResources(
                        kinds=["Job"],
                        operations=[_CREATE],
                        selector=ClusterPolicySpecRulesMatchAnyResourcesSelector(
                            match_expressions=[
                                ClusterPolicySpecRulesMatchAnyResourcesSelectorMatchExpressions(
                                    key="app.kubernetes.io/managed-by", operator="In", values=["cloudnative-pg"]
                                ),
                                ClusterPolicySpecRulesMatchAnyResourcesSelectorMatchExpressions(
                                    key="cnpg.io/jobRole", operator="Exists"
                                ),
                            ]
                        ),
                    )
                ),
                mutate=ClusterPolicySpecRulesMutate(
                    patch_strategic_merge={"metadata": {"annotations": {"reloader.stakater.com/auto": "false"}}}
                ),
            )
        ],
    )
    return chart


def cleanup_controller_jobs_chart(app: App) -> Chart:
    chart = _chart(app, "clusterrole-cleanup-controller-jobs")
    k8s.KubeClusterRole(
        chart,
        "role",
        metadata=k8s.ObjectMeta(
            name="kyverno-cleanup-controller-jobs",
            # Aggregates into the kyverno cleanup controller's ClusterRole. Required
            # before any CleanupPolicy can target Jobs: kyverno validates at policy
            # admission that the cleanup controller holds delete permission for the
            # matched kind. First consumer: gaffer-private's wedged-Job reaper in the
            # thrive-scraper namespace.
            labels=_AGGREGATE_TO_CLEANUP_CONTROLLER,
        ),
        rules=[k8s.PolicyRule(api_groups=["batch"], resources=["jobs"], verbs=_CLEANUP_VERBS)],
    )
    return chart


def cleanup_controller_workloads_chart(app: App) -> Chart:
    """Consumers: the claude-sandbox janitor (agents/agent-rbac-base/) and the props
    finished-pod reaper (props/app/) — kyverno rejects a CleanupPolicy at admission
    unless the controller can delete every kind it matches."""
    chart = _chart(app, "clusterrole-cleanup-controller-workloads")
    k8s.KubeClusterRole(
        chart,
        "role",
        metadata=k8s.ObjectMeta(name="kyverno-cleanup-controller-workloads", labels=_AGGREGATE_TO_CLEANUP_CONTROLLER),
        rules=[
            k8s.PolicyRule(api_groups=[""], resources=["pods", "services"], verbs=_CLEANUP_VERBS),
            k8s.PolicyRule(api_groups=["apps"], resources=["deployments", "statefulsets"], verbs=_CLEANUP_VERBS),
            k8s.PolicyRule(api_groups=["batch"], resources=["cronjobs"], verbs=_CLEANUP_VERBS),
        ],
    )
    return chart


def cleanup_controller_sandboxes_chart(app: App) -> Chart:
    """Consumer: the workspace-janitor CleanupPolicy (agents/agent-sandbox/workspaces/)."""
    chart = _chart(app, "clusterrole-cleanup-controller-sandboxes")
    k8s.KubeClusterRole(
        chart,
        "role",
        metadata=k8s.ObjectMeta(name="kyverno-cleanup-controller-sandboxes", labels=_AGGREGATE_TO_CLEANUP_CONTROLLER),
        rules=[
            k8s.PolicyRule(api_groups=["agents.x-k8s.io"], resources=["sandboxes"], verbs=_CLEANUP_VERBS),
            k8s.PolicyRule(
                api_groups=["extensions.agents.x-k8s.io"], resources=["sandboxclaims"], verbs=_CLEANUP_VERBS
            ),
        ],
    )
    return chart


_CHARTS = (
    require_gitops_chart,
    default_revision_history_limit_chart,
    default_disable_service_links_chart,
    default_vpa_requests_only_chart,
    proxy_injection.inject_mitmproxy_chart,
    proxy_injection.inject_haku_egress_proxy_chart,
    restrict_agent_kustomization_patch_chart,
    restrict_agent_gateway_routes_chart,
    require_secret_store_conditions_chart,
    generate_agent_diagnostics_readers_chart,
    cleanup_controller_jobs_chart,
    cleanup_controller_workloads_chart,
    cleanup_controller_sandboxes_chart,
    ignore_cnpg_jobs_for_reloader_chart,
)


def write_manifests(root: Path) -> None:
    (root / OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(root / OUTPUT_DIR))
    resources = [f"{build(app).node.id}.k8s.yaml" for build in _CHARTS]
    app.synth()
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=resources))


def kyverno_policies(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        "kyverno-policies",
        artifact,
        depends_on=[
            # Policies require Kyverno CRDs to be installed
            flux_kustomization_depends_on(kyverno)
        ],
        interval="5m",
        timeout="2m",
    )
