# Policy sets: reusable grants for ServiceAccounts

A `PolicySet` is a named, subject-free list of typed rules. A `PolicyBinding` attaches sets to
one ServiceAccount. Egress, Action and Kubernetes rules share these two kinds
(`agentplane/crds/manifests/crd-policy{sets,bindings}.yaml`), so one binding grants all of them
and one set can carry rules for several services. Each reader enforces only its own rule types and
reports in its own condition (`KubernetesReady`, `ActionsReady`, `EgressReady`) under its own
field manager. `EgressCredential` stays a separate kind. The Kubernetes reader is the
[Policy Controller](../policy_controller/SPEC.md); the Sandbox Controller writes each Sandbox's
binding ([SPEC](../sandbox_controller/SPEC.md#identity)).

A person picks a set such as `shared-diagnostics` for a preset or a launch, instead of hundreds of
per-namespace RoleBindings. Kubernetes still needs one RoleBinding per role and namespace, which the
Policy Controller keeps. Deployed scope and the static grants around it:
[agent RBAC](../../cluster/docs/agent_rbac.md).

## Decisions

- **Live semantics.** Editing a set changes every bound ServiceAccount's native bindings, including
  removals. Edits to a referenced Role's rules reach every binding to that Role under any design.
- **Roles are referenced, never defined.** A rule names an existing `Role` (in each named
  namespace) or `ClusterRole`; cluster-wide rules take only `ClusterRole`. Generated bindings name
  the ServiceAccount directly, so a set has no Kubernetes `Group` effect.
- **Namespaces are explicit lists.** cdk8s renders them from `namespace_access`. A selector would
  let whoever can label a namespace acquire grant authority.
- **Authority.** Writing a set or a binding is equivalent to granting RBAC, so no agent is
  granted write access to either on staging. Flux writes sets and the bindings of static accounts. The
  Sandbox Controller writes a Sandbox's binding, and the Sandbox Service writes runtime egress
  grants as bindings owned by the Sandbox. The controller writes bindings, never Roles. Its `bind`
  permission is a `resourceNames` list for exactly the roles the Flux-rendered sets reference,
  generated from the same source as the sets (`cluster/cdk8s/agentplane/policy_controller.py`);
  a wildcard `bind` or a broad ClusterRoleBinding in place of namespaced RoleBindings is not
  acceptable. Granting `bind` for a new role is a separate RBAC review.
- **Ownership.** A Sandbox owns its binding. The Policy Controller has no finalizer: its pass
  deletes every labeled native binding that no live binding grants, so deleting a binding while the
  controller is down is reconciled on start.
- **SA identity.** RBAC matches a subject by namespace and name, not UID, so a recreated
  ServiceAccount would inherit an uncleaned binding. Native bindings carry the account's UID and
  are deleted when the account is missing or its UID differs.
- **Native bindings** are one per (binding, role, namespace) with no deduplication, and each
  carries an instance label. Overlapping grants combine additively; revoking one never removes
  what another grants. Names and annotations identify the controller's objects, so it never adopts
  or deletes on name alone. A `roleRef` cannot be patched, so a changed role replaces the binding.
- **Subjects** are ServiceAccounts only.
- **Expiry.** The controller deletes the `PolicyBinding` at `expiresAt`, for every rule type, so
  no reader handles expiry itself. A binding a GitOps tool owns never sets it.

## Rejected alternatives

- **Expand once at assignment (snapshot).** The Sandbox Service resolved a preset to concrete
  bindings at launch. Adding a namespace or changing a preset left existing Sandboxes behind, and
  the create form listed hundreds of selections. Live expansion needs a controller, whose
  operational cost was accepted.
- **Adopt an existing operator.** None supplies subject-free sets plus independent per-account
  assignment with pruning:
  - [RBAC Manager](https://github.com/FairwindsOps/rbac-manager) (Fairwinds) adds every
    `ServiceAccount` subject to its desired accounts and may try to create or own a Sandbox's
    account. No bindings-only mode was established; a test with an externally owned account would
    decide it.
  - [access-manager](https://github.com/ckotzbauer/access-manager) and
    [rbacsync](https://github.com/cruise-automation/rbacsync) put subjects inside each definition,
    so per-account enrollment edits a shared object. access-manager is archived; rbacsync's
    generated bindings stay in one namespace.
  - The [OpenShift RBAC Permissions Operator](https://github.com/openshift/rbac-permissions-operator)
    combines one subject with its permissions, creates missing bindings without pruning removed
    ones, and builds its ClusterRoleBinding subjects without a namespace.
  - [role-operator](https://github.com/ricoberger/role-operator) creates Roles rather than
    referencing our GitOps-owned ones, and
    [nxs-rbac-operator](https://github.com/nixys/nxs-rbac-operator) is configured operator-wide
    with subjects embedded in each rule.
- **Kyverno generation.** The cluster retired a Kyverno-generated diagnostics path for Flux-owned
  bindings
  ([agent RBAC](../../cluster/docs/agent_rbac.md#namespace-reader-migration-kyverno-to-gitops)).
  Bringing it back needs a dynamic membership contract and reliable cleanup of generated objects.
- **Flux/cdk8s alone** cannot handle runtime-created ServiceAccounts or one-off additions, and
  aggregated ClusterRoles combine rules, not bindings across namespaces.
- **A finalizer on the binding** blocks deletion whenever the controller is down.
- **A privileged loop inside the Sandbox Service.** A caller-facing service would hold `bind`;
  the separate Deployment and ServiceAccount make the process identity the security boundary.
