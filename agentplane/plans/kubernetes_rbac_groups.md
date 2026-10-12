# Reusable Kubernetes RBAC grant groups

Status: **design decided (operator, 2026-10-12 PDT); the CRDs and the
[Policy Controller](../policy_controller/SPEC.md) run in staging, with Flux-owned sets and no
bindings yet.** The [task DAG](task_dag.md) tracks the rest as `KUBERNETES_POLICY_SETS` through
`POLICY_RUNTIME_GRANTS`, plus `MANAGED_SA_RBAC` (accounts without a Sandbox). Existing
[agent RBAC documentation](../../cluster/docs/agent_rbac.md) describes deployed/static
ownership; it remains the source for current grant scope.

## Why this is needed

The current [managed grant catalog](../sandbox_service/kubernetes_grants.py) maps a launch-time
name to one concrete RoleBinding or ClusterRoleBinding template. A preset in
[`agent_access_profiles.py`](../../cluster/cdk8s/agent_access_profiles.py) expands shared diagnostics
into many per-namespace selections; the [Sandbox creation form](../app/frontend/sandboxes.tsx)
presents that large list instead of one understandable diagnostics choice. At launch, Sandbox
Service [resolves and stores](../sandbox_service/provisioning.py) concrete selections, and its
[binding reconciler](../sandbox_service/kubernetes_bindings.py) maintains the resulting bindings
for that Sandbox ServiceAccount (SA). Adding a namespace or changing a preset's selections later
does **not** update those existing Sandboxes. Edits to a referenced Kubernetes Role's _rules_ do
affect every existing binding to that Role; this is separate from updating which roles/namespaces
a group includes. The large form and duplicated assignments are the immediate problem; update
propagation to existing SAs is a preference, not an unconditional requirement.

## Desired experience and constraints

- Define a named, subject-free group such as `low-privilege-cluster-diagnostics` containing the
  approved role references and namespace scope. Select one name in a preset or the create form;
  do not present hundreds of individual namespace grants as the normal choice. Kubernetes may
  still require one underlying RoleBinding per namespace/SA.
- Assign it to an SA. A preset supplies defaults for **new** Sandboxes, but editing a preset must
  not silently reassign already-running Sandboxes. An authorized operator must also be able to
  add or revoke one binding for one existing SA at runtime without changing shared groups or
  other SAs. A private singleton group through the _same_ assignment mechanism is acceptable;
  two distinct implementation paths are not required. Overlapping assignments combine
  additively, and removing one must not destroy the other's access.
- **Prefer live group associations:** changes to a group's constituent roles or eligible
  namespaces reconcile for all currently assigned SAs, including removals. **Acceptable
  alternative:** expand once at assignment (Sandbox creation or a later runtime assignment).
  Then existing expanded binding membership remains unchanged until an explicit update; UI and
  docs must make that snapshot semantics clear. Changes to the rules of a referenced Role still
  take effect under either model. Choose this consciously, including the operational cost of
  updating existing assignments.
- Preserve namespace-by-namespace scoping and reviewed namespace opt-in. Do not replace
  namespaced RoleBindings with a broader ClusterRoleBinding for convenience. Existing Role
  definitions and static bindings have their own GitOps/service owners; a new reconciler must
  own only the objects delegated to it. Policy names, assignment origin, expanded scope and reconciliation failures
  must be inspectable. Check actual effective permission with the SA; catalog membership is not
  proof of access.
- Define authorization, audit, revocation, cleanup and same-name SA replacement behavior for
  assignments. An SA cannot grant itself arbitrary RBAC by naming a powerful role. Existing
  Sandboxes with snapshotted concrete grants require an explicit migration decision; do not
  retroactively enroll them because a preset changes.

## Preferred live design (proposal)

Use a small **separate controller Deployment/image and ServiceAccount**, not an additional
privileged loop in caller-facing Sandbox Service. Sandbox Service validates authorized
create/update requests and writes assignments; the controller alone expands them into native
RBAC bindings. Its process identity is the security boundary; the separate small image also
limits dependencies. Reuse existing binding-name, conflict and cleanup logic where possible,
but do not let both reconcilers own the same bindings. This direction buys live updates; if
we choose snapshot semantics instead, a deployment-managed group catalog and existing Sandbox
state could avoid the new CRs initially.

