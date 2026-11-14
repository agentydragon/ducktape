# Environment lifecycle manager

`lifecycle.Manager` is the Runner Gateway's backend-owned policy and mutation point for resuming an
existing Sandbox. The public `EnvironmentService.ResumeEnvironment` handler delegates to it. It
exposes `RequestWake` for Dispatcher's in-process automatic wake path. EnvironmentService reads use
`Eligibility` and `AssessAutomaticWake` as read-only current assessments. All paths reread the exact
Sandbox UID and current execution binding through an uncached Kubernetes client. Explicit Identity
grants come from the consumer's typed policy reader and require healthy, synchronized watches.

Ordinary ServiceAccount self-resume is retired. Explicit resume accepts an Identity with a current
`environment` grant for `resume`, or a direct Pod-bound ServiceAccount in `operator_callers`.
Delegated requests are authorized as their effective Identity; the actor does not inherit authority.
Identity grants never override a requested stop and fail closed when policy is unavailable.
Identity automatic wake uses the existing `spec.resumePolicy.allowOwnServiceAccount` opt-in only
when the Sandbox currently acts as that exact live Identity. Same-name ServiceAccounts no longer
inherit the Identity path. `spec.wakeOnSend` gates automatic requests. The existing `wake_callers`
ServiceAccount broker grant remains automatic-only.

Resume tokens bind the Sandbox namespace, name, UID and observed wake generation. They are
preconditions, not capabilities. Writes use resource-version optimistic locking and recheck policy,
readiness and live Identity incarnations after conflicts. A stale generation returns state changed.
An error after a patch may have reached the API server is reported as an unknown outcome and is never
retried by the service. Automatic `RequestWake` reserves a bounded attempt from the shared
`WakeBudget`, keyed by effective caller identity, Sandbox UID and next generation. Explicit
`ResumeEnvironment` does not reserve wake budget.

`REQUESTED` reports that desired state was accepted. The Sandbox Controller owns stopping, startup,
readiness and execution-incarnation transitions; lifecycle acceptance does not imply readiness.

It has no gRPC registration or separate process.

See [SPEC.md](SPEC.md) for the RPC and authority contract.

Direct service SVIDs have independent, exact-SPIFFE-ID grants through
`environment_service_readers`, `environment_service_managers`,
`environment_service_launchers`, and `environment_service_operators`. Service operators may
explicitly resume, including a requested stop; delegated callers never inherit those roles.
`environment_service_delegation` admits a service actor only for configured read methods,
using the live effective Identity. `wake_service_callers` grants automatic broker wake only,
with budgets keyed by exact service SPIFFE ID. Create receipts retain the typed service actor
separately from the Action origin.
