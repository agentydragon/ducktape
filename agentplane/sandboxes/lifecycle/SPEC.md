# EnvironmentLifecycle SPEC

## Ownership

`lifecycle.Service` owns current resume policy checks and the guarded desired-state update for an
existing Sandbox. It runs in the Runner Gateway. It is shared in-process by the explicit RPC, the
Dispatcher automatic-wake path, and Discovery's read-only eligibility assessments. It does not own
Sandbox reconciliation or readiness; those remain with the Sandbox Controller.

The Kubernetes client is uncached. Every operation identifies a target by namespace, name and UID,
checks the original effective caller's live ServiceAccount UID, and reads the current controller-owned
execution ServiceAccount. A replacement Sandbox or ServiceAccount cannot inherit old authority.

## Authority

- An ordinary direct or delegated caller may resume an idle-suspended Sandbox only when its effective
  ServiceAccount namespace, name and UID exactly match the current execution account and
  `spec.resumePolicy.allowOwnServiceAccount` is true.
- Delegated admission retains the actor separately. The handler uses only `auth.EffectiveCaller` for
  ordinary policy; actor Pod proof and actor allowlist membership do not become the caller's grant.
- A direct TokenReview-proven Pod-bound account listed in `operator_callers` may explicitly resume a
  requested stop. This allowlist is separate from `wake_callers`; delegated calls cannot exercise it.
- `wake_callers` remains a restricted broker grant for automatic wake only. Automatic wake always
  uses the original effective caller identity, requires `spec.wakeOnSend`, and cannot override
  `spec.running=false`.
- Neither `resumePolicy` nor `wakeOnSend` grants Kubernetes access or permission to alter policy.

## Concurrency and outcomes

Discovery returns a deterministic opaque precondition derived from target UID and wake generation.
It is not a bearer capability. An ordinary `ResumeEnvironment` call requires that token. A direct
Pod-bound configured operator may omit it to state a fresh intent; the service captures the current
generation internally and still guards the write. Delegated callers never inherit token omission.

The service rereads and reauthorizes on every resource-version conflict. A stale token returns
`STATE_CHANGED` with current eligibility and generation rather than retrying against a later
suspension. It bounds conflict retries. A patch error may have uncertain effect, so the service
returns a typed `OUTCOME_UNKNOWN` status and does not replay the patch. The shared wake budget is
reserved before patching and is keyed by effective caller namespace/name/UID, target Sandbox UID and
next generation.

`REQUESTED` means the desired running state and next wake generation were accepted. It does not mean
the environment is ready. `ALREADY_RUNNING` means no wake mutation was needed. Quiescing and deleting
targets are not resumed by this service; the Controller owns the old incarnation's transition.

## Assessments

`Eligibility` describes current explicit-resume permission. `AssessAutomaticWake` describes whether
an automatic request is currently permitted, including `wakeOnSend` and the separate broker grant.
Both are read-only and do not reserve budget. `RequestWake` applies that same automatic policy,
reserves budget, then performs the guarded mutation. `WakeAssessment.ELIGIBLE` is an assessment only;
`REQUESTED` records a successful mutation, with timestamp and observed generation on both.

Expected wake and policy outcomes use the shared `agentplane.proto` lifecycle messages. gRPC errors
attach typed `ResumeEligibility` or `WakeAssessment` details so callers do not parse text. The shared
facts proto does not import Discovery; the lifecycle service proto may reuse Discovery's target
handle.
