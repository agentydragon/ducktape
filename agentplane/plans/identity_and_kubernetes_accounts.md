# Identities, workload authentication and Kubernetes accounts

Directions selected by the operator on 2026-11-05:
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
cutover left open.

## Target: one identity, three credential forms

Deployed (operator decision, 2026-11-10; [rollout record](../docs/sandbox_identity_cutover_2026_11_12/README.md)).
The Agentplane `Identity` is the one identity source; each credential form is an explicit, named
mapping to one `Identity`: an X.509 SVID over mTLS for in-cluster workloads
([workload authentication](../docs/workload_authentication.md)), an OAuth2 access token from the MCP
edge's own authorization server (`mcp_edge/internal/authserver`, dynamic client registration) for
external MCP clients ([MCP edge](../mcp_edge/README.md#authority)), and Kubernetes' own authentication
only toward the Kubernetes API ([agent RBAC](../../cluster/docs/agent_rbac.md#kubernetes-accounts)).

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

**Acting as an Identity, and what it implies** (operator, 2026-11-10). Acting in a workload is
implied for its bound Identity, and explicit lifecycle control needs an explicit grant:
[environment rules](../docs/adr_environment_rules.md).

- **An Identity is the isolation boundary.** One Session has one Identity, and Sessions sharing an
  Identity have the same access by design; to isolate, use different Identities. An Identity is not
  assumed to be one-to-one with a Sandbox: several may be bound to it.
- **Messaging between Sessions of the same Identity is allowed** without a grant; a send may wake
  the recipient under its existing resume policy. Cross-Identity sends still require
  `session_inbox_access`, whose current targets are Sandboxes and their tags. The inbox owner and
  History workload-read paths now use the recorded Session Identity, but the rule's target/group
  model and grant lifetime remain unresolved in
  [`SESSION_ACCESS_GROUPING_DESIGN`](task_dag.md#session_access_grouping_design--access-groups-across-independent-sessions-and-environments).
  Keep that enforcement and preserve component senders. Broader History reads remain held by
  `THREAD_READ_POLICY_DESIGN`.
- **Action boxes:** creating one is lifecycle. Today it is granted explicitly, through the Action
  Service's `action_auto_approve` policy on the effective caller. **Open consideration** (operator,
  not decided): an agent creating its own boxes with automatic approval is a deliberate path, so MCP
  clients can run bash and use egress credentials; if needed it can stay a privileged component
  (the MCP edge, acting for the Identity through delegation, under policy) rather than the
  Identity's own SVID being allowed to create boxes directly.

## Changing an environment's identity

Deployed: [Environments SPEC § Identity](../environments/SPEC.md#identity) (admission, credentials),
[EnvironmentService SPEC](../environments/internal/service/SPEC.md#caller-and-object-authorization)
(rebind) and [Dispatcher SPEC](../session_dispatcher/dispatcher/SPEC.md#session-binding) (retained Sessions).

## Central Sessions with no environment

Decoupling a central Session from any Sandbox, so it exists with none and attaches and detaches
environments, is a purpose of this migration (operator, 2026-11-10). A central-sampler Session
names its `Identity` at `CreateSession`; Dispatcher creates a Session-managed Identity or checks
that the caller may use an existing one. Identity callers still cannot call `CreateSession`
directly; caller-authorized child Sessions remain under `SESSION_SUBAGENT_DELEGATION`.

The central sampler already delegates its History and service calls, including the code-mode Action
`tools` namespace, for the central Session as that Identity, authenticating with its own service
SVID ([sampler SPEC](../sampler/SPEC.md#action-tools)); executor tools stay environment-bound. When
an environment is attached, its Identity must match the Session's, and the Sandbox SVID resolves to
`Sandbox.spec.identity`; the Sandbox contract and accepted rollout are recorded in the
[rollout evidence](../docs/sandbox_identity_cutover_2026_11_12/README.md). The sampler's egress
stays its own, and a Kubernetes account grant is inert until an environment is attached.

## MCP edge

Deployed: [MCP edge § Authority](../mcp_edge/README.md#authority) and
[Action Service SPEC § Caller admission and Connection authority](../action_service/SPEC.md#caller-admission-and-connection-authority).

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

Deployed. A Session executes as its recorded Identity incarnation
([Dispatcher SPEC](../session_dispatcher/dispatcher/SPEC.md#session-binding)); a component acting for a
workload names the Identity through [delegated authorization](../docs/delegated_authorization.md);
Kubernetes access is a separate account capability granted through egress impersonation
([agent RBAC](../../cluster/docs/agent_rbac.md#kubernetes-accounts)).

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

This plan owns the identity and lifecycle decisions until they graduate to maintained docs.
Remaining automated acceptance and the cleanup reference decision are tracked in the DAG.
