# Identities, workload authentication and Kubernetes accounts

Status: **Sandbox Identity switch serving and live acceptance complete (2026-11-12).**
Question 11 is settled. See [the rollout record](../docs/sandbox_identity_cutover_2026_11_12/README.md) for deployed evidence
and [the DAG](task_dag.md#14-identity-and-kubernetes-accounts-cutover) for remaining work.
Directions selected by the operator on
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
   (`mcp_edge/internal/authserver`, dynamic client registration), for external MCP clients such as claude.ai:
   a grant's Connection is bound to the Identity.
3. **Kubernetes' own authentication**, only where a component talks to the Kubernetes API: as
   itself, through Flux, or as an account by the proxy's impersonation.

**Why (operator, 2026-11-10):** to keep Agentplane identity from being hard-baked to the Kubernetes
API server. An identity that only a ServiceAccount and TokenReview can carry ties every caller to a
cluster-hosted Pod; one the Agentplane issuer owns can also act for a central Session with no
Sandbox, or later an enrolled external machine. The central-Session decoupling did not strictly
require the cutover, but building it on ServiceAccounts would add a contract the cutover has to undo.

A Kubernetes ServiceAccount TokenReview is not a second identity source for agents.

**Backend services (operator, 2026-11-14: "One auth mode can fire off").** Every first-party
service-to-service call authenticates its caller by a service SVID over mTLS with verified server
TLS. This supersedes the 2026-11-10 decision to retain backend ServiceAccount tokens and TokenReview.
Service SVIDs use SPIFFE paths that resolvers never map to an agent `Identity`; services read
certificate and trust-bundle files, issued in-cluster by cert-manager from the workload CA. A service
acting for an agent proves itself with its certificate and names the effective Identity in the typed
[delegation envelope](../docs/delegated_authorization.md). Its identity never becomes the effective
caller's grant. Kubernetes authentication remains only toward the Kubernetes API; operators retain
app OIDC and MCP clients the edge's OAuth tokens.

[`PRIVILEGED_BACKEND_AUTH_DESIGN`](task_dag.md#privileged_backend_auth_design--one-service-authentication-mode)
tracks the staged receiver/caller cutover and image-pin gates. The app's special `agentplane-agent`
TokenReview bearer also goes (operator, 2026-11-14 05:05–05:06 UTC), independently of those stages;
[`APP_AGENT_TOKEN_DECISION`](task_dag.md#app_agent_token_decision--the-apps-agentplane-agent-token-under-the-target)
tracks callers first, then manifest removal, settings/code removal after rollout, and RBAC last.
No Agentplane receiver admits a Kubernetes bearer at the exit.

## Where the migration stands

The source cutover and Sandbox acceptance are complete. This inventory records the current contracts;
deployment-specific observations remain separate in the DAG, including the standing-workload proxy
decision-log check. It does not claim that every workload rollout has been observed.

| Area                   | Current contract                                                                                                                                                                                                                                                                                                                 | Remaining question or evidence                                                                                             |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `Identity` and Sandbox | Workload `PolicyBinding`s and `WorkloadCredential`s use Identity subjects; component bindings may still name ServiceAccounts. The Sandbox controller pins the live Identity UID; Sandbox live acceptance, including owned cleanup, is recorded in the [rollout evidence](../docs/sandbox_identity_cutover_2026_11_12/README.md). | No repeat Sandbox rollout gate. Standing-workload proxy decision-log observation remains separately tracked in the DAG.    |
| Service authentication | Workload SVIDs resolve to Identity subjects; old ServiceAccount-subject `WorkloadCredential`s fail closed. Direct component ServiceAccount authentication and provenance remain supported. Session delegation records the effective Identity for workload requests.                                                              | Component actor proof moves to service SVIDs; provenance remains separate.                                                 |
| Session and History    | New Sessions execute as an exact Identity incarnation. History workload reads scope to the recorded Session Identity; writer and direct component-reader provenance remains independently recorded.                                                                                                                              | Broader cross-principal History reads remain held by `THREAD_READ_POLICY_DESIGN`.                                          |
| Notification inbox     | Inbox owners pin the Session Identity and destination Sandbox UID. Same-Identity sends pass directly; cross-Identity sends still require `session_inbox_access` under the current Sandbox/tag target model.                                                                                                                      | Target/group semantics and grant lifetime remain unresolved in `SESSION_ACCESS_GROUPING_DESIGN`; enforcement is unchanged. |
| App API                | Operator OIDC session or ID-token bearer (`app/internal/auth/auth.go`); Kubernetes bearers are rejected.                                                                                                                                                                                                                         | Pin the code removal, then retire the special agent ServiceAccount and token RBAC (`APP_AGENT_TOKEN_DECISION`).            |
| MCP edge               | OAuth tokens resolve to a Connection bound to an exact live Identity. The edge calls Action Service as itself and carries typed Connection/grant provenance; it does not mint or forward a workload token.                                                                                                                       | No workload-identity cutover remains for this path.                                                                        |

The retained ServiceAccount columns and wire fields are historical/component provenance, not
workload authority. [`SESSION_IDENTITY_CONTRACT`](task_dag.md#session_identity_contract--session-identity-and-shared-environment-admission)
continues to describe Session admission and retention.

## What this separates

The former Sandbox-owned ServiceAccount combined the roles below. The Sandbox source switch and live
acceptance are complete; ServiceAccounts remain for platform component identity and Kubernetes API
access through explicit grants and impersonation:

| Role                        | Former model                        | Target                                                                    |
| --------------------------- | ----------------------------------- | ------------------------------------------------------------------------- |
| Who the agent is            | ServiceAccount name and UID         | `Identity`                                                                |
| What Agentplane enforces    | `PolicyBinding` to a ServiceAccount | `PolicyBinding` to an `Identity`; Kubernetes rule types leave `PolicySet` |
| What Kubernetes enforces    | RoleBindings for the Pod's own SA   | `ServiceAccountGrant` on a ServiceAccount in the accounts namespace       |
| Kubernetes reach            | The Pod's own SA                    | An account the identity has a use rule for, through the proxy; else none  |
| Execution principal records | ServiceAccount UID                  | Identity name and UID, plus the workload incarnation as provenance        |

Agentplane policy (`PolicySet`, `PolicyBinding`, `EgressCredential`) and the Kubernetes RBAC
provisioner (`KubernetesRoleSet`, `ServiceAccountGrant`; subject a ServiceAccount) meet only in an
`EgressCredential` whose source names a ServiceAccount. A workload `PolicyBinding.spec.subject`
names exactly one `Identity`; component bindings may still name a ServiceAccount. A `PolicySet`
bound to many identities is the set-of-identities mechanism, and a Flux-rendered binding per
standing identity is the roster. `PolicySet` keeps `egress`, `action_auto_approve` and
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
- **Messaging between Sessions of the same Identity is allowed** without a grant; a send may wake
  the recipient under its existing resume policy. Cross-Identity sends still require
  `session_inbox_access`, whose current targets are Sandboxes and their tags. The inbox owner and
  History workload-read paths now use the recorded Session Identity, but the rule's target/group
  model and grant lifetime remain unresolved in
  [`SESSION_ACCESS_GROUPING_DESIGN`](task_dag.md#session_access_grouping_design--access-groups-across-independent-sessions-and-environments).
  Keep that enforcement and preserve component senders. Broader History reads remain held by
  `THREAD_READ_POLICY_DESIGN`.
- **Explicit lifecycle control needs an explicit grant**: resume, suspend, delete or rebind a
  Sandbox as a direct request, or bind a Session to an environment. A grant is a `PolicySet` rule bound to a
  subject, which may be I if someone writes that binding, never implied. Explicit environment
  rules, saying who may make these requests for which environments, are the mechanism; no such
  `PolicySet` rule type exists yet (`Rule.Type` has `kubernetes_role_binding`,
  `kubernetes_cluster_role_binding`, `egress`, `action_auto_approve`, `session_inbox_access`), an
  open item tracked by `ENVIRONMENT_LIFECYCLE_RULES`. The Identity inventory reads already admit
  Identity callers; explicit lifecycle operations remain ServiceAccount/operator-only, while
  automatic Identity wake uses the existing target resume policy and is outside the proposed rule.
  The reviewed design is [the `environment` rule type](environment_rules.md): resume followed by
  native `bind_session`. Session creation without a binding remains a separate, unresolved design.
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
the identity through its `WorkloadCredential`, so a newly accepted binding gets a new credential ID.
A suspended Firecracker launcher whose Identity changed is currently held with its checkpoint
(`RunnerCredentialMismatch`); safe checkpoint discard and cold start under the new Identity are not
implemented. An environment may therefore be created unbound and bound by the Session that attaches
it.

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
environments, is a purpose of this migration (operator, 2026-11-10). A central-sampler Session
names its `Identity` at `CreateSession`; Dispatcher creates a Session-managed Identity or checks
that the caller may use an existing one. Identity callers still cannot call `CreateSession`
directly; caller-authorized child Sessions remain under `SESSION_SUBAGENT_DELEGATION`.

The central sampler already delegates its History and service calls for the central Session as that
Identity, while authenticating as its own component ServiceAccount. The remaining Action-backed
slice is the central code-mode `tools` namespace; the sampler currently reuses the environment-bound
executor tools separately. When an environment is attached, its Identity must match the Session's,
and the Sandbox SVID resolves to `Sandbox.spec.identity`; the Sandbox contract and accepted rollout
are recorded in the [rollout evidence](../docs/sandbox_identity_cutover_2026_11_12/README.md). The
sampler's egress stays its own, and a Kubernetes account grant is inert until an environment is
attached.

## Kubernetes execution

No kubeconfig (operator, 2026-11-05): the agent learns how to reach Kubernetes as it learns any
credential. The rules endpoint lists what its identity may present, and each `EgressCredential`'s
`description` names the apiserver host, the account and the `kubectl --server <host> --token
<placeholder>` invocation; the catalog already renders that text (`kubernetes_accounts.py`). Nothing
in the sandbox holds a real token, and a new grant needs no file to change. Only the local cluster
is modelled.

## MCP edge

The edge resolves each OAuth grant to its Connection's current exact live Identity, authenticates to
Action Service as its own component ServiceAccount, and delegates the Identity through the
[delegated authorization](../docs/delegated_authorization.md) contract with typed Connection/grant
provenance. Consent or rebind is operator-approved against an eligible live Identity in the policy
namespace; the edge does not use a static name allowlist. No workload bearer is minted or forwarded.
This is separate from the retained internal ServiceAccount authentication used by backend services
calling one another.

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

The workload Identity source cutover and Sandbox rollout acceptance are complete. Preserve the live
evidence separately from source claims. Remaining work is scoped to explicit lifecycle authority,
the app's `agentplane-agent` bearer decision, and deployment-specific standing-workload proxy
observation; none reopens the completed workload-identity cutover.

**Current contract:** a Session executes as an exact Identity incarnation; workload-facing
authorization and History reads use that recorded Identity. Platform components continue to
authenticate as their own ServiceAccounts and retain actor/writer/sender provenance. A component
acting for a workload names the Identity through
[delegated authorization](../docs/delegated_authorization.md). Kubernetes access remains a separate
account capability granted and enforced through egress impersonation.

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
   `policy/api`. This remains a deferred API-design question; the workload cutover did not rewrite
   these kinds or make a group move a prerequisite.
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
    well; the check is advisory. `IDENTITY_CLEANUP_REFERENCE_DECISION` still needs to settle whether
    and where to enforce an authoritative concurrent-reference guard. A Sandbox's
    `WorkloadCredential`s carry a controller owner reference to the Sandbox (`requester.Controller`),
    so they go with it and exist only while a Sandbox names the Identity. Only owner-less
    credentials (`requester.Unowned`: standing workloads such as `haku-ui` and the `haku-ci` relay)
    count on their own. The Identity is never a credential's owner: the UID pin already revokes on
    delete, and an owner reference would not block the delete anyway. Accepted consequences: a launch failing after step 1
    leaves an Identity and its grants, which the app's unreferenced filter shows; if that becomes a
    problem, the server-side launch action rolls back what it created on failure. Also Identities left by `LaunchSandbox` or the system-test driver remain until
    deleted; a cleanup job can come later if they accumulate. Central Sessions keep naming their
    Identity at `CreateSession`; question 7 is independent of this.

## Exit

The source cutover is complete; this plan remains the owner of its identity and lifecycle decisions.
Remaining automated acceptance, explicit lifecycle-grant implementation and deferred cleanup
reference decisions are tracked separately in the DAG. Do not archive this plan on the basis of the
completed workload cutover alone.
