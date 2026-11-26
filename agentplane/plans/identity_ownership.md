# Identity ownership

**Design, no direction chosen.** DAG node:
[`IDENTITY_OWNERSHIP_DESIGN`](task_dag.md#identity_ownership_design--who-owns-an-identity-and-what-binds-to-it).
Four linked questions:

1. how Sessions and Sandboxes bind to an Identity, and how a rebind moves it;
2. where the authoritative Identity record lives;
3. when an Identity may be deleted, and who enforces it;
4. the order in which an agent's Sessions, Sandbox and Identity end.

Absorbs the former `IDENTITY_CLEANUP_REFERENCE_DECISION`. Settled inputs stay where they are:
[Identity lifetime](identity_and_kubernetes_accounts.md#identity-lifetime) (the creator decides it;
deleting an environment never deletes its Identity; deleting an Identity leaves an environment naming
it unable to start) and
[explicit Session deletion](session_environment_model.md#environment-deletion-and-explicit-session-deletion)
(not implemented). The wider question of which records leave Kubernetes is
[`KUBERNETES_DECOUPLING_DESIGN`](task_dag.md#kubernetes_decoupling_design--how-far-to-decouple-from-kubernetes)
and [architecture overview D4](architecture_overview.md#four-directions); this plan is its Identity
case.

Out of scope: deleting a Sandbox whose Runtime cannot recover
([`WEDGED_SESSION_AND_SANDBOX_RECOVERY`](task_dag.md#wedged_session_and_sandbox_recovery--archive-a-session-and-delete-a-sandbox-when-the-runtime-cannot-recover)).

## Why now

- **2026-11-23, `public-coder-ducktape-3`:** the Sandbox delete dialog listed three archived Sessions
  as blocking Identity cleanup ([`SESSION_ARCHIVE_CONTRACT`](task_dag.md#session_archive_contract--what-archiving-a-session-means)).
- **2026-11-26, `public-coder-1`:** the app offered an Identity delete the Policy Controller refused.
  Not reproduced: at inspection the Sandbox was Suspended, no WorkloadCredential named the Identity
  and the controller would have accepted. Likeliest cause: the WorkloadCredential gap below, hit while
  the Sandbox ran. Its Sandbox dialog blocked cleanup on Sessions `218cd484…` and `5ca0ba48…`, which
  the Threads tab no longer lists (probably archived; not verified).

## Today

### What names an Identity

| Object                                                      | Names the Identity by                                                                                    | Removed by                                           | Removal gate                                                                                                                                |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `Identity` CR, policy namespace                             | —                                                                                                        | `DeleteIdentity`                                     | Flux label; unowned binding naming it; WorkloadCredential acting as it ([`identities.go`](../policy_controller/internal/rpc/identities.go)) |
| `PolicyBinding` it owns                                     | name; owner reference by UID                                                                             | Kubernetes GC with the Identity                      | —                                                                                                                                           |
| Unowned `PolicyBinding`                                     | name only: follows a same-name successor                                                                 | its author (git, operator)                           | —                                                                                                                                           |
| `Sandbox`                                                   | `spec.identity.name`                                                                                     | `DeleteEnvironment`                                  | must be Suspended ([`beginTeardown`](../environments/internal/service/management/access.go))                                                |
| Sandbox `WorkloadCredential`                                | name + UID                                                                                               | Sandbox stop; GC with the Sandbox (controller owner) | —                                                                                                                                           |
| Standing `WorkloadCredential` (`haku-ui`, `haku-ci-egress`) | name only                                                                                                | its author                                           | —                                                                                                                                           |
| Dispatcher Session                                          | name + UID ([`Session.identity`](../session_dispatcher/dispatcher/api/session/session_dispatcher.proto)) | nothing: explicit deletion not implemented           | —                                                                                                                                           |
| App archive flag                                            | via the Session                                                                                          | app                                                  | refuses unknown activity                                                                                                                    |
| Inbox, subscriptions                                        | via the Session                                                                                          | nothing until Session deletion                       | —                                                                                                                                           |
| History                                                     | via the Session                                                                                          | retained by design                                   | —                                                                                                                                           |

Not yet traced: Connections (the MCP edge's enrolled principals, e.g. `claude-ai`) and
`orchestration_v1` launch origins.

### Three delete checks that disagree

| Check              | Policy Controller | Identity page, list row | Sandbox dialog "delete identity too"  |
| ------------------ | ----------------- | ----------------------- | ------------------------------------- |
| Flux label         | refuses           | blocks                  | blocks                                |
| Unowned binding    | refuses           | blocks                  | blocks                                |
| WorkloadCredential | refuses           | **cannot see**          | **cannot see**                        |
| Sandbox naming it  | —                 | —                       | blocks (except the one being deleted) |
| Session naming it  | —                 | —                       | blocks, by **name**, any state        |

The browser cannot read WorkloadCredentials, so it offers deletes the controller refuses whenever a
workload runs. The dialog matches Sessions by name although Sessions pin a UID, so Sessions of an
earlier same-name incarnation also block (from [`references.ts`](../app/frontend/identities/references.ts);
not observed). None of the checks is atomic with creating a reference.

### What deleting does

- **Running Sandbox:** its WorkloadCredential makes `DeleteIdentity` refuse. Were the Identity gone,
  the controller never stops a running workload for it ([`identityGate`](../environments/internal/controller/identity.go));
  the credential resolver would refuse the UID-pinned subject.
- **Suspended Sandbox:** no WorkloadCredential, so the delete is accepted. The Sandbox stays, cannot
  start, and can be rebound.
- **Sessions:** pinned to the UID, so dispatch fails (`sandbox execution identity changed`). A
  same-name successor does not revive them. Retained reads use the recorded UID.
- **Terminating Identity** (`deletionTimestamp` set; never happens today, nothing holds a finalizer):
  the WorkloadCredential resolver ([`resolver.go`](../identity/credentials/resolver/resolver.go)), the
  delegation UID lookup ([`delegation.go`](../identity/auth/delegation.go)), Sandbox admission
  (`requireIdentity`) and `GrantIdentityPolicyBinding` treat it as gone. The
  [`policy/reader`](../policy/reader/index.go) index behind egress, the Action Service and the MCP edge
  treats it as live (`PutIdentity` on every update).

## 1. Binding: a Session and its Sandboxes share one Identity

Operator, 2026-11-26: a Session and every Sandbox attached to it have the same Identity, for native
Sessions (harness in the Sandbox) and central sampler Sessions (harness in the sampler, the Sandbox as
executor) alike. With one Sandbox the Session acts as that Sandbox's Identity, or none when it has
none. With several, all carry the Session's Identity, so attaching a Sandbox requires it. With none,
nothing constrains it.

Today every Session has exactly one Sandbox; central V1 requires its executor Sandbox. `CreateSession`
records "the exact Identity its Sandbox names now" for both placements, and dispatch refuses when they
later differ ([`api.go`](../session_dispatcher/dispatcher/api.go)). Sessions with no Sandbox or several
are a future shape ([Sandbox-free central Sessions](task_dag.md#sandbox-free-central-sessions)).

### Rebind and unbind

Changing a Sandbox's Identity changes the Identity of every Session attached to it, and with it the
required Identity of every other Sandbox those Sessions have. A rebind therefore moves the whole
connected set of Sessions and Sandboxes, or detaches the Sandbox first. Today the set is one Sandbox
and its Sessions.

Today a rebind moves only the Sandbox. `SetEnvironmentIdentity` patches `spec.identity` on a stopped
Sandbox, or clears it ([`SetIdentity`](../environments/internal/service/management/access.go)); the
app exposes it as the clearable "Identity for next start"
([`identity.tsx`](../app/frontend/sandboxes/identity.tsx)). Sessions keep their pinned UID, so after a
rebind they fail dispatch, and #15445 refuses the rebind while a retained Session names another
incarnation.

The Identity itself is independent of both (operator, 2026-11-26): it can exist before any Session
names it, and many Sessions can name the same one; no Session or Sandbox owns its lifetime, as
[settled](session_environment_model.md#session-scoped-identities-and-native-creation-order). The
shapes below differ only in which record holds the authoritative Session-to-Identity reference.

Candidate shapes; **how any works is not designed**:

- **Derive:** a Session with Sandboxes stores no Identity; each dispatch uses its Sandboxes' common
  Identity, and History records the Identity per turn. Breaks down with several Sandboxes: rebinding
  one makes them disagree.
- **Session holds the reference:** the Session records which Identity it acts as; its Sandboxes must
  match. Rebinding is an operation on the Session that moves every attached Sandbox, all suspended.
  The Identity is still neither created nor deleted by it.
- **Move together:** rebind or unbind of a suspended Sandbox rewrites its Sessions' pinned Identity in
  the same Dispatcher-coordinated operation.

Open for each: commands Dispatcher accepted under the old Identity but has not delivered; native
harness state in the Sandbox (Claude Code, Codex) and central sampler continuation state from runs as
the old Identity; inbox and subscription ownership; delegation issued to the old Identity and still in
flight; the Sandbox and Dispatcher writes not being one transaction; what a Session with no Identity
shows and accepts. Past turns keep the Identity they ran as.

## 2. Where the Identity record lives

The binding rule (section 1) and the delete rule (section 3) are invariants across Identities,
Sandboxes and Sessions. Sessions must stay in a database (volume; agent traces stay out of etcd).
Kubernetes has no multi-object transactions, so the only way to check those invariants in one
transaction is to hold the Identity, and its Sandbox bindings, in the database too.

Operator, 2026-11-26: Identity is "a pretty clear case" for an Agentplane-owned record, for that
reason; portability away from Kubernetes is not the reason. Lean, low confidence on the details: the
authoritative record in Agentplane's database, with a controller reconciling it against an optional
Flux-managed Identity CR. Git-declared Identities stay read-only in the app; deleting the CR requests
deletion under the same reference rule.

| Store                    | Delete semantics                                                                      | Costs                                                                                                                                                                                                                               |
| ------------------------ | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CR plus in-use finalizer | Terminating, no new references, gone once unreferenced; **cannot be cancelled**       | `policy/reader` must treat Terminating as gone; Dispatcher `CreateSession` must refuse it; the controller must read Session references from Dispatcher. The cross-store check stays non-transactional                               |
| Agentplane-owned record  | Same draining contract, **cancellable**; reference checks transactional with Sessions | A watch API replacing the `policy/reader` informers; a Flux reconciler; owned bindings lose Kubernetes GC unless they move too; no `kubectl`. A database owner: PolicyService has none; Dispatcher's would co-locate Session claims |

The Agentplane record pays off fully only if Sandbox-to-Identity bindings (`spec.identity`) move into
the database and the Sandbox Controller reads them there; otherwise a group rebind is a database write
plus CR patches. The Identity trace in `KUBERNETES_DECOUPLING_DESIGN` answers that first.

## 3. Deleting an Identity

### Live references (draft rule)

Blocks delete: an unowned binding naming it; a WorkloadCredential acting as it; a Sandbox naming it,
other than one being deleted. Sessions count through their Sandbox; once Sessions with no Sandbox or
several exist, an unfinished one pinned to the UID counts directly.

Does not block: a finished Session; History; owned bindings; a Session pinned to another incarnation.

Every enforcement must cover: already referenced, a reference created concurrently with the delete, a
reference removed, same-name replacement, failure and recovery. A UID/resourceVersion precondition on
the Identity alone does not serialize references held in Kubernetes and Dispatcher.

### Published refusal, now

The controller computes the refusal once: `DeleteIdentity` returns it as `PreconditionFailure`, and
`WatchIdentities` carries it on each Identity, re-evaluated on binding and WorkloadCredential changes.
Every UI shows the controller's reasons and drops its own checks. Fixes the visible bug on today's
store and carries over to either store above. Needs `watch` on WorkloadCredentials for the controller
(RBAC unverified). The concurrent-reference race stays advisory until the draining contract of
section 2 lands, as the [operator UI plan](identity_and_kubernetes_accounts.md#operator-ui) accepts.

## 4. Agent teardown

"Finish with this agent" (the settled operator operation) for an app-created Identity. Each reference
is released one of two ways: **retire** the object, or **release** the Identity from it and keep the
object.

1. **Sessions.** Follow their Sandbox (step 2), or finish: no new turns; History and inbox stay
   readable (`SESSION_ARCHIVE_CONTRACT`).
2. **Sandbox.** Suspend it (revokes its WorkloadCredential), then delete it, or unbind / rebind it
   while suspended, carrying its Sessions per section 1.
3. **Identity.** Delete; owned bindings go with it. Refused while a live reference remains.

Steps are independent operations, each idempotent and resumable from any partial state. A standing
(Flux) Identity skips 3. A Session-managed Identity (`SESSION_IDENTITY_PROVISIONING_DESIGN`) runs 1 and
3 only.

Example: keep a paused Sandbox, its workspace and its Thread history, but retire the agent's grants:
unbind the suspended Sandbox (its Sessions now have no Identity and cannot run), then delete the
Identity. Rebinding later gives the Sandbox and its Sessions the new Identity's grants.

Who runs it:

- **Separate operations, presented as a checklist:** each step shows the controller's reasons and its
  own progress.
- **A backend "finish agent" operation:** resumable, survives the browser closing. The app may not own
  it ([dependency rule](../docs/service_boundaries.md)). Owner candidates: Dispatcher (owns Sessions,
  reads Sandboxes and Identities) or the Policy Controller (owns Identity).
- **Delete-driven:** deleting the Identity finishes its Sessions and deletes its Sandboxes.
  Contradicts the settled "deleting an Identity leaves an environment naming it unable to start" and
  lets one delete destroy a Sandbox's state; listed for completeness.

## Operator decisions

1. **Binding shape:** derive from the Sandbox, Session holds the reference, or pinned on both and moved together;
   and whether a rebind may detach a Sandbox instead of moving the whole set.
2. **Store:** CR plus finalizer, or Agentplane-owned record; decide after the Identity trace.
3. **Live-reference rule:** confirm or amend. Does a Sandbox naming the Identity block, or is "cannot
   start" enough? Unbinding makes blocking cheap to clear.
4. **Session "finished":** does it release the Identity, can it be undone, is it app archive (shared
   with `SESSION_ARCHIVE_CONTRACT`)?
5. **Who runs teardown:** checklist, backend operation (which owner), or delete-driven.

## Follow-ups

- Published refusal (section 3): independent of decisions 1, 2, 4 and 5.
- Identity trace under `KUBERNETES_DECOUPLING_DESIGN`, starting with whether Sandbox bindings can move.
- Retire the app's name-matched Session check once Sessions carry a finished state.
- Trace Connections and launch origins as references.
