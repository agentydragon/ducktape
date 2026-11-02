# Explicit environment resume and message-wake authority

Proposal for operator review, 2026-11-01. Implementation is not selected by this document.
The DAG owns `SANDBOX_LIFECYCLE_AUTHORIZATION_DESIGN` and the blocked `EXEC_SUSPENDED_RESUME` outcome.

## Recommendation to review

1. Allow the exact execution ServiceAccount to request resume under an explicit target policy.
   Use the object-shaped opt-in `resumePolicy: { allowOwnServiceAccount: true }`. The boolean
   defaults to false when the policy object or field is omitted. This grants a lifecycle operation,
   not Kubernetes access; existing operator/broker authority remains separately checked.
2. Allow an otherwise authorized same-account input to wake an idle-suspended environment when
   `wakeOnSend=true`. Retain separately configured broker wake authority; remove no send check.
3. Treat an operator-requested stop as sticky for agents and automatic wake. Only an explicitly
   authorized operator resume overrides it. Execution failures never silently request resume.

These are review decisions. Alternatives are existing broker-only resume, same-account resume
without a target opt-in, and allowing agents to override requested stops. The recommended defaults
keep resource consumption under target-owner control while making idle suspension recoverable.

### Target policy shape

The operator requested an object-shaped policy so additional options can be named independently:

```yaml
resumePolicy:
  allowOwnServiceAccount: true
```

"Own ServiceAccount" means the environment's exact live execution account, including namespace,
name and UID. An absent policy, an empty object, or `allowOwnServiceAccount: false` leaves this
ordinary-account resume permission disabled. The field does not control message wake or authorize
editing the policy itself. Selecting the caller, message-wake and manual-stop rules remains a
separate review outcome.

## Current implementation and ownership

- [Discovery](../sandboxes/discovery/service.go) describes the exact target, execution account,
  operating state and readiness. Its current `ResumeState` reflects send-wake eligibility; it is
  not an implemented explicit-resume RPC or a generic lifecycle grant.
- [Dispatcher wake](../runner_gateway/dispatcher/README.md#contract) requires both the configured
  wake-caller allowlist and `wakeOnSend`. Same-account send permission alone currently does not wake.
- [`WakeForSend`](../runner_gateway/dispatcher/sandbox_kubernetes.go) rereads the Sandbox UID,
  rejects quiesce, and patches desired state plus `wakeGeneration` under an optimistic lock.
  It does not currently make requested stops sticky when wake-on-send is enabled.
- The [Sandbox Controller](../sandbox_controller/SPEC.md) owns Pod/VM transitions and incarnation
  replacement. Runtime owns admission/drain/fences, not permission to consume another environment.

Make lifecycle authorization and the guarded desired-state change one backend responsibility,
initially a logical `EnvironmentLifecycle` service in the existing Gateway binary. Extract the
shared mutation/policy machinery for Dispatcher wake and the operator resume path to use. This
does not choose a new Deployment or the later Session/Environment dispatch grouping.

## Authority matrix

All target references include namespace, name and Sandbox UID. Normal account comparison includes
namespace, name and live SA UID; a same-name replacement is a different account.

| Caller / trigger                                   | Explicit resume                                                  | Wake caused by input                                                                   |
| -------------------------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Exact execution SA, direct workload                | `resumePolicy.allowOwnServiceAccount=true`; idle suspension only | Existing send admission plus `wakeOnSend`; idle suspension only                        |
| External MCP caller with the same verified account | Same rule through delegated authorization                        | Same effective-caller rule; Actions' identity adds no permission                       |
| Different ordinary SA                              | Denied; broader lifecycle grants are later work                  | Existing send grants still apply; no new wake grant follows from sending               |
| Configured privileged broker                       | Its explicitly reviewed broker policy                            | Preserve explicit broker wake eligibility, with the selected target/manual-stop policy |
| Operator through its authenticated operator API    | May resume a requested stop                                      | Its existing send/broker contract remains explicit                                     |
| Scheduler or other automation                      | Uses its verified account and this same matrix                   | No wake authority is inferred from being a scheduler                                   |

Use the existing [delegated authorization](../docs/delegated_authorization.md) machinery for
Action-backed calls. Actor trust, effective caller, target eligibility and trigger policy remain
separate. Add the lifecycle method to a delegator's explicit method scope; ordinary privileged
actor admission must never become the effective caller's permission. A caller cannot edit its own
`resumePolicy`, `wakeOnSend`, execution account or wake budget through the resume tool.

## Proposed API and tools

Resume needs an environment, not a Session. Support Pod and VM targets that implement it; other
environment kinds may truthfully return an unsupported capability. The initial public names below
are proposed, not existing callable operations.

| Surface                                                           | Request                                                | Response / role                                                                                                           |
| ----------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| `EnvironmentDiscovery.GetEnvironment` / `environments__get`       | Existing exact target; optional bounded readiness wait | Extend the descriptor with explicit-resume eligibility, denial reason, current wake generation and an opaque resume token |
| `EnvironmentLifecycle.ResumeEnvironment` / `environments__resume` | `{target: TargetHandle, resume_token: string}`         | Typed `REQUESTED`, `ALREADY_RUNNING`, or `STATE_CHANGED` result, target and observed generation; errors remain structured |
| `agentplane environments resume`                                  | Same target/token contract                             | Direct gRPC client with the same authorization; return acceptance separately from readiness                               |

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

Dispatcher first authorizes and retains the input. Idle suspension plus `wakeOnSend=true` permits
the reviewed effective-caller wake path. A requested stop leaves input queued with a structured
manual-stop reason; sending does not erase the stop. A central Session can accept input without
waking its attached execution environment when its harness does not require one to run.

## Implementation and acceptance after review

Implement the shared lifecycle policy/mutation owner, then its authenticated RPC, Discovery
projection and CLI/Action surfaces. Update the operator path and Dispatcher wake deliberately;
their current allowlist/manual-stop semantics change only with the reviewed matrix. Use new Goose
migrations if moving existing budget records requires a schema change, preserving retained state.

Focused coverage must include same-account and delegated success, foreign/recreated accounts,
target replacement, operator stop racing resume, concurrent tokens, budget exhaustion, lost replies,
quiesce/readiness failure and restart reconciliation. Exercise an actual suspended target followed
by a successful new exec. Existing no-replay and process-domain fences remain binding; broad
disruption exercises are still deferred.

Review outcome needed: select the target opt-in, same-account message wake and sticky-stop rules
above. Record any rejected alternative before implementation; this proposal authorizes no rollout.
