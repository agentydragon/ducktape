# Environment lifecycle manager SPEC

## Ownership

`lifecycle.Manager` owns current resume policy checks and the guarded desired-state update for an
existing Sandbox. It runs in the Runner Gateway. It is shared in-process by the explicit RPC, the
Dispatcher automatic-wake path, and EnvironmentService's read-only eligibility assessments. It does not own
Sandbox reconciliation or readiness; those remain with the Sandbox Controller.

The Kubernetes client is uncached. Every operation identifies a target by namespace, name and UID,
and reads the current controller-owned execution ServiceAccount or Identity. Explicit resume accepts
either an Identity with a live `environment` grant for `resume`, or a direct, live Pod-bound
ServiceAccount in `operator_callers`. Automatic wake accepts a live Identity matching the Sandbox's
current Identity binding or an explicitly configured ServiceAccount broker. A replacement Sandbox,
ServiceAccount or Identity cannot inherit old authority.

## Authority

- Ordinary ServiceAccount self-resume is retired. An Identity may explicitly resume a matching
  Sandbox only through a current `environment` policy grant for `resume`. A grant may select a
  foreign target even when the caller cannot read it. Without a grant, the manager returns
  `NotFound` unless the Sandbox is currently bound to that exact live Identity; read grants remain
  outside this slice.
- Direct Identity and delegated Identity callers use the effective Identity as the grant subject.
  The delegated actor remains authentication provenance and never supplies resume authority. A
  direct TokenReview-proven Pod-bound ServiceAccount in `operator_callers` keeps its separate
  explicit-resume path, including the ability to resume a requested stop.
- An Identity grant never overrides a requested stop. Policy readiness is required for Identity
  eligibility and every retry; a missing or unavailable policy view fails closed.
- Dispatcher automatic wake for queued work attributed to an Identity (including its own workload or
  a component acting for it) may wake an idle-suspended Sandbox under the existing
  `spec.resumePolicy.allowOwnServiceAccount` opt-in. The Sandbox's `spec.identity` must name that
  Identity, and the live Identity UID must match. This existing opt-in now covers the Sandbox's own
  effective Identity; it does not authorize a ServiceAccount with the same name. Broker and operator component grants do not supply Identity authority.
- Delegated admission retains the actor separately. Lifecycle authorization uses the effective
  Identity from `auth.Admitted`; actor Pod proof and actor allowlist membership do not become the
  caller's grant.
- A direct TokenReview-proven Pod-bound account listed in `operator_callers` may explicitly resume a
  requested stop. This allowlist is separate from `wake_callers`; delegated calls cannot exercise it.
- `wake_callers` remains a restricted ServiceAccount broker grant for automatic wake only. Identity
  self-wake and broker wake require `spec.wakeOnSend` and cannot override `spec.running=false`.
- Neither `resumePolicy` nor `wakeOnSend` grants Kubernetes access or permission to alter policy.

## Concurrency and outcomes

Discovery returns a deterministic opaque precondition derived from target UID and wake generation.
It is not a bearer capability. An ordinary `ResumeEnvironment` call, including an Identity grant,
requires that token. A direct Pod-bound configured operator may omit it to state a fresh intent; the
manager captures the current generation internally and still guards the write. Delegated callers
never inherit token omission.

The manager rereads and reauthorizes on every resource-version conflict, including policy readiness,
the live caller Identity and the target's live bound Identity when a grant selector uses it. A stale
token returns `STATE_CHANGED` with current eligibility and generation rather than retrying against a
later suspension. It bounds conflict retries. A patch error may have uncertain effect, so the manager
returns a typed `OUTCOME_UNKNOWN` status and does not replay the patch. The shared wake budget is
reserved before patching and is keyed by effective caller namespace/name/UID (an Identity by the
manager's namespace, its name and UID), target Sandbox UID and next generation.

`REQUESTED` means the desired running state and next wake generation were accepted. It does not mean
the environment is ready. `ALREADY_RUNNING` means no wake mutation was needed. A current-generation
idle suspension is resumable only after `StopStageStopped`; while its stop stage is incomplete, the
manager returns a typed `FailedPrecondition` quiescing assessment without reserving wake budget or
changing the Sandbox. A stale idle marker from an earlier generation does not block a newer accepted
wake. Quiescing and deleting targets are not resumed by the manager; the Controller owns the old
incarnation's transition.

## Assessments

`Eligibility` describes current explicit-resume permission, including the typed
`ALLOWED_IDENTITY_GRANT` reason. `AssessAutomaticWake` describes whether
an automatic request is currently permitted, including `wakeOnSend` and the separate broker grant.
Both are read-only and do not reserve budget. `RequestWake` applies that same automatic policy,
reserves budget, then performs the guarded mutation. `WakeAssessment.ELIGIBLE` is an assessment only;
`REQUESTED` records a successful mutation, with timestamp and observed generation on both.

Expected wake and policy outcomes use the shared `agentplane.proto` lifecycle messages. gRPC errors
attach typed `ResumeEligibility` or `WakeAssessment` details so callers do not parse text. The shared
facts proto does not import the API proto; the EnvironmentService proto owns its target handle.

## API boundary

The manager is an in-process component shared by `EnvironmentService` and Dispatcher automatic
wake. Only `EnvironmentService` is registered as a public environment API. Its list/get operations
remain read-only; explicit resume uses the policy and mutation contract above.
The public service exposes no suspend operation. Any future public suspend API requires a separately
reviewed authority and retry contract.
