# Environment lifecycle manager SPEC

## Ownership

Explicit `ResumeEnvironment` handling lives in
[`environments/internal/resume`](../internal/resume/manager.go). Its manager accepts no budget and
cannot issue automatic wake requests. Dispatcher constructs
[`dispatcher/wake.Manager`](../../session_dispatcher/dispatcher/wake/manager.go), whose constructor
requires the shared PostgreSQL wake budget and accepts only automatic-wake grants.

Both owners use `lifecycle.Policy` for uncached authorization, read-only projections and the same
guarded write loop. Automatic writes require a Dispatcher reservation callback, invoked after
reauthorization and immediately before each patch attempt. Budget keys and reservation execution
belong to Dispatcher; the shared package has no budget or EnvironmentService wire dependency.
Neither owner reconciles Sandboxes or determines readiness; those stay with the Sandbox Controller.

The Kubernetes client is uncached. Every operation identifies a target by namespace, name and UID,
and reads the current controller-owned execution ServiceAccount or Identity. Explicit resume accepts
either an Identity with a live `environment` grant for `resume`, or an exact service-SPIFFE
operator. Automatic wake accepts a live Identity matching the Sandbox's current Identity binding or
an exact service-SPIFFE broker. A ServiceAccount caller holds neither grant. A replacement Sandbox,
ServiceAccount or Identity cannot inherit old authority.

## Authority

- Ordinary ServiceAccount self-resume is retired. An Identity may explicitly resume a matching
  Sandbox only through a current `environment` policy grant for `resume`. A grant may select a
  foreign target even when the caller cannot read it. Without a grant, the manager returns
  `NotFound` unless the Sandbox is currently bound to that exact live Identity; read grants remain
  outside this slice.
- Direct Identity and delegated Identity callers use the effective Identity as the grant subject.
  The delegated actor remains authentication provenance and never supplies resume authority.
- An Identity grant never overrides a requested stop. Policy readiness is required for Identity
  eligibility and every retry; a missing or unavailable policy view fails closed.
- Dispatcher automatic wake for rows attributed as an Identity workload or Identity delegation may
  wake an idle-suspended Sandbox under the existing
  `spec.resumePolicy.allowOwnServiceAccount` opt-in. The Sandbox's `spec.identity` must name that
  Identity, and the live Identity UID must match. This existing opt-in now covers the Sandbox's own
  effective Identity; it does not authorize a ServiceAccount with the same name. Broker and operator component grants do not supply Identity authority.
- Explicit delegated resume retains the actor separately and uses the effective Identity from
  `auth.Admitted`; actor Pod proof and actor allowlist membership do not become the caller's grant.
- An exact service SPIFFE ID in `service_operators` may explicitly resume a requested stop. This
  allowlist is separate from `wake_service_callers`; delegated calls cannot exercise it.
- `wake_service_callers` is an exact-SPIFFE broker grant for automatic wake only. Identity self-wake
  and broker wake require `spec.wakeOnSend` and cannot override `spec.running=false`. Dispatcher
  maps Action-attributed rows to their service actor for this check and retains the effective Identity
  as row provenance. Environments configures its own list for its assessment path; that list does not
  authorize Dispatcher's in-process wake manager.
- Neither `resumePolicy` nor `wakeOnSend` grants Kubernetes access or permission to alter policy.

## Concurrency and outcomes

Discovery returns a deterministic opaque precondition derived from target UID and wake generation.
It is not a bearer capability. An ordinary `ResumeEnvironment` call, including an Identity grant,
requires that token. A configured service operator may omit it to state a fresh intent; the
manager captures the current generation internally and still guards the write. Delegated callers
never inherit token omission.

The manager rereads and reauthorizes on every resource-version conflict, including policy readiness,
the live caller Identity and the target's live bound Identity when a grant selector uses it. A stale
token returns `STATE_CHANGED` with current eligibility and generation rather than retrying against a
later suspension. It bounds conflict retries. A patch error may have uncertain effect, so the manager
returns a typed `OUTCOME_UNKNOWN` status and does not replay the patch. Only automatic `RequestWake`
reserves the shared wake budget before patching, keyed by caller principal (an Identity's
manager namespace/name/UID or a service's exact SPIFFE ID), target Sandbox UID and next
generation. Explicit `ResumeEnvironment`, for both service operators and Identity grants, does not reserve budget and is unaffected by budget exhaustion or unavailability.

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
Both are read-only and do not reserve budget. `AssessAutomaticWake` is available on the
explicit-resume manager so inventory retains its automatic-eligibility response. `RequestWake`,
available only on Dispatcher's budget-backed `wake.Manager`, applies that same automatic policy, reserves budget,
then performs the guarded mutation. `WakeAssessment.ELIGIBLE` is an assessment only;
`REQUESTED` records a successful mutation, with timestamp and observed generation on both.

Expected wake and policy outcomes use the shared `agentplane.proto` lifecycle messages. gRPC errors
attach typed `ResumeEligibility` or `WakeAssessment` details so callers do not parse text. The shared
facts proto does not import the API proto; the EnvironmentService proto owns its target handle.

## API boundary

The two owners share in-process policy and guarded-write code, not a manager instance or policy
index. Environments makes no call to Sessions. Only `EnvironmentService` is registered as a public environment API. Its list/get operations
remain read-only; explicit resume uses the policy and mutation contract above.
The public service exposes no suspend operation. Any future public suspend API requires a separately
reviewed authority and retry contract.

Service SVID role grants and delegation: [EnvironmentService SPEC](../internal/service/SPEC.md#resume-and-ownership).
