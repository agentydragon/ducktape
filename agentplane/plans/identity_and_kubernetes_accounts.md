# Identities, workload authentication and Kubernetes accounts

Status: **proposal, 2026-11-05.** The operator selected these directions that day (recorded in
[`NETWORK_WORKLOAD_IDENTITY`](task_dag.md#network_workload_identity--spiffe-as-the-one-way-agent-workloads-authenticate)
and [`AGENT_PRINCIPAL_MODEL_DESIGN`](task_dag.md#agent_principal_model_design--agentplane-principals-and-external-identities)):
agent workloads authenticate to the Runner Gateway and the central egress proxy with SPIFFE SVIDs,
not ServiceAccount tokens; Agentplane owns agent identity and no agent is provisioned a Kubernetes
ServiceAccount by default; a Kubernetes account is a resource several identities may act as, with
grants attached by separate objects rather than fields so Flux and runtime writers can share one
account; and the Agentplane-side binding and the Kubernetes-side grant are separate kinds, since
no rule type is meaningful on both. Decided the same day, on this plan's blocking questions:
first-party services verify the SVID themselves rather than trusting a proxy-asserted caller; the
proxy acts as a Kubernetes account by impersonation; Kubernetes rules live in `KubernetesRoleSet`
and a grant references sets; the managed-SA kind is `ManagedServiceAccount`. The remaining field
shapes are this plan's proposal; the implementation is DAG section
[14](task_dag.md#14-identity-and-kubernetes-accounts-cutover). Policy objects stay Kubernetes
objects.

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
| What Kubernetes enforces    | RoleBindings for the Pod's own SA   | `KubernetesAccountGrant` for any SA on any configured apiserver                      |
| Kubernetes reach            | The Pod's own SA                    | An account the identity may present a credential for, through the proxy; none unless |
| Execution principal records | ServiceAccount UID                  | Identity name and UID, plus the workload incarnation as provenance                   |

Two systems fall out, each with one kind of input: Agentplane policy (`PolicySet`, `PolicyBinding`,
`EgressCredential`, subject `Identity`) and a Kubernetes RBAC provisioner (`KubernetesRoleSet`,
`KubernetesAccountGrant`, subject a ServiceAccount). The Policy Controller is the provisioner, as it
already is for SA subjects today; the Crossplane managed-resource pattern is the same shape. The two
meet in exactly one place: an `EgressCredential` whose source names a ServiceAccount.

## Objects

### `Identity`

Namespaced in the policy namespace. The spec is a description; the identity is a subject and
nothing else, and must not grow harness, model or environment fields
([principles](../docs/principles.md) § no opaque bundles). Status is written by controllers:

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: Identity
metadata:
  name: agent-123
  namespace: agentplane-staging
  # no ownerReference: an identity is never owned by an environment
spec:
  description: Daily CI agent
status:
  workloadCredentials: # what authenticates as this identity; controller-written, never caller-supplied
    - spiffeID: spiffe://agentplane/environment-runtime/agentplane-staging/<sandbox-uid>/<credential-id>
    - serviceAccount: { namespace: agentplane-staging, name: openclaw } # workloads with no SVID only
```

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
`PolicySet` keeps `egress`, `action_auto_approve` and `session_inbox_access` and loses the two
Kubernetes rule types, so every rule in a set is enforced for every binding of it. Sets, `expiresAt`
and ownership are unchanged. "May act as a Kubernetes account" is an `egress` rule like any other
credential use:

```yaml
kind: PolicyBinding # the basic set carries the shared account's use rule for every agent
spec: { subject: { identity: agent-123 }, policySets: [basic, github-ducktape] }
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
    kubernetesServiceAccount: { apiServer: local, namespace: agentplane-k8s-accounts, name: shared-diagnostics }
  targets: [{ header: Authorization, method: schemeToken, scheme: Bearer }]
```

### `KubernetesRoleSet` and `KubernetesAccountGrant`: the provisioner's input

The two Kubernetes rule shapes move, unchanged, from `PolicySet` into a set of their own, so
"cluster diagnostics" stays one reusable name. A grant attaches sets to one ServiceAccount, named by
apiserver, namespace and name, with the binding's expiry and ownership semantics:

```yaml
kind: KubernetesRoleSet
metadata: { name: cluster-diagnostics }
spec:
  rules:
    - { type: role_binding, role: { kind: ClusterRole, name: agent-diagnostics }, namespaces: [cnpg, forgejo] }
    - { type: cluster_role_binding, clusterRole: agent-node-reader }
---
kind: KubernetesAccountGrant # Flux: standing roles on the shared account
metadata: { name: shared-diagnostics }
spec:
  account: { apiServer: local, namespace: agentplane-k8s-accounts, name: shared-diagnostics }
  roleSets: [cluster-diagnostics]
---
kind: KubernetesAccountGrant # app: every agent on the shared account gets more for an hour
metadata: { name: shared-diagnostics-cnpg-read-20261105 }
spec:
  account: { apiServer: local, namespace: agentplane-k8s-accounts, name: shared-diagnostics }
  roleSets: [cnpg-read]
  expiresAt: "2026-11-05T13:00:00Z"
```

Grants on one account combine additively; revoking one never removes what another grants, exactly as
bindings do today. The Policy Controller reads grants and role sets and nothing else; whether a
grant may instead list roles inline is open (question 10).

### `ManagedServiceAccount`: optional, for accounts Agentplane creates

A static account is a ServiceAccount Flux creates, referenced by the triple above; no object stands
in for it. An account Agentplane must create, such as a temporary one for a single agent, needs one
object to hold its expiry and owner, because a ServiceAccount in another namespace or on another
apiserver can carry neither an ownerReference to the identity nor an `expiresAt`:

```yaml
kind: ManagedServiceAccount
metadata:
  name: agent-123-extra
  ownerReferences: [{ kind: Identity, name: agent-123 }]
spec:
  apiServer: local
  expiresAt: "2026-11-05T12:34:00Z"
status:
  serviceAccount: { namespace: agentplane-k8s-accounts, name: agent-123-extra, uid: ... }
```

The Policy Controller creates the ServiceAccount, emits the account's `EgressCredential` and
one-rule `PolicySet` owned by it (the same two objects cdk8s renders for a static account), and
deletes the account at `expiresAt`. Grants and use bindings for a temporary account are owned by
it, so one clock covers the whole grant. The ServiceAccount lives in another namespace, or on
another apiserver, so no ownerReference reaches it: the controller labels it with the account's
UID and prunes it on reconcile once that account is gone, exactly as it prunes native RoleBindings
today, and never adopts one on name alone. No finalizer, for the reason the PolicySet ADR gives.

## Workload authentication

The SVIDs already exist: the Sandbox Controller requests one cert-manager `Certificate` per
incarnation with URI SAN `spiffe://agentplane/environment-runtime/<ns>/<sandbox-uid>/<credential-id>`
([Sandbox Controller SPEC](../sandbox_controller/SPEC.md)), and Environment Runtime presents it on the
control connection. Firecracker launcher delivery over the Workload API remains part of the launcher
integration. The egress sidecar or relay presents the same SVID on its hop to the central proxy in
place of the proxy-audience projected token; the proxy verifies it with the Gateway's transport package
(`environment_runtime_transport`) and resolves the Sandbox UID in the path to the Sandbox's
`Identity`. No SPIRE, Cilium mutual authentication or mesh is involved.

Rules then key on the identity. Everything the egress SPEC says about a subject holds with
"ServiceAccount" read as "Identity": a subject no binding names reaches no rule, and the connection
address is not identity evidence.

Workloads without an SVID, such as the public coder's OpenClaw fronted by the proxy, keep presenting
a projected SA token; `status.workloadCredentials` maps that account to its identity. The MCP edge
binds a Connection to an `Identity` instead of a ServiceAccount.

## Changing an environment's identity

The credential is not what makes this hard: the SVID names the Sandbox incarnation, not the
identity, and every verifier reads `spec.identity` from the live Sandbox, so a changed field takes
effect on the next request with the certificate untouched. What makes it hard is what is in flight
at the moment of the change: Sessions already bound to the old identity, whose execution principal
is immutable and which the same-environment invariant now bars from running there; requests and
tunnels admitted as one identity and completing as another; and a RAM-preserving Firecracker
snapshot, whose restore would resume the old agent's harness mid-turn acting as the new identity.

So `spec.identity` may be absent or changed while the environment is suspended or stopped, and a
start requires one (operator, 2026-11-05: changing it while suspended is wanted). The next start
runs as the new identity, and the controller rotates the credential ID on that start so the
boundary shows in every audit log as a new SPIFFE ID. If a RAM-preserving Firecracker snapshot
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
Nothing then holds an SVID for the agent, so the identity is proven by delegation rather than by a
workload credential:

- The Session names its `Identity` at `CreateSession`; Dispatcher is the authority for that
  association, and either creates a Session-managed identity or checks the caller may use an
  existing one. The identity's `status.workloadCredentials` may be empty.
- The sampler calls the Action Service, Notification and History as the actor under the
  [delegated authorization contract](../docs/delegated_authorization.md), with the effective caller
  an identity reference instead of an SA UID. The receiving service trusts the sampler to assert
  identities in its namespace, as the contract already provides, and applies the identity's
  bindings. The sampler's own ServiceAccount never becomes the agent's.
- Egress from the sampler itself is the sampler's: it reaches LLM ingress and the direct Anthropic
  route with its own credentials today and keeps doing so. The agent's egress rules apply where
  its code runs, which with no environment is nowhere; outbound reach for such a Session is Actions.
- Kubernetes without an environment is an Action or nothing. There is no `kubectl` process to
  present a placeholder, so a grant to an account is inert until an environment is attached; the
  `kubernetes_admin` Action path is what remains, and it is authorized by the identity's bindings.
- Attaching an environment later: the Sandbox's spec names the identity, and the proxy resolves the
  incarnation's SVID to that Sandbox and so to the identity, admitting it under the same bindings.
  Detaching, or deleting the environment, changes nothing about the identity or its grants. The
  same-environment invariant holds because the environment runs as exactly one identity, whichever
  Session attached it.

## Kubernetes execution

**Policy Controller, per ServiceAccount named by a live grant:** keep the RoleBindings that the
role sets of every live grant naming it expand to, exactly as it keeps them for an SA subject today
(additive, one native binding per grant, role and namespace, pruned when no live grant covers them,
never adopted on name alone). A grant naming a ServiceAccount that does not exist reports
`SubjectMissing`, as a binding does now. For a `ManagedServiceAccount`, also create the ServiceAccount
and prune it by UID label once the account is gone. Reconcile on start covers controller downtime,
as it does now.

**What revokes.** Three cuts, in the order they take effect:

1. The use rule leaving the identity's bindings. The proxy stops substituting on the next request;
   this is the cut that matters, and it needs no apiserver write.
2. The controller pruning the RoleBindings once no live grant covers them. RBAC matches a
   ServiceAccount by name, and impersonation does not require the object to exist, so until this
   pass an impersonated request still carries the old roles.
3. The ServiceAccount going. This matters only under TokenRequest, whose tokens are rejected by
   UID; under impersonation it revokes nothing by itself.

So a deleted ServiceAccount is not revocation, and a grant's expiry is only as prompt as the
controller's pass, the same as today.

**Egress proxy, acting as the account: impersonation** (decided 2026-11-05). The proxy's own
apiserver credential gets `impersonate` on `serviceaccounts` in the accounts namespace. Per request
it replaces the placeholder with its own bearer and adds
`Impersonate-User: system:serviceaccount:<ns>/<account>`; the apiserver adds the
`system:serviceaccounts` groups itself. No token is minted or cached, revocation is the rule going,
enforced on the next request, and the audit log records both the account and the proxy as original
user. Impersonating ServiceAccounts rather than users keeps OIDC users bound by name out of reach.
A TokenRequest source (`create serviceaccounts/token`, short TTL, cached) is not built; add it only
when a remote apiserver refuses the proxy impersonation rights.

A remote apiserver is a named target in both the controller's and the proxy's settings carrying its
kubeconfig, the way tofu-controller holds Forgejo credentials.

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
| `KubernetesAccountGrant`                     | Flux, Identity (runtime widening), `ManagedServiceAccount`                               | GC with its owner; the controller at `expiresAt`                        |
| `ManagedServiceAccount`                      | Identity                                                                                 | GC with the identity; the controller at `expiresAt`                     |
| Generated `EgressCredential` and `PolicySet` | `ManagedServiceAccount`, or Flux for a static account                                    | GC with the account; Flux prune                                         |
| ServiceAccount in the accounts namespace     | Flux, or the controller (UID-labelled)                                                   | Flux prune; the controller's prune pass                                 |
| Native RoleBindings                          | The controller (labelled, grant UID)                                                     | The controller's prune pass                                             |

Identities do not garbage-collect at all, by design: nothing in Kubernetes owns them. Three
things therefore need an explicit end: the app-created identity of an agent the operator is done
with, the Dispatcher-created identity of a central Session on whatever Session deletion turns out
to be, and anything on a remote apiserver, which the controller prunes with that apiserver's
credential. A leaked identity costs nothing while nothing can authenticate as it, but its bindings
remain latent authority for whoever can later run as it, which is why question 6 bounds what the
sampler may assert.

| Case                                  | Identity                                         | Kubernetes account                                                        | Grants and use                                  |
| ------------------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------- | ----------------------------------------------- |
| Standing agent (Haku, daily CI agent) | Flux                                             | Flux ServiceAccount, or none                                              | Flux grant; use rule in the agent's sets        |
| Agent started from the app            | The app at launch, free-standing                 | None, or the shared account via `basic`                                   | Identity-owned binding from the launch sets     |
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

- **CRDs and catalog:** `Identity`, `KubernetesRoleSet`, `KubernetesAccountGrant`,
  `ManagedServiceAccount`, the `Identity` binding subject, `PolicySet` without Kubernetes rule types,
  and the `kubernetesServiceAccount` credential source; Flux-rendered identities for the static
  agents (`claude-ai`, `haku-agent`) in place of their ServiceAccounts, the `shared-diagnostics`
  ServiceAccount with its grant, credential and set, every binding rewritten from its SA subject,
  every Kubernetes rule moved from its `PolicySet` to a role set, and the `basic` set carrying the
  shared account's use rule.
- **Controllers:** the Sandbox Controller requires `spec.identity`, creates no ServiceAccount, and
  owns nothing identity-related; the app creates a free-standing `Identity` and its launch binding
  before the Sandbox, or names an existing one; the Policy Controller reads grants and role sets,
  keeps `PolicyBinding` expiry, and manages `ManagedServiceAccount`s.
- **Data plane:** the egress sidecar and the Firecracker relay present the SVID on the hop; the
  proxy accepts SVIDs, resolves identities, and drops the proxy-audience and Kubernetes-audience
  projected tokens; the sandbox templates stop mounting a kubeconfig.
- **Services:** History, Dispatcher, Notification, the Action Service, LLM ingress and the central
  sampler authenticate, authorize and record by identity. Rows that pin an SA UID are rewritten by
  the migration where the identity is known and marked historical where it is not, as History's
  migration 8 did for harness provenance; same-principal reads of historical rows stay unavailable
  and operator access covers them.

What stops during convergence: every running Sandbox loses egress and platform routes from the
moment the proxy drops projected tokens until its sidecar image carries the SVID hop, and `kubectl`
until the shared account exists and the agent reads its credentials from the rules endpoint. The Gateway path is unaffected, since it
verifies SVIDs already. Running Sandboxes are replaced rather than patched: a Pod's ServiceAccount
cannot change in place, and the new launch writes the identity.

## Open questions and tricky bits

1. **Platform routes without a bearer: decided 2026-11-05.** Notification, Dispatcher, runner
   ingress, the Action Service and LLM ingress verify the SVID themselves: the proxy tunnels the
   TLS connection through, each service verifies the client certificate with the transport
   package and resolves the identity through the live Sandbox. The proxy's rule check no longer
   applies on those routes, so each service's own authorization is the whole gate there, and the
   proxy never asserts an identity. The sampler's zero-environment path stays on delegation.
   Still to settle per route: what each service does with the Pod-bound checks it makes today.
2. **One key, two holders.** Environment Runtime holds the SVID for the control connection; the
   relay would hold it for the hop. Either both read one Secret, or the controller issues a second
   certificate under the same Sandbox path for the relay. Agent code that can read Runtime's key can
   already act as the workload at the Gateway; presenting it at the proxy adds no reach beyond that
   identity's rules, but say so where the SPEC states what the certificate authenticates.
3. **SVID to identity.** The path carries the Sandbox UID and credential ID, not the identity. A
   status list of SPIFFE IDs on the identity is a cache that dangles once a Sandbox is gone while
   its Flux-owned identity stays. Prefer the Gateway's way: the proxy resolves the UID to the live
   Sandbox, checks the credential ID against its status, and reads `spec.identity`, so a deleted or
   replaced Sandbox fails the lookup with nothing to clean. That gives the proxy a Sandbox read it
   does not have today. `status.workloadCredentials` then holds only the SA-token entries for
   workloads without an SVID.
4. **Impersonation: decided 2026-11-05.** Still open: which apiservers the proxy and controller
   may reach, with what credential, and whether any of them refuses impersonation.
5. **Credential descriptions as the only instructions.** With no kubeconfig, the `description`
   on each Kubernetes credential is what tells an agent the host, the account and the `kubectl`
   invocation; settle the convention for that prose, generated by cdk8s and the controller alike,
   and whether the rules endpoint should carry a structured `howToUse` beside it for every
   credential kind, not Kubernetes alone.
6. **Central sampler as an identity.** Delegation today carries an SA UID as the effective caller;
   it must carry an identity reference, and the sampler's own account must never become the agent's.
   With zero environments the identity has no workload credential at all, so every service the
   agent reaches must accept the delegated form, and whether the sampler may assert any identity in
   the namespace or only those Dispatcher has bound to its Sessions is the trust decision to make.
7. **Stored principals.** History's `execution_principal` and Dispatcher rows pin SA UIDs. The
   cutover migration rewrites rows whose Sandbox still exists to its identity and marks the rest
   historical; confirm that is enough for the retained Sessions the operator wants readable, since
   same-principal reads of historical rows are unavailable.
8. **Same-environment invariant** ([identity contract](task_dag.md#session_identity_contract--session-identity-and-shared-environment-admission))
   becomes "same identity"; co-resident Sessions share one SVID, so it holds by construction, and
   admission must still refuse a Session whose identity differs.
9. **Revocation granularity.** Removing an identity's rule stops substitution on the next request;
   an established TLS connection to the apiserver carrying earlier requests is not cut. State whether
   the proxy ends tunnels on rule loss, as the egress SPEC does for other credentials.
10. **Role sets: decided 2026-11-05.** A grant references `KubernetesRoleSet`s; no inline roles.
    Still open: whether the app may author role sets, or only grants to Flux-authored ones.
11. **Admission for controller-written policy.** The Policy Controller writing a `PolicySet` and an
    `EgressCredential` per managed account is new authority; an admission rule limits it to the
    one-rule shape naming that account's own ServiceAccount and apiserver host.
12. **Ending an identity.** The identities list and the finish-time offer above cover the operator
    path. Open: whether a one-off agent's identity may carry an `expiresAt` the Policy Controller
    honours, and what "referenced" means for a retired Sandbox that could still resume.
13. **Second control plane.** Accounts live on target apiservers regardless of where Agentplane's
    records live, so this plan is neutral across the [architecture directions](architecture_overview.md);
    `Identity`, grants and managed accounts move with the Sandbox family under D3.

## Exit

The implementation is DAG section [14](task_dag.md#14-identity-and-kubernetes-accounts-cutover),
whose acceptance scenarios are: an SVID-authenticated egress hop and one platform route in the
system tests; a Sandbox with no Kubernetes use rule has no ServiceAccount and `kubectl` fails
closed; two identities act as one shared account and one of them loses it without the other
noticing; a temporary account expires and its SA, RoleBindings, grant, credential, set and use
binding are gone with no controller restart; deleting an identity removes everything it owned. When
the cutover lands, this file's content moves to `docs/` and the file is deleted.
