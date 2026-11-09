# Identities, workload authentication and Kubernetes accounts

Status: **expand phase landed; cutover open, 2026-11-09.** Directions selected by the operator on
2026-11-05 ([workload authentication](../docs/workload_authentication.md),
[`AGENT_PRINCIPAL_MODEL_DESIGN`](task_dag.md#agent_principal_model_design--agentplane-principals-and-external-identities)):
SVIDs, not ServiceAccount tokens, authenticate agent workloads; Agentplane owns agent identity and
no agent gets a ServiceAccount by default; a Kubernetes account is a resource several identities
may act as, granted by separate objects; first-party services verify the SVID themselves; the proxy
acts as an account by impersonation. Policy objects stay Kubernetes objects. The implementation is
DAG section [14](task_dag.md#14-identity-and-kubernetes-accounts-cutover).

Landed, with contracts in the owning docs: the `Identity`, `KubernetesRoleSet`,
`ServiceAccountGrant` and `ManagedServiceAccount` kinds and the additive Identity binding subject
(#15072; `policy/api/v1alpha1/types.go`); grants, role sets and managed ServiceAccounts in the
Policy Controller (#15076; [SPEC](../policy_controller/SPEC.md#serviceaccount-grants)); the
`kubernetesServiceAccount` source and impersonation at the proxy (#15075, #15095;
[egress SPEC](../egress/SPEC.md) § Decision); Identity lookups in the shared reader (#15115) and
`PolicyService` Identity watches and deletes (884deb63, a3b40e4e); the catalog rendered in the new
shapes behind `Environment.identity_catalog` (#15118, #15121, da2b769e); and per-incarnation
`WorkloadCredential`s for Sandboxes ([workload authentication](../docs/workload_authentication.md#workloadcredentials)).
What follows is what the cutover still changes.

## What this separates

Today one object, the Sandbox-owned ServiceAccount, plays four roles: the workload credential the
egress proxy and first-party services authenticate by TokenReview, the `kubectl` identity (the Pod's
own projected token, substituted by `projectedWorkloadToken`), the `PolicyBinding` subject, and the
execution principal History and Dispatcher pin by UID. Its lifetime is the Sandbox's. The proposal
gives each role its own object:

| Role                        | Today                               | Proposed                                                                             |
| --------------------------- | ----------------------------------- | ------------------------------------------------------------------------------------ |
| Who the agent is            | ServiceAccount name and UID         | `Identity`                                                                           |
| Proof of a workload         | Projected SA token, TokenReview     | SVID on the egress hop; SA token only for workloads without one                      |
| What Agentplane enforces    | `PolicyBinding` to a ServiceAccount | `PolicyBinding` to an `Identity`; Kubernetes rule types leave `PolicySet`            |
| What Kubernetes enforces    | RoleBindings for the Pod's own SA   | `ServiceAccountGrant` for any ServiceAccount in the accounts namespace               |
| Kubernetes reach            | The Pod's own SA                    | An account the identity may present a credential for, through the proxy; none unless |
| Execution principal records | ServiceAccount UID                  | Identity name and UID, plus the workload incarnation as provenance                   |

Two systems fall out, each with one kind of input: Agentplane policy (`PolicySet`, `PolicyBinding`,
`EgressCredential`, subject `Identity`) and a Kubernetes RBAC provisioner (`KubernetesRoleSet`,
`ServiceAccountGrant`, subject a ServiceAccount). The Policy Controller is the provisioner, as it
already is for SA subjects today; the Crossplane managed-resource pattern is the same shape. The two
meet in exactly one place: an `EgressCredential` whose source names a ServiceAccount.

## Objects

### `Identity`

The kind has landed (description only, no status). What the cutover adds is how environments
and creators use it.

An identity is never owned by a Sandbox or any other environment (operator, 2026-11-05): a central
Session attaches and detaches environments repeatedly, and a standing agent moves between Sandboxes,
so the agent's lifetime and the compute's are different things. A Sandbox names its identity in
`spec.identity`, which may be absent or changed while nothing is running
([below](#changing-an-environments-identity)); a start without one is refused with
`IdentityMissing`. Who creates the identity decides its lifetime: Flux for a standing agent; the app at
launch when the operator starts a fresh agent, deleted by the operator's explicit "finish with this
agent"; Dispatcher for a Session-managed identity, deleted with the Session
([Session/environment model](session_environment_model.md#sessions-serviceaccounts-and-environments)).
Deleting the environment never deletes the identity; deleting the identity does not delete an
environment that names it, which then cannot start a new workload and reports `IdentityMissing`.
Every binding and grant that belongs to the agent is owned by the identity, so the agent's whole
authority goes with one delete and survives any number of environment changes.

### `PolicyBinding` and `PolicySet`: Agentplane policy only

`PolicyBinding.spec.subject` is an `Identity`; the ServiceAccount subject goes with the cutover.
The catalog already renders this shape behind the switch (`cluster/cdk8s/agentplane/kubernetes_accounts.py`).
`PolicySet` keeps `egress`, `action_auto_approve` and `session_inbox_access` and loses the two
Kubernetes rule types, so every rule in a set is enforced for every binding of it. Sets, `expiresAt`
and ownership are unchanged. "May act as a Kubernetes account" is an `egress` rule like any other
credential use:

```yaml
kind: PolicyBinding # every launch also holds the shared account's use set, an environment default
spec: { subject: { identity: agent-123 }, policySets: [basic, k8s-account-shared-diagnostics, github-ducktape] }
---
kind: PolicySet
metadata: { name: k8s-account-shared-diagnostics } # cdk8s-rendered for a static account
spec:
  rules:
    - type: egress
      hosts: [kubernetes.default.svc.cluster.local]
      clusterInternal: true
      credentialRef: { name: k8s-account-shared-diagnostics }
---
kind: EgressCredential
metadata: { name: k8s-account-shared-diagnostics }
spec:
  description: Act as the shared read-only diagnostics account on the local apiserver.
  source:
    kubernetesServiceAccount: { namespace: agentplane-k8s-accounts, name: shared-diagnostics }
  targets: [{ header: Authorization, method: schemeToken, scheme: Bearer }]
```

### `ManagedServiceAccount`: the remaining emission

The kind and its ServiceAccount lifecycle have landed
([Policy Controller SPEC](../policy_controller/SPEC.md#managed-serviceaccounts)). Still to build
(`POLICY_GRANTS`): the controller emits the account's `EgressCredential` and one-rule `PolicySet`,
owned by the account (the same two objects cdk8s renders for a static account), so grants, use
bindings, credential and set for a temporary account all go with its one clock. Admission for that
new authority is question 11.

## Workload authentication

Sandbox SVID authentication has landed with the Sandbox's own ServiceAccount as the subject
([workload authentication](../docs/workload_authentication.md)); non-Sandbox callers keep Pod-bound
bearers until the [workload identity lifecycle](workload_identity_lifecycle.md) moves them. What the
cutover changes:

- The resolver yields the record's `spec.subject.identity`, and rules key on the identity.
  Everything the egress SPEC says about a subject holds with "ServiceAccount" read as "Identity": a
  subject no binding names reaches no rule, and the connection address is not identity evidence.
- The proxy drops the proxy-audience and Kubernetes-audience projected tokens.
- The MCP edge binds a Connection to an `Identity` instead of a ServiceAccount (question 21).

## Changing an environment's identity

The credential is not what makes this hard: the SVID names the Sandbox incarnation, not the
identity, and verifiers resolve the identity through the credential's record, which the Sandbox
Controller writes from `spec.identity` at each start. The identity changes only while nothing runs,
so the next start is a new binding and the credential ID rotates anyway
([workload authentication](../docs/workload_authentication.md#workloadcredentials)). What makes it hard is what is in flight
at the moment of the change: Sessions already bound to the old identity, whose execution principal
is immutable and which the same-environment invariant now bars from running there; requests and
tunnels admitted as one identity and completing as another; and a RAM-preserving Firecracker
snapshot, whose restore would resume the old agent's harness mid-turn acting as the new identity.

So `spec.identity` may be absent or changed while the environment is suspended or stopped, and a
start requires one (operator, 2026-11-05: changing it while suspended is wanted). The next start
runs as the new identity under a new credential ID, so the boundary shows in every audit log as a
new SPIFFE ID. If a RAM-preserving Firecracker snapshot
exists, the controller discards it and cold-starts the guest, which is the one way the snapshot
hazard is handled. An environment may therefore be created unbound and bound by the Session that
attaches it, the create-then-bind sequence the
[Session/environment model](session_environment_model.md#session-managed-sa-and-native-creation-order)
floated.
A change is admitted only when no Session of a different identity is still placed on or attached
to the environment (operator, 2026-11-05: never pull the identity out from under a harness); the
operator ends or detaches those Sessions first. Two layers hold this, because the Sandbox is a
Kubernetes object and Sessions live in Dispatcher's directory. The admitted path, the change through
`EnvironmentService`, runs in Dispatcher's process, checks the directory and refuses with the
Sessions named. A bypass, such as `kubectl edit`, cannot be caught by CRD validation, so
Dispatcher's delivery and wake path checks that the environment's current identity equals the
Session's, which the same-environment invariant already requires, and refuses to deliver to a
mismatched Session with a reason. Nothing ever runs as an identity it was not created as; the worst
a bypass does is strand those Sessions until the identity is put back. New identity, new
Sessions. The workspace and state volumes carry over, which is the point of reusing the
environment: the operator is accepting that the next agent sees whatever the previous one left
there, exactly as two Sessions sharing an environment do today, and the app says so in the same
words it uses for running as an existing identity.

## Central Sessions with no environment

A central-sampler Session can run with zero environments
([Session/environment model](session_environment_model.md#central-session-without-a-required-executor-environment)).
Nothing then holds a credential for the agent:

- The Session names its `Identity` at `CreateSession`; Dispatcher is the authority for that
  association, and either creates a Session-managed identity or checks the caller may use an
  existing one.
- Nothing acts as that identity yet (operator, 2026-11-09). The sampler calling the Action Service,
  Notification or History as the agent, including how such calls originate from its V8 isolates, is
  a later design ([`CENTRAL_SESSION_ACTING_DESIGN`](task_freezer.md#central_session_acting_design--a-central-session-acting-as-its-identity)).
  Until then the identity's bindings take effect only once an environment is attached.
- Egress from the sampler itself is the sampler's: it reaches LLM ingress and the direct Anthropic
  route with its own credentials today and keeps doing so. The agent's egress rules apply where
  its code runs, which with no environment is nowhere.
- Kubernetes without an environment is nothing: no process presents a placeholder, so a grant to an
  account is inert until an environment is attached.
- Attaching an environment later: the Sandbox's spec names the identity, and the proxy resolves the
  incarnation's SVID to its credential record and so to the identity, admitting it under the same bindings.
  Detaching, or deleting the environment, changes nothing about the identity or its grants. The
  same-environment invariant holds because the environment runs as exactly one identity, whichever
  Session attached it.

## Kubernetes execution

Native bindings for grants and managed accounts, and what revokes them, are in the
[Policy Controller SPEC](../policy_controller/SPEC.md#serviceaccount-grants); acting as an account
by impersonation is in the [egress SPEC](../egress/SPEC.md) § Decision. Only the local cluster is
modelled.

**What the agent sees.** No kubeconfig. Materializing one was a Kubernetes-specific convenience
that does not generalize to any other external service an agent may be granted (operator,
2026-11-05). The agent learns how to reach Kubernetes the way it learns every other credential: the
rules endpoint lists the credentials its identity may present, and each `EgressCredential`'s
`description` says what the credential is and how to use it, here the apiserver host, the account
it acts as, and that `kubectl --server <host> --token <placeholder>` or a kubeconfig the agent
writes itself is the way in. The platform instructions point at the endpoint, as they do today.
Nothing inside the sandbox ever holds a real token, and a new grant needs no file to change.

## Operator UI

Because nothing in Kubernetes owns an identity, the app has to show identities as first-class
objects and let the operator end them (operator, 2026-11-05). The surface is the existing policy
watch and "View grants" disclosure re-anchored on the identity instead of a ServiceAccount:

- **An identities list.** Name, description, origin (git, app, Session), what references it (the
  Sandboxes naming it and the Sessions Dispatcher has bound to it, both live reads), its bindings
  with expiry, the Kubernetes accounts its rules let it present, and last activity from the egress
  decision history and Action receipts, which already name the subject. A filter for identities
  nothing references, so a leak is visible rather than silent.
- **Delete.** Enabled when nothing references the identity; refused by the app otherwise, with the
  referencing objects named. Git-origin identities are read-only here, since Flux would recreate
  them. The check is advisory and racy against a concurrent launch, which is acceptable because a
  Sandbox that names a missing identity fails closed and says so; Kubernetes cannot enforce
  "unreferenced" on delete.
- **Finish an agent.** The Thread's finish, and a Sandbox delete, offer to take the identity along
  when it is the last thing naming it. The default follows how the identity came to be (operator,
  2026-11-05): an identity the app minted for this launch is selected for deletion, since its whole
  reason to exist was this agent; an existing identity the operator chose at launch, or a git-origin
  one, is kept, since a standing agent's grants are the expensive thing to recreate. The app records
  that fact on the identity at creation (an annotation naming the Sandbox or Session it was minted
  for), so the default is a property of the object, not of whichever screen happens to finish it.
- **Start a session** (operator, 2026-11-05: "session", not "agent") is unchanged on the surface
  for a new identity: pick sets, and they become the identity's launch binding. Choosing an
  existing identity hides the set selector and shows that identity's current bindings read-only,
  with a link to its page. Permissions are a property of the identity: bindings are additive, so a
  launch cannot narrow them per environment, and widening a standing identity is a deliberate grant
  with its own expiry on the identity's page, never a side effect of starting a session.
  The same form chooses placement (operator, 2026-11-05: one form rather than environment-then-
  session, either acceptable): a new environment from a template, an existing environment, or none
  for a central Session that attaches one later. The two choices constrain each other through the
  same-environment invariant: an existing environment fixes the identity to its own, read-only; a
  new or absent environment leaves the identity free. One submit makes the two backend calls,
  `CreateEnvironment` then `CreateSession`, or just the second.

## Lifetimes

Who cleans what, and by which mechanism:

| Object                                       | Owner                                                                                    | Removed by                                                              |
| -------------------------------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `Identity`                                   | Nobody in Kubernetes: Flux, the app, or Dispatcher created it                            | Flux prune; the operator's explicit delete; Dispatcher with the Session |
| `PolicyBinding`                              | Identity (launch sets and runtime grants), Flux, `ManagedServiceAccount` (temporary use) | GC with its owner; the controller at `expiresAt`                        |
| `ServiceAccountGrant`                        | Flux, Identity (runtime widening), `ManagedServiceAccount`                               | GC with its owner; the controller at `expiresAt`                        |
| `ManagedServiceAccount`                      | Identity                                                                                 | GC with the identity; the controller at `expiresAt`                     |
| Generated `EgressCredential` and `PolicySet` | `ManagedServiceAccount`, or Flux for a static account                                    | GC with the account; Flux prune                                         |
| ServiceAccount in the accounts namespace     | Flux, or the controller (UID-labelled)                                                   | Flux prune; the controller's prune pass                                 |
| Native RoleBindings                          | The controller (labelled, grant UID)                                                     | The controller's prune pass                                             |

Identities do not garbage-collect at all, by design: nothing in Kubernetes owns them. Two
things therefore need an explicit end: the app-created identity of an agent the operator is done
with, and the Dispatcher-created identity of a central Session on whatever Session deletion turns
out to be. A leaked identity costs nothing while nothing can authenticate as it, but its bindings
remain latent authority for whoever can later run as it, which is why `CENTRAL_SESSION_ACTING_DESIGN` must bound what
the sampler may assert.

| Case                                  | Identity                                         | Kubernetes account                                                        | Grants and use                                  |
| ------------------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------- | ----------------------------------------------- |
| Standing agent (Haku, daily CI agent) | Flux                                             | Flux ServiceAccount, or none                                              | Flux grant; use rule in the agent's sets        |
| Agent started from the app            | The app at launch, free-standing                 | None, or the shared account via the launch defaults                       | Identity-owned binding from the launch sets     |
| Agent with no Kubernetes need         | as above                                         | None; no object anywhere                                                  | No use rule, no grant                           |
| Temporary extra permissions           | unchanged                                        | `ManagedServiceAccount` owned by the identity, expiring                   | Grant and use binding owned by the account      |
| Widening a shared account for an hour | unchanged                                        | unchanged                                                                 | One expiring grant on the account               |
| Central Session, no environment       | Dispatcher, or an existing one named at creation | None unless granted; usable only through an Action or a later environment | Bindings on the identity; reached by delegation |
| Identity deleted                      | gone                                             | Owned accounts garbage-collected                                          | Owned bindings and grants garbage-collected     |

## Cutover

One incompatible cutover, not a staged migration (operator, 2026-11-05: downtime in existing
services until images converge is acceptable; [`AGENTS.md`](../AGENTS.md) § Deployed state is
disposable). No dual readers, no ServiceAccount binding subject, no tolerant decoding of SA-keyed
rows. The change set lands together, and the PR states the rollback: revert the Flux pins and
delete the objects of the new kinds.

- **CRDs and catalog:** flip `Environment.identity_catalog`, whose rendering has landed; remove the
  ServiceAccount binding subject and `PolicySet`'s Kubernetes rule types.
- **Controllers:** the Sandbox Controller requires `spec.identity`, creates no ServiceAccount, and
  owns nothing identity-related; the app creates a free-standing `Identity` and its launch binding
  before the Sandbox, or names an existing one.
- **Data plane:** the proxy resolves identities and drops the proxy-audience and Kubernetes-audience
  projected tokens; the sandbox templates stop mounting a kubeconfig. Sandbox sidecars and the VM
  launcher relay already present the SVID on the hop; the Firecracker relay is an
  `IDENTITY_SYSTEM_TESTS` scenario.
- **Services:** History, Dispatcher, Notification, the Action Service, LLM ingress
  authenticate, authorize and record by identity. Rows that pin an SA UID are rewritten by
  the migration where the identity is known and marked historical where it is not, as History's
  migration 8 did for harness provenance; same-principal reads of historical rows stay unavailable
  and operator access covers them.

What stops during convergence: decisions for a running Sandbox until the proxy, services and
bindings agree on identity subjects, and `kubectl` from the moment the proxy drops the
Kubernetes-audience token until the agent reads the shared account's credential from the rules
endpoint. Sandbox sidecars already use the SVID hop, and the Gateway path verifies SVIDs already.
Running Sandboxes are replaced rather than patched: a Pod's ServiceAccount cannot change in place,
and the new launch writes the identity.

## Open questions and tricky bits

Numbers are stable because other plans cite them.

- **1. Pod-bound checks per route.** Services verify the SVID themselves (decided 2026-11-05). Still
  to settle per route: what each service does with the Pod-bound checks it makes today.
- **5. Credential descriptions as the only instructions.** With no kubeconfig, the `description`
  on each Kubernetes credential is what tells an agent the host, the account and the `kubectl`
  invocation; settle the convention for that prose, generated by cdk8s and the controller alike,
  and whether the rules endpoint should carry a structured `howToUse` beside it for every
  credential kind, not Kubernetes alone.
- **6. Central sampler as an identity: deferred** (operator, 2026-11-09). Out of scope for this
  cutover; `CENTRAL_SESSION_ACTING_DESIGN` in the task freezer owns it. Until designed, the sampler's
  own account never becomes the agent's and no service accepts a delegated identity from it.
- **7. Stored principals.** History's `execution_principal` and Dispatcher rows pin SA UIDs. The
  cutover migration rewrites rows whose Sandbox still exists to its identity and marks the rest
  historical; confirm that is enough for the retained Sessions the operator wants readable, since
  same-principal reads of historical rows are unavailable.
- **8. Same-environment invariant** ([identity contract](task_dag.md#session_identity_contract--session-identity-and-shared-environment-admission))
  becomes "same identity"; co-resident Sessions share one SVID, so it holds by construction, and
  admission must still refuse a Session whose identity differs.
- **10. Who authors role sets.** A grant references `KubernetesRoleSet`s, with no inline roles
  (decided 2026-11-05). Open: whether the app may author role sets, or only grants to Flux-authored
  ones.
- **11. Admission for controller-written policy.** The Policy Controller writing a `PolicySet` and an
  `EgressCredential` per managed account is new authority; an admission rule limits it to the
  one-rule shape naming that account's own ServiceAccount and apiserver host.
- **12. Ending an identity.** The identities list and the finish-time offer above cover the operator
  path; `Identity` carries no `expiresAt` (operator, 2026-11-09). Open: what "referenced" means for
  a retired Sandbox that could still resume.
- **13. Second control plane.** Accounts live on the local apiserver regardless of where
  Agentplane's records live, so this plan is neutral across the
  [architecture directions](architecture_overview.md); `Identity`, grants and managed accounts move
  with the Sandbox family under D3.
- **14. Binding subjects after the cutover** (operator asked 2026-11-09). `PolicyBinding.spec.subject`
  names exactly one `Identity`; no ServiceAccount, Group or selector subject. Grouping stays on the
  policy side: a `PolicySet` bound to many identities is the set-of-identities mechanism, and a
  Flux-rendered binding per standing identity is the roster. Open: whether a real case, many
  short-lived identities needing one grant that cannot be their launch set, justifies an
  `IdentityGroup` kind. Label selection is the shape this plan rejects elsewhere: namespaces are
  named, never selected.
- **15. Who mints identities** (operator, 2026-11-09). Dispatcher owns a Session-scoped identity because
  it is the one component that knows a Session is ending. Open: collapse the app-minted path into
  Dispatcher's, so every non-Flux identity is Session-managed and the finish dialog deletes it
  through the Session; the launch form already calls `CreateSession`, and the create-then-bind
  sequence of `SANDBOX_IDENTITY_SPEC` makes the Sandbox-first order unnecessary. Recommendation:
  collapse, so the app never writes an `Identity`.
- **16. API groups by domain** (operator asked 2026-11-09). The kinds fall into three domains:
  environments (`Sandbox`, `SandboxTemplate`), Agentplane identity and affordances (`Identity`,
  `PolicySet`, `PolicyBinding`, `EgressCredential`), and Kubernetes grants (`KubernetesRoleSet`,
  `ServiceAccountGrant`, `ManagedServiceAccount`). All share one API group, and the Go packages only
  partly follow the split (`sandboxes/api`, `policy/api`, `egress/api`; the grant kinds sit in
  `policy/api`). Recommendation: three API groups at `IDENTITY_CUTOVER`, which already rewrites
  every policy object, moving the grant kinds' Go package with them; no churn before that.
- **17. `EgressCredential` with an impersonation source: name kept** (recommendation, 2026-11-09). From
  the agent's side it is still a credential it may present a placeholder for, listed by the rules
  endpoint with a description; the source kind (Secret, projected token, impersonated
  ServiceAccount) is the proxy's business. Rename only if a source appears that is not "present as
  someone" from the agent's view.
- **19. Authority outside Kubernetes** (operator raised 2026-11-09). A Session-bound identity lifetime
  makes `Identity` the first object whose natural owner is a service database rather than the
  apiserver, which is [direction D4](architecture_overview.md#four-directions)'s case; CRs could
  remain as reconciled inputs for Flux-authored objects. Not decided here. This track keeps
  Kubernetes authoritative because every enforcement point (proxy informers, `policy/reader`,
  Action Service policies, MCP edge) consumes CRs by watch, and Dispatcher can own a CR's lifetime
  as well as a row's. Constraint so D4 stays open: services reference an identity by name and UID
  through the shared reader and nothing else, and no service's correctness depends on `Identity`
  being a Kubernetes object.
- **20. TODO: Accounts per preset vs per role set** (operator, 2026-11-09: per preset for now; reconsider).
  Per preset gives each agent one `kubectl` identity, holding its preset's role sets plus cluster
  diagnostics. Per role set is finer-grained, but an agent then needs a different token per command.
- **21. How the MCP edge proves an Identity to the Action Service: decided 2026-11-09 (operator).**
  Today the edge mints a TokenRequest for the ServiceAccount a Connection is bound to. A Connection
  bound to an `Identity` has no account to mint for, so the edge uses the
  [delegated authorization](../docs/delegated_authorization.md) contract instead: it authenticates
  as itself, the Action Service's per-method delegation rule names the edge's account as an
  allowed actor, and the envelope names the Identity as the effective caller once
  `SERVICES_IDENTITY_PRINCIPAL` gives it an identity reference. Trust is coarse: the Action
  Service accepts any Identity in the policy namespace from the edge, and the edge gates itself,
  binding Connections only to Identities in its `connection_identities` allowlist, each binding
  operator-approved at consent or rebind. This is the same trust as the other trusted writers
  (the `WorkloadCredential` requesters, Dispatcher for Sessions). A narrower check at the Action
  Service (a second allowlist, or a per-identity RBAC verb on the edge's account) is additive if
  the edge's internet exposure ever warrants it. Revisit the static allowlist if runtime-created
  identities need Connections; eligibility would then move onto the `Identity`. This is the shape
  `CENTRAL_SESSION_ACTING_DESIGN` defers for the sampler (question 6), so the two should share one
  design. Until `SERVICES_IDENTITY_PRINCIPAL` lands, the edge's `mintable_accounts` stays required
  with OAuth.

## Exit

The implementation is DAG section [14](task_dag.md#14-identity-and-kubernetes-accounts-cutover),
whose acceptance scenarios are: an SVID-authenticated egress hop and one platform route in the
system tests; a Sandbox with no Kubernetes use rule has no ServiceAccount and `kubectl` fails
closed; two identities act as one shared account and one of them loses it without the other
noticing; a temporary account expires and its SA, RoleBindings, grant, credential, set and use
binding are gone with no controller restart; deleting an identity removes everything it owned. When
the cutover lands, this file's content moves to `docs/` and the file is deleted.
