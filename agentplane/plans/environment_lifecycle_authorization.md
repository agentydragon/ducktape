# Explicit environment resume and message-wake authority

Draft for operator review, updated 2026-11-02. Implementation is not selected by this document.
The DAG owns `SANDBOX_LIFECYCLE_AUTHORIZATION_DESIGN` and the blocked `EXEC_SUSPENDED_RESUME` outcome.

## Recommendation to review

1. Allow the exact execution ServiceAccount to request resume under an explicit target policy.
   Use the object-shaped opt-in `resumePolicy: { allowOwnServiceAccount: true }`. The boolean
   defaults to false when the policy object or field is omitted. This grants a lifecycle operation,
   not Kubernetes access; existing operator/broker authority remains separately checked.
2. Make queued delivery request resume using the original sender's effective identity and the same
   lifecycle authorization as explicit resume. `wakeOnSend=true` enables that automatic request;
   it grants no additional resume authority. Send authorization remains independent.
3. Treat an operator-requested stop as sticky for agents and automatic wake. Only an explicitly
   authorized operator resume overrides it. Execution failures never silently request resume.
4. Keep an authorized input queued when wake is denied or disabled. Expose the delivery blocker and
   typed wake assessment through its receipt and watches, including whether the sender lacks resume
   authority. Queuing succeeds even when automatic wake cannot proceed.

These are review decisions. Alternatives include broker-only resume, a separate message-wake grant
independent of explicit-resume permission, resume without a target opt-in, and allowing agents to
override requested stops. This revision recommends shared resume authorization; the earlier draft
allowed same-account message wake independently of the explicit-resume opt-in. The operator's
object-shape direction does not by itself select these remaining policy choices.

### Target policy shape

The operator requested an object-shaped policy so additional options can be named independently:

```yaml
resumePolicy:
  allowOwnServiceAccount: true
```

"Own ServiceAccount" means the environment's exact live execution account, including namespace,
name and UID. An absent policy, an empty object, or `allowOwnServiceAccount: false` leaves this
ordinary-account resume permission disabled. Under the revised recommendation, the permission is
checked for both explicit and automatic resume. `wakeOnSend` controls whether queued delivery
requests it automatically. Neither field authorizes editing policy. Selecting the caller,
message-wake and manual-stop rules remains a separate review outcome.

## Current implementation and ownership

- [Discovery](../sandboxes/discovery/service.go) describes the exact target, execution account,
  operating state and readiness. Its current `ResumeState` reflects send-wake eligibility; it is
  not an implemented explicit-resume RPC or a generic lifecycle grant.