Decided (operator, 2026-10-12 PDT): egress, Action and Kubernetes policy merge into one generic
`PolicySet` of typed rules (a `type` discriminator) and one `PolicyBinding`. Each reader handles
only its own rule types and writes its own condition type (`KubernetesReady`, `ActionsReady`,
`EgressReady`) through server-side apply; `EgressCredential` stays separate. Kubernetes and egress
rules have moved over; Actions follow.

The CRDs are `agentplane/crds/manifests/crd-policy{sets,bindings}.yaml`; what the controller
guarantees is its [SPEC](../policy_controller/SPEC.md).

Also decided (operator, 2026-10-12 PDT):

- **Live semantics**: editing a set changes every bound SA's native bindings.
- **Namespaces** are explicit lists that cdk8s renders from `namespace_access`; no selectors.
- **Writers**: only Flux writes sets. Sandbox Service writes a Sandbox's binding at create and
  through an operator-only grant/revoke RPC; Flux writes bindings for static SAs.
- **`bind` authority**: the controller holds `bind` only on role `resourceNames` generated from
  the same source as the sets.
- **Ownership**: a Sandbox owns its binding. No finalizer: every controller pass deletes the
  labeled native bindings that no live binding grants.
- **SA identity**: native bindings carry the SA's UID and are deleted when the SA is missing or
  its UID differs, so a same-name replacement inherits nothing.
- **Native bindings**: one per (binding, role, namespace), no deduplication, each carrying an
  instance label.
- **Controller**: its own Deployment and ServiceAccount.
- **Expiry**: the controller deletes the `PolicyBinding` at `expiresAt`, for every rule type, and
  with it its native bindings; an overdue expiry alerts.
- **Subjects**: ServiceAccounts only.
- **Cutover**: clean. Existing staging Sandboxes with grants are recreated, and the PR that does
  it gets a decision card first.

The definition refers only to **existing** Roles/ClusterRoles. Its namespaced entries support
`Role` (existing in each named target namespace) or `ClusterRole`; cluster-wide entries
support only `ClusterRole`. Choose explicit namespace names first, rather than introducing a
selector whose label writer might inadvertently acquire grant authority. A binding names its
SA subject; who may write a binding for which SA is the authority question below. Presets populate _new_ assignments, not existing
Sandboxes. An authorized one-off binding can use a dedicated singleton group through the
same assignment path; removing that membership must not revoke another group's overlapping
grant. Status belongs on the assignment (observed group generations, readiness/error and
counts, without a massive per-namespace status payload). The group has no native Kubernetes
`Group` membership effect: generated RoleBindings/ClusterRoleBindings name the SA directly.

**Authority and ownership:** agents must not have unrestricted CR write privileges. Editing a
group or assignment is equivalent to granting RBAC; only a reviewed deploy/operator workflow
or a validated, audited service operation may do so. The controller should read only the
necessary Sandbox/SA and CR data, and write **bindings**, not Roles/ClusterRoles. Prefer
namespace-scoped Role grants in approved target namespaces; cluster-scoped binding writes,
if enabled, are separately reviewed. Kubernetes RBAC's privilege-escalation checks also
require `bind` on the particular approved Role/ClusterRole names (unless already held); grant
`bind` by `resourceNames`, not a wildcard. Do not substitute a broad ClusterRoleBinding for
hundreds of namespaced RoleBindings. Treat new authority as a separate RBAC review, not as a
side effect of deploying the controller image.

**Deletion and rollout:** deterministic generated names and owner/UID annotations must let
the controller distinguish its objects from Flux's and other assignments' bindings. A
namespaced assignment cannot owner-reference bindings in other namespaces or a
ClusterRoleBinding: use finalizers and explicit cleanup; same-namespace owner references can
assist GC. Never adopt/delete on name alone. Kubernetes RBAC subject identity is SA
namespace/name, _not UID_: merely recording a Sandbox UID does not prevent a replacement SA
with the same name from inheriting an uncleaned binding. Define cleanup/gating and test SA
replacement before enabling live grants. Existing launch-snapshot bindings remain under
Sandbox Service until an explicit per-Sandbox handoff has removed/retired them; no automatic
migration or double reconciliation. Definition narrowing, deletion and namespace removal must
remove old bindings even after a roleRef changes (which cannot be patched in-place). Test
partial failures, restarts and revocation before relying on live semantics.

## Candidate mechanisms and prior art

