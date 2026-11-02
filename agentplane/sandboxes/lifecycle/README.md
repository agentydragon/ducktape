# Environment lifecycle service

`lifecycle.Service` is the Runner Gateway's backend-owned policy and mutation point for resuming an
existing Sandbox. It serves `EnvironmentLifecycle.ResumeEnvironment` and exposes `RequestWake` for
Dispatcher's in-process automatic wake path. Discovery uses `Eligibility` and
`AssessAutomaticWake` as read-only current assessments. All paths reread the exact Sandbox UID,
current ServiceAccount UIDs, execution-account ownership and policy through an uncached Kubernetes
client.

Ordinary callers must be the exact live execution ServiceAccount and the Sandbox must opt in with
`spec.resumePolicy.allowOwnServiceAccount`. Delegated calls use their effective caller's account
and UID; the Actions actor does not add authority. `spec.wakeOnSend` gates only automatic requests.
The existing `wake_callers` broker grant remains automatic-only. A distinct `operator_callers` grant
allows a direct Pod-bound operator to resume a requested stop; a delegated actor cannot exercise it.

Resume tokens bind the Sandbox namespace, name, UID and observed wake generation. They are
preconditions, not capabilities. Writes use resource-version optimistic locking and recheck policy
after conflicts. A stale generation returns state changed. An error after a patch may have reached the
API server is reported as an unknown outcome and is never retried by the service. A shared `WakeBudget`
reserves a bounded attempt keyed by effective caller identity, Sandbox UID and next generation.

`REQUESTED` reports that desired state was accepted. The Sandbox Controller owns stopping, startup,
readiness and execution-incarnation transitions; lifecycle acceptance does not imply readiness.

See [SPEC.md](SPEC.md) for the RPC and authority contract.
