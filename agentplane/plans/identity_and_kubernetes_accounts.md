# Identities, workload authentication and Kubernetes accounts

Status: **expand phase on staging; the switch waits on open question 11, 2026-11-10.** Directions selected by the operator on
2026-11-05 ([`AGENT_PRINCIPAL_MODEL_DESIGN`](task_dag.md#agent_principal_model_design--agentplane-principals-and-external-identities)):
SVIDs, not ServiceAccount tokens, authenticate agent workloads; Agentplane owns agent identity and
no agent gets a ServiceAccount by default; a Kubernetes account is a resource several identities
may act as, granted by separate objects; first-party services verify the SVID themselves; the proxy
acts as an account by impersonation. Policy objects stay Kubernetes objects. Status and
dependencies are DAG section [14](task_dag.md#14-identity-and-kubernetes-accounts-cutover).

The landed contracts are in the kind doc comments (`agentplane/policy/api/v1alpha1/types.go`), the
[Policy Controller SPEC](../policy_controller/SPEC.md) (grants, managed ServiceAccounts, expiry,
`PolicyService` Identity watches and deletes), the [egress SPEC](../egress/SPEC.md) § Decision
(impersonation), [workload authentication](../docs/workload_authentication.md) (per-incarnation
`WorkloadCredential`s) and [agent RBAC](../../cluster/docs/agent_rbac.md#kubernetes-accounts) (the
accounts the catalog renders behind `Environment.identity_catalog`). What follows is what the
cutover still changes.

## Target: one identity, three credential forms

Operator decision, 2026-11-10. This is the contract the cutover works toward;
[where the migration stands](#where-the-migration-stands) says how far it has got. The Agentplane `Identity` is the one identity source. Each credential
form below is an explicit, named mapping to one `Identity`:

1. **X.509 SVID over mTLS**, for in-cluster workloads: a `WorkloadCredential` names the Identity.
2. **OAuth2 access tokens issued by the MCP edge's own authorization server**
   (`mcp_edge/authserver`, dynamic client registration), for external MCP clients such as claude.ai:
   a grant's Connection is bound to the Identity.
3. **Kubernetes' own authentication**, only where a component talks to the Kubernetes API: as
   itself, through Flux, or as an account by the proxy's impersonation.

**Why (operator, 2026-11-10):** to keep Agentplane identity from being hard-baked to the Kubernetes
API server. An identity that only a ServiceAccount and TokenReview can carry ties every caller to a
cluster-hosted Pod; one the Agentplane issuer owns can also act for a central Session with no
Sandbox, or later an enrolled external machine. The central-Session decoupling did not strictly
require the cutover, but building it on ServiceAccounts would add a contract the cutover has to undo.

A Kubernetes ServiceAccount TokenReview is not a second identity source for agents.

**Backend services (operator, 2026-11-10).** Agentplane's own services keep authenticating to each
other as themselves with ServiceAccount tokens and TokenReview for privileged service-to-service
calls; that is service identity, not agent identity, and keeps the API server dependency to the
platform's internal hops only. No service holds a private key or certificate that resolves to an
agent `Identity`: a service acting for an agent does so only under the
[delegated authorization contract](../docs/delegated_authorization.md), authenticating as itself and
naming the `Identity` as effective caller. Service-class SVIDs (a SPIFFE path resolvers never map to
an agent `Identity`) stay open for later; nothing now needs them.

Other bearer forms are not planned and no task depends on them; if we wanted, we could later accept
bearer tokens resolving to the same `Identity`. The paths that do not meet this yet, with the
bucket each falls in, are the
[workload identity lifecycle's inventory](workload_identity_lifecycle.md#remaining-paths).

## Where the migration stands

The transport is in place; the subject it resolves to is still a ServiceAccount. Verified against
the code on 2026-11-10:

| Area                        | Today                                                                                                                                                                                                                                                                                                                                                                                         | Goal                                                                                                                                                                      |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Identity` kind             | Exists (`policy/api/v1alpha1/types.go`); a `PolicyBinding` subject may name one, which only `policy/reader`'s Identity lookups resolve. Staging renders none: the catalog is behind `identity_catalog=False` (`cluster/cdk8s/agentplane/staging.py`)                                                                                                                                          | Every agent binding names an `Identity`; the ServiceAccount subject is gone (`IDENTITY_CUTOVER`)                                                                          |
| Sandbox identity            | `Sandbox.spec.identity` and its only-while-stopped admission rule exist (`sandboxes/api/types.go`); the controller ignores it and creates and binds the Sandbox's own ServiceAccount (`sandbox_controller/identity.go`)                                                                                                                                                                       | The controller acts as `spec.identity` and creates no account (`SANDBOX_IDENTITY_SPEC`)                                                                                   |
| Sandbox egress              | SVID over mTLS, resolved through the live `WorkloadCredential`, whose subject is the Sandbox's own ServiceAccount. The proxy decides an Identity subject by the bindings naming it and impersonates accounts it has a use rule for ([egress SPEC](../egress/SPEC.md#decision)), but no record names one. The Sandbox no longer projects a Kubernetes token (#15258), so `kubectl` has no path | Every Sandbox record names its `Identity` and `kubectl` acts as a shared account by impersonation (`SANDBOX_IDENTITY_SPEC`)                                               |
| Environment Runtime control | Gateway and runner authenticate each other by SVID; the runner's certificate is the Sandbox's `WorkloadCredential`'s, subject the Sandbox's ServiceAccount (`sandbox_controller/environment_runtime_credential.go`, [runner control](../docs/runner_control.md))                                                                                                                              | Unchanged transport; the record names the `Identity`                                                                                                                      |
| Service principal           | `auth.Subject` is `Principal` (ServiceAccount) or `IdentityPrincipal` (`workload_auth/auth/auth.go`); every service admits the union on its workload-facing methods, given its `policy_namespace`. The delegation envelope's effective caller is a ServiceAccount (`workload_auth/auth/delegation.go`); #15389 adds the Identity form beside it                                               | The envelope names an Identity only; `Principal` means "platform component" (`IDENTITY_CUTOVER`)                                                                          |
| Session identity            | `SessionSpec` has no identity (`proto/session.proto`). `CreateSession` derives the execution principal from the placed Sandbox's own ServiceAccount and pins its UID, and records the Sandbox's `spec.identity` beside it (`runner_gateway/dispatcher/api.go`); delivery refuses a mismatch of either (`delivery.go`). An Identity caller's `CreateSession` is refused (#15387)               | A Session has an Identity, never a ServiceAccount (`SESSION_SA_PRINCIPAL_REMOVAL`); a central Session names it at `CreateSession` (`CENTRAL_SESSION_WITHOUT_ENVIRONMENT`) |
| History                     | Rows pin `execution_principal_*` and `writer_*` ServiceAccount namespace, name and UID, with Identity name and UID beside them since migration 13 (`history_service/migrations/history/`)                                                                                                                                                                                                     | Identity name and UID only, with the workload incarnation as provenance (`SESSION_SA_PRINCIPAL_REMOVAL`)                                                                  |
| App API                     | Operator OIDC cookie session or ID-token bearer, which the k3s system tests sign under a pinned `.test` issuer key; or a TokenReview bearer for `agentplane-agent`, which only staging live suites and hand-run scripts still use (`app/go/auth/auth.go`, `caller`)                                                                                                                           | `APP_AGENT_TOKEN_DECISION`                                                                                                                                                |
| MCP edge                    | OAuth tokens resolve to a Connection bound to a ServiceAccount (staging: `claude-ai`); the edge forwards a TokenRequest for it; `connection_identities` is parsed but unread (`mcp_edge/config.go`, `mcp_edge/connections/service.go`)                                                                                                                                                        | Connections bind to an `Identity`; the edge delegates as itself (`IDENTITY_CUTOVER`, [below](#mcp-edge))                                                                  |

Session work built before the cutover keeps these ServiceAccount pins working and adds no new
ServiceAccount-keyed contract; [`SESSION_IDENTITY_CONTRACT`](task_dag.md#session_identity_contract--session-identity-and-shared-environment-admission)
states the Session rules against the goal.

## What this separates

Today the Sandbox-owned ServiceAccount plays every role below, with the Sandbox's lifetime:

| Role                        | Today                               | After                                                                     |
| --------------------------- | ----------------------------------- | ------------------------------------------------------------------------- |
| Who the agent is            | ServiceAccount name and UID         | `Identity`                                                                |
| What Agentplane enforces    | `PolicyBinding` to a ServiceAccount | `PolicyBinding` to an `Identity`; Kubernetes rule types leave `PolicySet` |
| What Kubernetes enforces    | RoleBindings for the Pod's own SA   | `ServiceAccountGrant` on a ServiceAccount in the accounts namespace       |
| Kubernetes reach            | The Pod's own SA                    | An account the identity has a use rule for, through the proxy; else none  |
| Execution principal records | ServiceAccount UID                  | Identity name and UID, plus the workload incarnation as provenance        |

Agentplane policy (`PolicySet`, `PolicyBinding`, `EgressCredential`; subject an `Identity`) and the
Kubernetes RBAC provisioner (`KubernetesRoleSet`, `ServiceAccountGrant`; subject a ServiceAccount)
meet only in an `EgressCredential` whose source names a ServiceAccount. `PolicyBinding.spec.subject`
names exactly one `Identity`: no ServiceAccount, Group or selector subject. A `PolicySet` bound to
many identities is the set-of-identities mechanism, and a Flux-rendered binding per standing
identity is the roster. `PolicySet` keeps `egress`, `action_auto_approve` and
`session_inbox_access`, so every rule in a set is enforced for every binding of it. The
impersonation credential keeps the name `EgressCredential`: to the agent it is one more credential
it may present a placeholder for.

## Identity lifetime

An identity is never owned by a Sandbox or any other environment (operator, 2026-11-05): a central
Session attaches and detaches environments repeatedly, and a standing agent moves between Sandboxes.
Who creates it decides its lifetime: Flux for a standing agent; the app at launch, deleted by the
operator's explicit "finish with this agent"; Dispatcher for a Session-managed identity, deleted
with the Session. Every binding, grant and `ManagedServiceAccount` of the agent is owned by the
identity, so its whole authority goes with one delete and survives any number of environment
changes. Deleting an environment never deletes the identity; deleting the identity leaves an
environment naming it unable to start (`IdentityMissing`). A leaked identity costs nothing while
nothing authenticates as it, but its bindings stay latent authority for whoever can later run as it.

Identities that exist for a single Session stay wanted from the app (operator, 2026-11-10): "create
a Session with a fresh identity just for it". Who creates one, how its lifetime follows the
Session's, its cleanup, and whether it ever needs a Kubernetes account of its own are open under
[`SESSION_IDENTITY_PROVISIONING_DESIGN`](task_dag.md#session_identity_provisioning_design--session-scoped-identities);
open question 7 below is the creator half.

A managed account's `EgressCredential` and one-rule `PolicySet` are emitted by the controller and
owned by the account, so a temporary account's grants, use bindings, credential and set go with its
one clock ([Policy Controller SPEC](../policy_controller/SPEC.md#managed-serviceaccounts)).

**Acting as an Identity, and what it implies** (operator, 2026-11-10).

- **An Identity is the isolation boundary.** One Session has one Identity, and Sessions sharing an
  Identity have the same access by design; to isolate, use different Identities. An Identity is not
  assumed to be one-to-one with a Sandbox: several may be bound to it.
- **Acting in a workload is implied for its bound Identity**: exec in a Sandbox or box, reading its
  files, interacting with Sessions running as that Identity. A harness-hosted Session already
  executes commands inside the Sandbox it runs in, as that Sandbox's Identity; any Session of
  Identity I executing in any Sandbox bound to I is the transitive closure of that, and grants
  nothing I does not already have.
- **Messaging between Sessions of the same Identity is implied**, since the recipient could learn the
  same itself, and so is a resume or wake that such a send causes, under the recipient's resume
  policy, for multi-agent use. Only sending across Identities needs an explicit grant
  (`session_inbox_access`). This is the intended design under
  [`SESSION_IDENTITY_AUTHZ`](task_dag.md#session_identity_authz--session-reads-and-sends-authorized-by-identity);
  Notification's send check today still requires a `session_inbox_access` binding even for a
  same-Identity send (#15374).
- **Lifecycle control needs an explicit grant**: create, resume, wake, suspend, delete or rebind a
  Sandbox or box as a direct request, or create Sessions. A grant is a `PolicySet` rule bound to a
  subject, which may be I if someone writes that binding, never implied. Explicit environment
  rules, saying who may resume, wake or manage which environments, are the mechanism; no such
  `PolicySet` rule type exists yet (`Rule.Type` has `kubernetes_role_binding`,
  `kubernetes_cluster_role_binding`, `egress`, `action_auto_approve`, `session_inbox_access`), an
  open item tied to open question 11 and `SESSION_IDENTITY_AUTHZ`. Its proposed shape, the
  `environment` rule type, is [The `environment` rule type](environment_rules.md).
- **Action boxes:** creating one is lifecycle. Today it is granted explicitly, through the Action
  Service's `action_auto_approve` policy on the effective caller. **Open consideration** (operator,
  not decided): an agent creating its own boxes with automatic approval is a deliberate path, so MCP
  clients can run bash and use egress credentials; if needed it can stay a privileged component
  (the MCP edge, acting for the Identity through delegation, under policy) rather than the
  Identity's own SVID being allowed to create boxes directly.
- Deleting an Identity revokes what a workload acting as it can do, since its credentials stop
  resolving, but deletes no Sandbox, environment or box. `Sandbox.spec.identity` is a name-based
  reference: a Sandbox acts as whichever Identity holds that name now, so a recreated same-named
  Identity becomes its acting identity again, while the Sandbox's credentials, which pin the
  incarnation's UID, stop working. A `WorkloadCredential`'s Identity `uid` is optional (operator,
  2026-11-10): a record naming one binds that incarnation and fails closed on a replacement; a
  record without one follows the live Identity of that name. Pinning is the requester's explicit
  choice: the Sandbox Controller and the Action Service pin, a Flux-rendered standing record cannot
  know the UID of an Identity applied beside it and does not.

## Changing an environment's identity

`Sandbox.spec.identity` may be absent or changed while the environment is suspended or stopped, and
a start requires one (operator, 2026-11-05). The SVID names the incarnation and verifiers resolve
the identity through its `WorkloadCredential`, so the next start is a new binding with a new
credential ID, and the boundary shows in every audit log as a new SPIFFE ID. A RAM-preserving
Firecracker snapshot is discarded and the guest cold-started, since its restore would resume the
old agent's harness acting as the new identity. An environment may therefore be created unbound and
bound by the Session that attaches it.

The same-environment invariant becomes "same identity": co-resident Sessions share one SVID, and
admission refuses a Session whose identity differs. A change is admitted only when no Session of a
different identity is placed on or attached to the environment (operator, 2026-11-05: never pull
the identity out from under a harness); `EnvironmentService`, in Dispatcher's process, refuses with
the Sessions named. A bypass such as `kubectl edit` is caught by Dispatcher's delivery and wake path
refusing a mismatched Session, so it strands Sessions rather than re-attributing them. Workspace and
state volumes carry over: the next identity reads whatever the previous one left, including anything
privileged it wrote there. That is an accepted risk of choosing to change an environment's identity,
not something the controllers prevent (operator, 2026-11-10); the app shows it when an identity
change is requested, and the environment's page lists the identities it has run as.

## Central Sessions with no environment

Decoupling a central Session from any Sandbox, so it exists with none and attaches and detaches
environments, is a purpose of this migration (operator, 2026-11-10). With
`SERVICES_IDENTITY_PRINCIPAL`, a central-sampler Session names its `Identity` at `CreateSession`;
Dispatcher either creates a Session-managed identity or checks the caller may use an existing one.
Today a Session records a ServiceAccount `execution_identity` instead
(`runner_gateway/dispatcher/session_dispatcher.proto`).

Nothing acts as that identity without an environment
([`CENTRAL_SESSION_ACTING_DESIGN`](task_dag.md#central_session_acting_design--a-central-session-acting-as-its-identity)):
the sampler calls LLM ingress, History and the Runner Gateway as its own account, which never
becomes the agent's, and no service accepts a delegated identity from it. Tool calls run in an
attached environment and reach Agentplane services with that environment's SVID, so attaching makes
the Sandbox's identity act for the Session and detaching leaves nothing acting. That is sound only
once a Sandbox's SVID resolves to `Sandbox.spec.identity` (`SANDBOX_IDENTITY_SPEC`) and attachment
admits only the Session's identity. The sampler's egress stays its own, and a grant to a Kubernetes
account is inert until an environment is attached.

## Kubernetes execution

No kubeconfig (operator, 2026-11-05): the agent learns how to reach Kubernetes as it learns any
credential. The rules endpoint lists what its identity may present, and each `EgressCredential`'s
`description` names the apiserver host, the account and the `kubectl --server <host> --token
<placeholder>` invocation; the catalog already renders that text (`kubernetes_accounts.py`). Nothing
in the sandbox holds a real token, and a new grant needs no file to change. Only the local cluster
is modelled.

## MCP edge

Decided by the operator on 2026-11-09. A Connection bound to an `Identity` has no account to mint a
TokenRequest for, so the edge uses the [delegated authorization](../docs/delegated_authorization.md)
contract: it authenticates as itself (its own ServiceAccount token; backend services keep TokenReview, operator 2026-11-10), the Action Service's per-method delegation rule names the
edge's account as an allowed actor, and the envelope names the Identity as the effective caller
once `SERVICES_IDENTITY_PRINCIPAL` gives it an identity reference. The Action Service accepts any
Identity in the policy namespace from the edge; the edge binds Connections only to Identities in its
`connection_identities` allowlist, each binding operator-approved at consent or rebind, the same
trust as the `WorkloadCredential` requesters and Dispatcher. A narrower check at the Action Service
is additive if the edge's exposure warrants it. A static name list cannot cover runtime-created or
ephemeral identities (operator, 2026-11-10): eligibility belongs on the `Identity` or on the binding
record, not on a list of names in the actor's config, so `connection_identities` is reworked at
`IDENTITY_CUTOVER` rather than populated. One design for every delegating actor (operator,
2026-11-10): Actions, the edge and the central sampler each authenticate as themselves, receivers
trust them per method and by namespace scope, and the effective caller comes from the actor's own
durable binding record. `CENTRAL_SESSION_ACTING_DESIGN` uses it. Until
`SERVICES_IDENTITY_PRINCIPAL`, the edge's `mintable_accounts` stays required with OAuth. Today a
grant still resolves to its Connection's bound ServiceAccount, and the edge forwards a minted token
for it ([lifecycle](workload_identity_lifecycle.md#remaining-paths) rows 1 and 2).

## Operator UI

Nothing in Kubernetes owns an identity, so the app shows identities as first-class objects and lets
the operator end them (operator, 2026-11-05):

- **Identities list:** origin (git, app, Session), what references it (Sandboxes, Sessions,
  Connections), bindings with expiry, the accounts it may present, last activity, and an
  unreferenced filter so a leak is visible.
- **Delete:** refused while referenced, naming the references; git-origin identities are
  read-only. The check is advisory; a concurrent launch naming a deleted identity fails closed.
- **Finish an agent:** the Thread's finish and a Sandbox delete offer to take the identity along when
  nothing else names it, selected by default only for an identity the app minted for that launch
  (an annotation records it at creation).
- **Start a session** (operator, 2026-11-05): a new identity takes the picked sets as its launch
  binding; an existing identity hides the set selector and shows its bindings read-only. Widening
  is a deliberate grant with its own expiry on the identity's page. The same form chooses placement
  (new environment, existing environment or none); an existing environment fixes the identity to its
  own. One submit makes `CreateEnvironment` then `CreateSession`, or just the second.

## Cutover

One incompatible cutover, not a staged migration (operator, 2026-11-05; [`AGENTS.md`](../AGENTS.md)
§ Deployed state is disposable): no dual readers, no ServiceAccount binding subject, no tolerant
decoding of SA-keyed rows. History, Dispatcher, Notification, the Action Service and LLM ingress
authenticate, authorize and record by identity; rows pinning an SA UID are rewritten where the
identity is known and marked historical otherwise, as History's migration 8 did. What stops during
convergence: decisions for a running Sandbox until proxy, services and bindings agree, and `kubectl`
from the moment the proxy drops the Kubernetes-audience token until the agent reads the shared
account's credential. Running Sandboxes are replaced, since a Pod's ServiceAccount cannot change in
place.

**End state for Sessions** (operator, 2026-11-10): a Session has an Identity, never a ServiceAccount;
Kubernetes is reached only at egress, by impersonation through a use rule. The services get there by
expand → switch → contract: they first accept Identity callers and record Identities beside the
ServiceAccount (`SERVICES_IDENTITY_PRINCIPAL`, `EGRESS_IDENTITY_RULES`), Sandboxes then run as their
Identity (`SANDBOX_IDENTITY_SPEC`), and the Session ServiceAccount paths are then deleted
([`SESSION_SA_PRINCIPAL_REMOVAL`](task_dag.md#session_sa_principal_removal--sessions-carry-no-serviceaccount)). Workload and Session
principals end as Identities. The callers that stay ServiceAccount-authenticated are privileged
platform components (the app, central sampler, Action Service executors, MCP edge, gateway proxy),
authorized by component role rather than by bindings; one acting for an Identity names it through
[delegated authorization](../docs/delegated_authorization.md), whose effective caller becomes an
Identity only (`IDENTITY_CUTOVER`).

## Open questions and tricky bits

1. **Pod-bound checks per route.** Services verify the SVID themselves (decided 2026-11-05); settle
   per route what each does with the Pod-bound checks it makes today.
2. **Credential descriptions as the only instructions.** Settle the convention for that prose,
   generated by cdk8s and the controller alike, and whether the rules endpoint carries a structured
   `howToUse` for every credential kind.
3. **Stored principals.** Confirm that rewriting rows whose Sandbox still exists and marking the rest
   historical is enough for the retained Sessions the operator wants readable, since same-principal
   reads of historical rows are unavailable.
4. **Who authors role sets.** Whether the app may author `KubernetesRoleSet`s, or only grants to
   Flux-authored ones.
5. **Ending an identity.** `Identity` carries no `expiresAt` (operator, 2026-11-09). Open: what
   "referenced" means for a retired Sandbox that could still resume.
6. **An `IdentityGroup` kind** (operator asked 2026-11-09), only if many short-lived identities need
   one grant that cannot be their launch set. Label selection stays rejected: namespaces are named,
   never selected.
7. **Who mints identities.** Recommendation: collapse the app-minted path into Dispatcher's, so every
   non-Flux identity is Session-managed and the finish dialog deletes it through the Session; the
   launch form already calls `CreateSession`, and create-then-bind makes the Sandbox-first order
   unnecessary.
8. **API groups by domain** (operator asked 2026-11-09). Environments, Agentplane identity and
   affordances, and Kubernetes grants share one API group; the grant kinds' Go package sits in
   `policy/api`. Recommendation: three groups at `IDENTITY_CUTOVER`, which rewrites every policy
   object anyway, moving the grant kinds' package with them.
9. **Authority outside Kubernetes** (operator raised 2026-11-09). A Session-bound lifetime makes
   `Identity` the first object whose natural owner is a service database
   ([direction D4](architecture_overview.md#four-directions)). This track keeps Kubernetes
   authoritative, since every enforcement point consumes CRs by watch. Constraint so D4 stays open:
   services reference an identity by name and UID through the shared reader only, and no service's
   correctness depends on `Identity` being a Kubernetes object. Accounts live on the local apiserver
   whichever direction is chosen.
10. **Accounts per preset or per role set** (operator, 2026-11-09: per preset for now; reconsider).
    Per role set is finer-grained, but an agent then needs a different token per command.
11. **Who creates the Identity a launched Sandbox runs as: settled, separate operations**
    (operator, 2026-11-11). A launch is three operations, and no environment operation creates or
    deletes an Identity:
    1. Create the `Identity` (the Identity API the app's identity pages use).
    2. Grant it the launch's PolicySets: ordinary `PolicyBinding`s naming it as subject. Who may
       create those is `AGENT_LAUNCH_POLICY_DESIGN`'s.
    3. `CreateEnvironment` with `spec.identity` naming it, or bind an existing stopped environment to
       it, as admission already allows. `CreateEnvironment` takes no PolicySets.

    The app's launch form and `LaunchSandbox` may run the three for the caller as one action; they
    stay separate operations underneath. Deleting an environment never deletes its Identity. On
    delete, the app checks whether anything else still uses the Identity and offers to delete it as
    well; the check is advisory, and the Identity delete guard
    (`IDENTITY_CLEANUP_REFERENCE_DECISION`) is authoritative, refusing while a Sandbox, Session,
    credential or Connection still uses it. Accepted consequences: a launch failing after step 1
    leaves an Identity and its grants, which the app's unreferenced filter shows (the form offers
    cleanup on failure), and Identities left by `LaunchSandbox` or the system-test driver remain until
    deleted; a cleanup job can come later if they accumulate. Central Sessions keep naming their
    Identity at `CreateSession`; question 7 is independent of this.

## Exit

The DAG's `IDENTITY_SYSTEM_TESTS` scenarios gate `IDENTITY_CUTOVER`. When the cutover lands, this
file's content moves to `docs/` and the owning SPECs, and the file is deleted.