### Agentplane-owned definition and assignment

The preferred CR/controller split above directly models subject-free definitions and per-SA
membership, borrowing virtual-group expansion from rbacsync and bindings-only namespace
fan-out from access-manager. It has the operational cost of a separately deployed privileged
controller and reviewed cross-namespace cleanup. Its definition and assignment objects must
not have multiple writers: Flux may own group definitions while an authorized runtime
service owns per-Sandbox assignments, but neither should patch the other's objects. See the
ownership and migration constraints above before selecting it for implementation.

### Fairwinds RBAC Manager

[RBAC Manager](https://github.com/FairwindsOps/rbac-manager/blob/master/docs/introduction.md) is
an Apache-2.0 operator that reconciles a cluster-scoped
[`RBACDefinition`](https://github.com/FairwindsOps/rbac-manager/blob/master/docs/rbacdefinitions.md)
to native RoleBindings/ClusterRoleBindings. Its documented examples assign a named
Role/ClusterRole to subjects in fixed namespaces or namespaces matching a label selector;
namespace changes and removal of previously generated bindings are part of its reconciliation.
This is close prior art for the namespace fan-out, ownership and revocation problem. It does
not supply separate, subject-free policy and assignment objects: `rbacBindings` combine subjects
with role references and namespace scope. Runtime membership would require patching the
subjects in a definition or generating definitions from another source of truth; Flux and a
runtime writer should not both edit that same subject list.

**Important compatibility gap for dynamically created Sandbox SAs:** in the inspected upstream
[source parser](https://github.com/FairwindsOps/rbac-manager/blob/master/pkg/reconciler/parser.go),
every `ServiceAccount` subject is also added to RBAC Manager's desired SAs. Its
[SA reconciler](https://github.com/FairwindsOps/rbac-manager/blob/master/pkg/reconciler/reconciler.go)
compares ownership (see the [matcher](https://github.com/FairwindsOps/rbac-manager/blob/master/pkg/reconciler/matcher.go))
and may try to create an already-existing SA; an SA owned by Sandbox Service
is not a clean reference-only match. A RoleBinding might still be produced despite an SA-create
conflict; do **not** treat that as a supported integration. We have not established whether a
supported bindings-only mode exists in the version we would deploy. Static `User`/`Group`
subjects are a simpler fit; the existing static public-coder group and a per-Sandbox SA are
_different_ identities. RBAC Manager could be useful for the former without solving the latter.
Do not transfer SA ownership, duplicate the controller's binding names, or replace existing
Flux bindings without an explicit migration. An isolated test with an externally owned SA,
namespace addition/removal and definition deletion would answer the compatibility question.

### Wider search: virtual membership and other controllers

A broader GitHub repository search found
[rbacsync](https://github.com/cruise-automation/rbacsync), a closer conceptual precedent for
**virtual** (not Kubernetes-authentication) groups. Its
[`bindings` and `memberships`](https://github.com/cruise-automation/rbacsync/blob/master/pkg/apis/rbacsync/v1alpha/types.go)
map a group name to a RoleRef and a set of native Kubernetes RBAC subjects; the generated
binding directly lists the resolved subjects. Memberships accept `rbacv1.Subject` (including
an SA with its namespace); the controller
[updates subject lists and prunes owned bindings](https://github.com/cruise-automation/rbacsync/blob/master/pkg/controller/controller.go).
This demonstrates that group-like membership can be implemented _without_ adding SAs to a
Kubernetes authentication group. Its namespaced `RBACSyncConfig` generates RoleBindings
**only in that config's namespace**; its cluster-scoped variant generates
ClusterRoleBindings, not fan-out to selected namespaces. Both membership and binding
configuration live in each config, rather than in a single subject-free definition with
separate per-SA assignments across many namespaces. The repository is not archived, but its
last recorded push in this review was December 2023; assess maintenance before considering
adoption.

[ricoberger/role-operator](https://github.com/ricoberger/role-operator) is a newer example
that creates Roles and bindings for a list of subjects in explicitly named namespaces.
Because one CR defines both rules and subjects, and it creates Roles rather than simply
referencing our existing GitOps-owned ones, it is less aligned with our desired ownership
split. [nxs-rbac-operator](https://github.com/nixys/nxs-rbac-operator) is another
binding fan-out example for Users, Groups and SAs using namespace-name regexes, but its
configuration is operator-wide and embeds the subjects directly in each rule. Neither
provides our subject-free grant group plus independent runtime assignments out of the box.
These are comparison examples, not endorsements or proof of full revocation behavior.

### Other binding-fan-out controllers

[access-manager](https://github.com/ckotzbauer/access-manager) is particularly close to the
**externally owned SA** requirement: its cluster-scoped `RbacDefinition` references existing
Roles/ClusterRoles and subjects (including an SA in another namespace), and generates only
RoleBindings/ClusterRoleBindings across explicitly named or label-selected namespaces. It
watches definitions, namespaces and SAs, and does not take over unrelated bindings. Unlike the
proposed group + assignment split, a definition contains its own subjects; per-SA enrollment
still needs a separate mechanism or a definition edit. The project is **archived**, so borrow
its bindings-only ownership and fan-out ideas rather than assuming it is a maintained dependency.

The [OpenShift RBAC Permissions Operator](https://github.com/openshift/rbac-permissions-operator)
uses a namespaced
[`SubjectPermission`](https://github.com/openshift/rbac-permissions-operator/blob/master/api/v1alpha1/subjectpermission_types.go)
to bind existing ClusterRoles to a `User`, `Group`, or `ServiceAccount` (with an optional
`subjectNamespace`). `clusterPermissions` produce ClusterRoleBindings;
`permissions` produce RoleBindings in namespaces chosen by allow/deny **name regexes**. Its
[subject controller](https://github.com/openshift/rbac-permissions-operator/blob/master/controllers/subjectpermission/subjectpermission_controller.go)
handles definitions, while its
[namespace controller](https://github.com/openshift/rbac-permissions-operator/blob/master/controllers/namespace/namespace_controller.go)
creates bindings for new matching namespaces. This is useful prior art for binding dynamically
created, separately owned SAs, not for reusable group **membership**: the CR combines one
subject with its permissions. Binding a Kubernetes `Group` does not add an SA to that group;
normal SA token authentication supplies only Kubernetes' built-in SA groups, not arbitrary
per-SA group membership. Our named grant group would be Agentplane policy expanded into native
bindings with the SA itself as subject, not a Kubernetes RBAC `Group` subject.

**Revocation caveat:** the inspected operator controllers create missing bindings and skip
existing ones; they do not visibly prune bindings when a permission or namespace regex is
removed. Do not assume live removal/declassification works without testing or adding a cleanup
mechanism. Also validate SA ClusterRoleBindings separately before adopting this implementation:
the [ClusterRoleBinding helper](https://github.com/openshift/rbac-permissions-operator/blob/master/controllers/subjectpermission/subjectpermission_controller.go)
constructs subjects without `subjectNamespace`, whereas the namespaced RoleBinding helper
sets it. Neither controller supplies our separately reusable, subject-free grant definition.

### Kyverno generation, GitOps and native RBAC aggregation

Kyverno generate rules could produce bindings from eligible namespaces and subjects. This repo
[retired a Kyverno-generated diagnostics-binding path](../../cluster/docs/agent_rbac.md#namespace-reader-migration-kyverno-to-gitops)
in favor of Flux-owned bindings. That does not prove Kyverno cannot implement this requirement,
but reintroducing it needs a clear owner, dynamic SA membership/definition update contract and
reliable generated-resource cleanup. Flux/cdk8s generation works for fixed subjects and reviewed
changes but does not by itself handle runtime-managed Sandbox SAs or their operator-requested
one-SA additions. Native aggregated ClusterRoles combine _rules_, not the RoleBindings across
selected namespaces or the assignment of those bindings to SAs.

## Decision and implementation checks

Compare the mechanisms against **two existing managed Sandbox SAs sharing one group** and a
third SA with a one-off binding. Show the create form's compact selection, then add and remove
an eligible namespace, widen/narrow a referenced Role, change the group membership, revoke the
one-off and shared assignments independently, delete a Sandbox, and restart the controller.
State exactly which existing SAs change after each step,
including same-name SA replacement, partial failures and owner/Flux conflicts. Show how an
operator inspects both intent and expanded grants, and verify representative allowed and denied
requests with the affected SAs. If selecting RBAC Manager, first prove that an externally owned
Sandbox SA can be bound without RBAC Manager trying to own/create/delete it; do not test with a
production SA or credentials. An SA with no Sandbox is an ordinary binding of the same CR.