- [Dispatcher wake](../runner_gateway/dispatcher/README.md#contract) requires both the configured
  wake-caller allowlist and `wakeOnSend`. Same-account send permission alone currently does not wake.
  The [delivery worker](../runner_gateway/dispatcher/delivery_wake.go) decides when queued work needs
  wake; an ordinary ineligible sender currently sees only `reason: "sandbox_suspended"`. A failed
  privileged Kubernetes patch may produce `sandbox_wake_failed: ...`, but that is not a typed
  sender-authorization decision. The new policy object and richer receipt below are not implemented.
- [`WakeForSend`](../runner_gateway/dispatcher/sandbox_kubernetes.go) rereads the Sandbox UID,
  rejects quiesce, and patches desired state plus `wakeGeneration` under an optimistic lock.
  It does not currently make requested stops sticky when wake-on-send is enabled.
- The [Sandbox Controller](../sandbox_controller/SPEC.md) owns Pod/VM transitions and incarnation
  replacement. Runtime owns admission/drain/fences, not permission to consume another environment.
- Explicit suspend/resume currently use the integration app's
  [operator HTTP routes](../app/go/sandboxapi.go) and
  [inventory mutations](../app/go/sandboxes/inventory.go), which patch Sandbox desired state.
  Neither the [Session Dispatcher proto](../runner_gateway/dispatcher/session_dispatcher.proto) nor
  the [Environment Runtime proto](../env/runtime/control/environment_runtime.proto) currently has
  public suspend/resume lifecycle RPCs. Runtime `Quiesce` is one teardown step, not an environment
  suspend API. `EnvironmentLifecycle` below is proposed.
- The Gateway already has a [direct Runtime proxy](../runner_gateway/gateway/proxy.go), including
  `SubmitInput`, that bypasses the Dispatcher queue and performs no automatic wake. Its caller policy
  is separate from Dispatcher admission. This is not a selectable queue-free mode on today's
  `SessionDispatcher.SubmitInput`; the [optional-queue redesign](dispatcher_gateway_merge.md) is a
  separate, unselected proposal.

Make lifecycle authorization and the guarded desired-state change one backend responsibility,
initially a logical `EnvironmentLifecycle` service in the existing Gateway binary. Extract the
shared mutation/policy machinery for Dispatcher wake and the operator suspend/resume paths to use. This
does not choose a new Deployment or the later Session/Environment dispatch grouping.

## Layering: admission, optional wake, lifecycle and execution

1. **Session command admission** authorizes sending to the Session and, on the queued path, retains
   the command under its caller-held ID. Lack of resume permission does not remove send permission.
2. **Dispatcher delivery** determines whether the actual Session owner can accept the command. It
   requests automatic wake only if delivery needs the environment and `wakeOnSend` enables it.
   Central sampling can accept input while an attached execution environment is suspended; that
   alone must not wake the environment.
3. **Environment lifecycle** rechecks the exact target, original effective caller, current resume
   policy, manual-stop state and shared budget, then makes the guarded desired-state change. An
   in-process Dispatcher uses the same policy/mutation implementation as the public resume RPC;
   a loopback RPC or a new binary is not required.
4. **Sandbox Controller** reconciles desired state into the Pod/VM transition. Readiness and the
   resulting incarnation are observations after acceptance. Gateway routes/fences live calls and
   Runtime admits work; neither owns an extra permission to resume on execution failure.

The Dispatcher retains verified caller provenance, including the SA UID, with the command. It
rechecks current eligibility when wake is needed rather than treating an enqueue-time eligibility
boolean as a permanent grant. A delegated Actions call retains the agent's effective identity;
Dispatcher/Gateway/Actions actor privileges do not become the sender's fallback authority.

## Authority matrix

All target references include namespace, name and Sandbox UID. Normal account comparison includes
namespace, name and live SA UID; a same-name replacement is a different account.

| Caller / trigger                                   | Explicit resume                                                  | Wake caused by input                                                           |
| -------------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Exact execution SA, direct workload                | `resumePolicy.allowOwnServiceAccount=true`; idle suspension only | Independent send admission, the same resume permission, and `wakeOnSend`       |
| External MCP caller with the same verified account | Same rule through delegated authorization                        | Same effective-caller rule; Actions' identity adds no permission               |
| Different ordinary SA                              | Denied; broader lifecycle grants are later work                  | Existing send grants still apply; no new wake grant follows from sending       |
| Configured privileged broker                       | Its explicitly reviewed lifecycle authority                      | Its own resume authority plus `wakeOnSend` and the selected manual-stop policy |
| Operator through its authenticated operator API    | May resume a requested stop                                      | Its existing send/broker contract remains explicit                             |
| Scheduler or other automation                      | Uses its verified account and this same matrix                   | No wake authority is inferred from being a scheduler                           |

Use the existing [delegated authorization](../docs/delegated_authorization.md) machinery for
Action-backed calls. Actor trust, effective caller, target eligibility and trigger policy remain
separate. Add the lifecycle method to a delegator's explicit method scope; ordinary privileged
actor admission must never become the effective caller's permission. A caller cannot edit its own
`resumePolicy`, `wakeOnSend`, execution account or wake budget through the resume tool.

## Proposed API and tools

Resume needs an environment, not a Session. Support Pod and VM targets that implement it; other
environment kinds may truthfully return an unsupported capability. The initial public names below
are proposed, not existing callable operations.

| Surface                                                           | Request                                                      | Response / role                                                                                                                                     |
| ----------------------------------------------------------------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EnvironmentDiscovery.GetEnvironment` / `environments__get`       | Existing exact target; optional bounded readiness wait       | Extend the descriptor with explicit-resume eligibility, denial reason, current wake generation and an opaque resume token                           |
| `EnvironmentLifecycle.ResumeEnvironment` / `environments__resume` | `{target: TargetHandle, resume_token: string}`               | Typed `REQUESTED`, `ALREADY_RUNNING`, or `STATE_CHANGED` result, target and observed generation; denied calls return structured `PERMISSION_DENIED` |
| `agentplane environments resume`                                  | Same target/token contract                                   | Direct gRPC client with the same authorization; return acceptance separately from readiness                                                         |
| `EnvironmentLifecycle.SuspendEnvironment`                         | Exact target; suspend retry/precondition contract for review | Proposed operator-authorized companion; no agent suspend permission follows from resume permission                                                  |

The operator's suspend/resume facade becomes a client of the lifecycle owner; backend-required
state changes cannot call the app. Explicit resume is the selected first implementation. A public
suspend RPC can follow when its authority and retry contract are reviewed; it is not a prerequisite
for resume, and this draft does not invent a generic agent suspend grant.

The resume token is a precondition binding the target UID and observed wake generation, not a
capability or an execution-incarnation fence. Discovery supplies it; agents copy it rather than
inventing a generation. The backend validates it with current policy. No model-selected expected
execution incarnation is required for resume. Existing optional execution-incarnation fencing
remains a separate exec argument.

`GetEnvironment` remains read-only. A readiness wait has a caller-supplied bounded duration and
returns current observations on timeout; it never starts work. Reuse existing typed readiness,
stop-stage, incarnation and source-error facts rather than treating Pod existence as readiness.

Routine resume may use the existing `basic` Action auto-approval convention only after backend
eligibility is implemented. Auto-approval does not bypass target policy or grant environment
creation. Fresh provisioning retains its separate approval/catalogue decision.

## Queued and direct delivery from an agent's point of view

Assume the sender is authorized to use the selected send surface. The current direct proxy has a
separate, narrower caller policy; Dispatcher permission does not grant access to it, and today's
delegation Actions expose queued sending only. Failure of send authorization rejects the input
before enqueueing and creates no dispatch on its behalf. The table describes an existing native
Session whose environment must run before delivery:

| Environment / sender policy                   | Queued delivery                                                                     | Direct delivery, without the Dispatcher queue                                                  |
| --------------------------------------------- | ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Runtime reachable                             | Deliver normally; resume authority is irrelevant                                    | Return Runtime admission or a known refusal                                                    |
| Suspended; resume allowed; `wakeOnSend=true`  | Retain input, request authorized resume, report waiting for readiness, then deliver | Report unavailable; caller may explicitly resume and wait before making a new delivery attempt |
| Suspended; resume denied; `wakeOnSend=true`   | Retain input; report suspended plus wake denied for the sender                      | Report unavailable; explicit resume is denied too                                              |
| Suspended; resume allowed; `wakeOnSend=false` | Retain input; report automatic wake disabled; caller may explicitly resume          | Report unavailable; caller may explicitly resume                                               |
| Operator-requested stop                       | Retain input and expose the manual-stop blocker                                     | Report unavailable; ordinary agent resume remains denied                                       |

Queued wake denial is not a terminal command failure. Another authorized actor may later resume the
environment, after which the original queued command can deliver. The sender can cancel while it
remains queued. If input was queued, the sender must not also submit a direct copy after waking;
observe the retained command instead. A caller's explicit-resume eligibility alone does not imply
that a wake has been attempted or that the environment is currently resuming.

"Direct" means no Dispatcher delivery queue, not no durable admission or internal work queue in
the receiving runtime. A successful direct reply contains the Runtime's journal admission, not
proof that the model consumed the input or completed a turn. A known pre-admission refusal is
different from a lost response after possible admission; a transport error alone cannot establish
that the command was not admitted. Retain the original command ID and reconcile evidence before
retrying work with possible effects.

### Receipt reads and the missing wake assessment

Today `GetDispatch(command_id)`, `ListDispatches` and `WatchDispatches` expose retained Dispatcher
state and `reason`, including the Runner's admission entry once recorded. `NOT_FOUND` means no
caller-visible retained dispatch, not proof that no receiver ever saw the command. These reads do
not query the Runner for its current full command state; `HARNESS_CONFIRMED` has no writer today.
The implemented [`sessions__observe_input`](../session_client/observe_input.go) combines that
receipt with independently authorized History evidence to report input confirmation and turn
outcomes. An MCP caller can use this Action-backed observer without its own environment.

Extend the existing receipt with shared protobuf-defined delivery-blocker and wake-observation
records. Keep `DeliveryState` for where the command is; do not replace it with a wake result or
require agents to parse English errors. The initial proposed wake states distinguish not evaluated,
not needed, automatic wake disabled, denied, rate limited, requested, failed and outcome unknown.
Denial reasons distinguish sender authority from a manual stop or unsupported resume capability.
Use generated enum names, an observation timestamp and target generation where observed, a bounded
retry time for budget exhaustion, and structured status details for errors. Direct lifecycle results
and queued receipts reuse the same wake vocabulary.

For example, the following is a **proposed** receipt fragment, not today's schema:

```json
{
  "state": "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
  "delivery_blocker": { "kind": "DELIVERY_BLOCKER_ENVIRONMENT_SUSPENDED" },
  "wake": {
    "state": "WAKE_STATE_DENIED",
    "reason": "WAKE_DENIAL_CALLER_NOT_AUTHORIZED",
    "observed_at": "2026-11-02T03:00:00Z"
  }
}
```

An initial enqueue reply may precede wake assessment. The proposed wake field then says not evaluated;
`GetDispatch`/`WatchDispatches` and `sessions__observe_input` expose the later assessment even when
the command remains queued. Store and publish changes to these facts transactionally, including
updates where the delivery state does not change. The current watch's state/reason change filter
must include the new fields. A status is a timestamped assessment, not a promise that policy or
environment state cannot change. Discovery exposes current caller eligibility separately.

A direct proxy call creates no Dispatcher row, so `GetDispatch` cannot observe it today. Keep its
Runtime admission and Session/command IDs; retained History may provide later evidence, subject to
coverage and read authority. Where it cannot establish admission or outcome, return unknown. If a
future unified API offers queue-free sends with command-status lookup, it needs an explicit owning
Runtime/central-harness observation contract for those IDs; it cannot fabricate a Dispatcher queue
receipt. Selecting that API and any missing backend read is separate work, not a required queue or
database in every deployment.

## Concurrency, budgets and uncertain replies

An attempt consumes a resume token once by advancing its captured generation under the Sandbox
UID/resourceVersion guard. Concurrent requests for that generation converge on the same desired
running state. Do not turn a retry of an old token into another generation after a later suspension.
If state changed, return current facts and require a fresh explicit intent before another wake.
This keeps retry safety in Kubernetes state rather than introducing another command queue or a
resume-receipt database.

On a resourceVersion conflict, reread and reauthorize. A changed generation or requested stop
settles the old precondition; it is not permission to select a new generation in the retry loop.
Deleting/replacing the target or account fails closed. The controller still finishes any accepted
quiesce and fences the previous incarnation before starting its successor.

Reuse the current durable wake budget: ten guarded attempts per caller per rolling 60 seconds,
keyed by caller, target UID and generation. Share reservations across explicit and message wake;
the lifecycle owner controls them across replicas. Preserve charging of uncertain guarded patch
attempts and return the retry time for exhaustion. No provider-spend budget is introduced here.

If a reply is lost, read the descriptor and retry only the same token where permitted. A later
generation proves state advanced, not that this caller caused it or that execution is ready.
Neither reconnect, resume nor a new incarnation replays the original failed exec or restores old
process handles. The agent issues a new command after observing readiness.

## Concrete workflows

### Central agent recovering an idle environment

1. An exec reports suspended state and preserves any observed output/unknown prior effects.
2. `environments__get({target})` returns explicit-resume eligibility and a resume token. If denied,
   explain the returned policy reason; do not attempt Kubernetes patching.
3. `environments__resume({target, resume_token})` requests desired running state. `REQUESTED` is
   acceptance, not readiness or successful execution.
4. Read/wait with `environments__get` until ready, retaining the returned execution incarnation.
   On timeout or controller failure, report those facts while the central loop remains usable.
5. Run a newly requested command. Adopt a new incarnation explicitly where the exec contract
   requires it; no prior operation is automatically replayed.

An external MCP caller follows exactly this sequence without its own environment. The Actions
adapter authenticates as Actions and delegates the verified caller; lifecycle authorizes that
caller against the exact execution account. A native harness suspended inside the target needs an
external caller or approved automation to perform these steps.

### Input for a suspended native Session

1. The agent obtains the current tool schema and submits input once with its retained command ID.
2. Dispatcher authorizes and retains the input independently of resume permission.
3. If delivery needs a suspended environment, `wakeOnSend` selects automatic resume and lifecycle
   checks the sender's current authority. An allowed request exposes resume acceptance and waits
   for readiness; a denied one exposes the suspended delivery blocker and typed wake denial.
4. The agent follows `GetDispatch`/`WatchDispatches`, or the implemented `sessions__observe_input`,
   with the same command ID. It can distinguish admission, queued-with-wake-denied, resume requested,
   Runner admission and later History observations. It need not provoke a separate mutation to
   learn that automatic wake was denied.
5. If permitted and automatic wake was disabled, the agent may explicitly resume. It then observes
   the already-queued input; it does not submit a second copy. If not permitted, wait for an
   authorized wake, cancel while still queued, or report the blocker.

The initial APIs in steps 1, 2 and 4 exist; the explicit lifecycle call and typed wake assessment are
the additions described above. An operator-requested stop remains a structured blocker. A central
Session that needs no environment to receive input bypasses automatic wake and can accept the input
while its execution environment stays suspended.

## Implementation and acceptance after review

Implement the shared lifecycle policy/mutation owner, then its authenticated RPC, Discovery
projection, typed queued wake observations and CLI/Action surfaces. Update the operator path and
Dispatcher wake deliberately; their current allowlist/manual-stop semantics change only with the
reviewed matrix. Use new Goose
migrations if moving existing budget records requires a schema change, preserving retained state.

Focused coverage must include same-account and delegated success, foreign/recreated accounts,
target replacement, operator stop racing resume, concurrent tokens, budget exhaustion, lost replies,
quiesce/readiness failure and restart reconciliation. Exercise an actual suspended target followed
by a successful new exec. Cover all queued/direct cases above, sender-visible wake denial through
read/watch/MCP observation, an initial unassessed receipt followed by an assessment, policy changes
before wake, and receipt updates without delivery-state changes. Direct transport ambiguity must not
be reported as known non-admission. Existing no-replay and process-domain fences remain binding;
broad disruption exercises are still deferred.

Review outcome needed: select shared versus independent resume/message-wake authority, target
opt-in and sticky-stop rules, and review the receipt contract above. Record any rejected alternative
before implementation; this draft authorizes no rollout or queue-free API redesign.
