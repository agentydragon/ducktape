# Agent teardown

**Design, no direction chosen.** DAG node:
[`AGENT_TEARDOWN_DESIGN`](task_dag.md#agent_teardown_design--a-teardown-sequence-for-an-agent). How an
agent's Sessions, Sandbox and Identity end, in what order, which references block which step, and who
runs the sequence. Absorbs the former `IDENTITY_CLEANUP_REFERENCE_DECISION`. Settled inputs stay where
they are: [Identity lifetime](identity_and_kubernetes_accounts.md#identity-lifetime) (the creator decides
it; deleting an environment never deletes its Identity; deleting an Identity leaves an environment
naming it unable to start) and
[explicit Session deletion](session_environment_model.md#environment-deletion-and-explicit-session-deletion)
(not implemented).

Out of scope: deleting a Sandbox whose Runtime cannot recover
([`WEDGED_SESSION_AND_SANDBOX_RECOVERY`](task_dag.md#wedged_session_and_sandbox_recovery--archive-a-session-and-delete-a-sandbox-when-the-runtime-cannot-recover)).
That is step 2 failing; this plan is the order around it.

## Symptoms

- **2026-11-23, `public-coder-ducktape-3`:** the Sandbox delete dialog listed three archived Sessions
  as blocking Identity cleanup ([`SESSION_ARCHIVE_CONTRACT`](task_dag.md#session_archive_contract--what-archiving-a-session-means)).
- **2026-11-26, `public-coder-1`:** the operator saw the app offer an Identity delete the Policy
  Controller refused. Not reproduced: at inspection the Sandbox was Suspended, no WorkloadCredential
  named the Identity and the controller would have accepted. The likeliest cause is the
  WorkloadCredential gap below, hit while the Sandbox ran. The same Identity's Sandbox dialog
  blocked cleanup on Sessions `218cd484…` and `5ca0ba48…`, which the Threads tab no longer lists
  (probably archived; not verified).

## What an agent leaves

| Object                                                      | Names the Identity by                                                                                    | Removed by                                           | Removal gate today                                                                                                                          |
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
`orchestration_v1` launch origins, which also name Identities.

## Three rule sets

| Check              | Policy Controller | Identity page, list row | Sandbox dialog "delete identity too"  |
| ------------------ | ----------------- | ----------------------- | ------------------------------------- |
| Flux label         | refuses           | blocks                  | blocks                                |
| Unowned binding    | refuses           | blocks                  | blocks                                |
| WorkloadCredential | refuses           | **cannot see**          | **cannot see**                        |
| Sandbox naming it  | —                 | —                       | blocks (except the one being deleted) |
| Session naming it  | —                 | —                       | blocks, by **name**, any state        |

The browser cannot read WorkloadCredentials, so it offers deletes the controller refuses whenever a
workload runs. The dialog's Session check matches by name although Sessions pin a UID, so Sessions of
an earlier same-name incarnation also block (from [`references.ts`](../app/frontend/identities/references.ts);
not observed). None of the checks is atomic with creating a reference.

## Behavior on deletion today

- **Running Sandbox:** its WorkloadCredential makes `DeleteIdentity` refuse. Were the Identity gone,
  the controller never stops a running workload for it ([`identityGate`](../environments/internal/controller/identity.go));
  the credential resolver would refuse the UID-pinned subject.
- **Suspended Sandbox:** no WorkloadCredential, so the delete is accepted. The Sandbox stays, cannot
  start, and can be rebound to another Identity.
- **Sessions:** pinned to the UID, so dispatch to a Session of a deleted Identity fails
  (`sandbox execution identity changed`). A same-name successor does not revive it. Retained reads
  use the recorded UID.
- **Terminating Identity** (`deletionTimestamp` set; never happens today, nothing holds a finalizer):
  the WorkloadCredential resolver ([`resolver.go`](../identity/credentials/resolver/resolver.go)), the
  delegation UID lookup ([`delegation.go`](../identity/auth/delegation.go)), Sandbox admission
  (`requireIdentity`) and `GrantIdentityPolicyBinding` already treat it as gone. The
  [`policy/reader`](../policy/reader/index.go) index behind egress, the Action Service and the MCP edge
  still treats it as live (`PutIdentity` on every update).

## Invariant: every Sandbox attached to a Session carries the Session's Identity

Operator, 2026-11-26: a Session and every Sandbox attached to it have the same Identity, wherever the
harness runs: native Sessions (harness in the Sandbox) and central sampler Sessions (harness in the
sampler, the Sandbox as executor) alike. With one Sandbox the Session acts as that Sandbox's Identity,
or none when it has none. With several, all carry the Session's Identity. With none, nothing
constrains it.

Today every Session has exactly one Sandbox; central V1 requires its executor Sandbox. `CreateSession`
records "the exact Identity its Sandbox names now" for both placements, and dispatch refuses when they
later differ (`sandbox execution identity changed`, [`api.go`](../session_dispatcher/dispatcher/api.go)).
Sessions with no Sandbox or several are a future shape
([Sandbox-free central Sessions](task_dag.md#sandbox-free-central-sessions)); there, attaching a
Sandbox requires it to carry the Session's Identity.

Consequence: changing a Sandbox's Identity changes the Identity of every Session attached to it, in
the same operation, and with it the required Identity of every other Sandbox those Sessions have
attached. A rebind therefore moves the whole connected set of Sessions and Sandboxes, or detaches the
Sandbox first. Today the set is one Sandbox and its Sessions. Today it does not. `SetEnvironmentIdentity` patches only the Sandbox (stopped workloads
only, [`SetIdentity`](../environments/internal/service/management/access.go)); the app exposes it as
the clearable "Identity for next start" ([`identity.tsx`](../app/frontend/sandboxes/identity.tsx)).
Sessions keep their pinned UID, so after a rebind they fail dispatch, and #15445 refuses the
rebind while a retained Session names another incarnation. Three candidate shapes; **how any works
is not designed**:

- **Derive:** a Session with Sandboxes stores no Identity; each dispatch uses its Sandboxes' common
  Identity, and History records the Identity per turn. Past turns keep the Identity they ran as. Breaks
  down with several Sandboxes: rebinding one makes them disagree.
- **Session owns it:** the Session holds the Identity; its Sandboxes follow. Rebinding is an operation
  on the Session that moves every attached Sandbox, all suspended.
- **Move together:** rebind or unbind of a suspended Sandbox rewrites its Sessions' pinned
  Identity in the same Dispatcher-coordinated operation.

Open for each: commands Dispatcher accepted under the old Identity but has not delivered; native harness
state inside the Sandbox (Claude Code, Codex) and central sampler continuation state from runs as the
old Identity; inbox and subscription
ownership; delegation issued to the old Identity and still in flight; the Sandbox and Dispatcher
writes not being one transaction; and what a Session with no Identity shows and accepts.

## Target sequence

"Finish with this agent" (the settled operator operation) for an app-created Identity. Each reference
is released one of two ways: **retire** the object, or **release** the Identity from it and keep the
object.

1. **Sessions.** Follow their Sandbox (step 2), or finish (no new turns; History and inbox stay
   readable; `SESSION_ARCHIVE_CONTRACT`).
2. **Sandbox.** Suspend it (revokes its WorkloadCredential), then either delete it or unbind / rebind
   it while suspended, carrying its Sessions per the invariant.
3. **Delete the Identity.** Owned bindings go by GC. Refused while a live reference remains.

Steps are independent operations; each is idempotent and resumable from any partial state. A
standing (Flux) Identity skips 3. A Session-managed Identity (`SESSION_IDENTITY_PROVISIONING_DESIGN`)
runs 1 and 3 only.

Example: keep a paused Sandbox, its workspace and its Thread history, but retire the agent's grants:
unbind the suspended Sandbox (its Sessions now have no Identity and cannot run), then delete the
Identity. Rebinding later gives the Sandbox and its Sessions the new Identity's grants.

### Live references (draft rule)

Blocks step 3: an unowned binding naming it; a WorkloadCredential acting as it; a Sandbox naming it,
other than one being deleted. Sessions count through their Sandbox; once Sessions with no Sandbox or
several exist, an unfinished one pinned to its UID counts directly.

Does not block: a finished Session; History; owned bindings; a Session pinned to another incarnation.

## Enforcing step 3

Every option must cover: already referenced, a reference created concurrently with the delete, a
reference removed, same-name replacement, failure and recovery. A UID/resourceVersion precondition on
the Identity alone does not serialize references held in Kubernetes and Dispatcher.

**B. Refuse at request, from one published rule.** The controller computes the refusal once:
`DeleteIdentity` returns it as `PreconditionFailure`, and `WatchIdentities` carries it on each
Identity, re-evaluated on binding and WorkloadCredential changes. Every UI shows the controller's
reasons. Fixes the visible bug on today's store; needs `watch` on WorkloadCredentials for the
controller (RBAC unverified). The concurrent-reference race stays advisory, as the
[operator UI plan](identity_and_kubernetes_accounts.md#operator-ui) accepts.

**A. CR plus an in-use finalizer** (the PVC-protection pattern). Delete marks intent: the Identity
turns Terminating, new references are refused, and the controller removes the finalizer once no live
reference remains. Closes the race without a cross-store transaction, and makes teardown "request,
then watch it drain". Costs:

- A delete cannot be cancelled: a Terminating Identity never returns, and a Sandbox naming it can
  never restart.
- Needs: `policy/reader` treating Terminating as gone; Dispatcher `CreateSession` refusing it;
  the controller reading Session references from Dispatcher.
- Progress shown from the published refusal of B, which A extends.

**C. Agentplane-owned record** ([`KUBERNETES_DECOUPLING_DESIGN`](task_dag.md#kubernetes_decoupling_design--how-far-to-decouple-from-kubernetes),
[O4](kubernetes_decoupling.md#o4-move-the-store)). Session claims could commit with Session rows if
they share a database. Sandboxes and WorkloadCredentials stay CRs, so references still span stores.
Loses owner-reference GC of bindings, Flux authoring (needs a reconciler), CEL admission and the
`policy/reader` informers. Teardown alone does not pay for it.

Operator, 2026-11-26: Identity is "a pretty clear case" for moving. The deciding argument is the
invariants above: they span Sessions, which must stay in a database, so only a database-held Identity
puts them in one transaction. Portability away from Kubernetes is not the reason.

Operator lean, 2026-11-26, low confidence on the details: the authoritative record in Agentplane's database, with a
controller reconciling it against an optional Flux-managed Identity CR (git-declared Identities stay
read-only in the app; deleting the CR requests deletion under the same reference rule). With the
Session-Sandbox invariant this buys more than teardown: reference checks and new references in one
transaction, a cancellable "deleting" state, and a group rebind as one write. That holds only if
Sandbox-to-Identity bindings (`spec.identity`) also move into the database and the Sandbox Controller
reads them there; otherwise a group rebind is a database write plus CR patches. Also open: which
service owns the database (PolicyService has none; Dispatcher's would co-locate Session claims) and
the watch API replacing the `policy/reader` informers. The Identity trace in
`KUBERNETES_DECOUPLING_DESIGN` should answer the Sandbox-binding question first.

**Recommendation:** B now in any case, since it fixes the visible bug on either store. Choose A or C
as the target after the Identity trace; C is favored if the trace shows Sandbox bindings can move. The
draining contract (deleting, no new references, gone once unreferenced) is the same in both; only C
can make it cancellable.

## Who runs the sequence

1. **Separate operations, presented as a checklist.** Today's shape minus the browser-side checks:
   each step shows the controller's reasons and its own progress.
2. **A backend "finish agent" operation.** Resumable, idempotent, survives the browser closing. The
   app may not own it ([dependency rule](../docs/service_boundaries.md)). Owner candidates: Dispatcher
   (owns Sessions, already reads Sandboxes and Identities) or the Policy Controller (owns Identity).
3. **Delete-driven.** Deleting the Identity (under A) drives the rest: Sessions pinned to it finish,
   Sandboxes naming it are suspended and deleted. Contradicts "deleting an Identity leaves an
   environment naming it unable to start" and lets one delete destroy a Sandbox's state; listed for
   completeness.

## Operator decisions

1. **Live-reference rule:** confirm or amend the draft above. In particular, does a Sandbox naming
   the Identity block, or is "cannot start" (today's accepted consequence) enough? Unbinding makes
   blocking cheap to clear.
2. **Where the Session-Sandbox Identity lives:** derived from the Sandbox, owned by the Session, or
   pinned on both and moved together; and whether a rebind may detach a Sandbox instead of moving the
   whole set.
3. **Session "finished":** does finishing release the Identity, can it be undone, and is it the same
   as app archive (shared with `SESSION_ARCHIVE_CONTRACT`)? Which Identity a Session with no Sandbox
   acts as waits for that shape.
4. **Enforcement:** B now and A later, B only, or wait for C.
5. **Who runs it:** checklist, backend operation (and which owner), or delete-driven.

## Follow-ups once decided

- B: published refusal on `WatchIdentities`; the app drops its own checks. Independent of the
  decisions above except 1.
- A: finalizer, `policy/reader` and Dispatcher admission changes, Session-reference source.
- Retire the app's name-matched Session check once Sessions carry a finished state.
- Trace Connections and launch origins as references.
