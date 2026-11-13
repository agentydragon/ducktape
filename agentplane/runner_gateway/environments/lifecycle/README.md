# Environment lifecycle manager

`lifecycle.Manager` is the Runner Gateway's backend-owned policy and mutation point for resuming an
existing Sandbox. The public `EnvironmentService.ResumeEnvironment` handler delegates to it. It
exposes `RequestWake` for Dispatcher's in-process automatic wake path. EnvironmentService reads use
`Eligibility` and `AssessAutomaticWake` as read-only current assessments. All paths reread the exact
Sandbox UID and current controller-owned execution binding and policy through an uncached Kubernetes
client.

Ordinary ServiceAccount self-resume is retired. Explicit resume is limited to direct Pod-bound
ServiceAccounts in `operator_callers`; delegated callers cannot exercise that grant. Identity
automatic wake uses the existing `spec.resumePolicy.allowOwnServiceAccount` opt-in only when the
Sandbox currently acts as that exact live Identity. Same-name ServiceAccounts no longer inherit the
Identity path. `spec.wakeOnSend` gates automatic requests. The existing `wake_callers` ServiceAccount
broker grant remains automatic-only.

Resume tokens bind the Sandbox namespace, name, UID and observed wake generation. They are
preconditions, not capabilities. Writes use resource-version optimistic locking and recheck policy
after conflicts. A stale generation returns state changed. An error after a patch may have reached the
API server is reported as an unknown outcome and is never retried by the service. A shared `WakeBudget`
reserves a bounded attempt keyed by effective caller identity, Sandbox UID and next generation.

`REQUESTED` reports that desired state was accepted. The Sandbox Controller owns stopping, startup,
readiness and execution-incarnation transitions; lifecycle acceptance does not imply readiness.

It has no gRPC registration or separate process.

See [SPEC.md](SPEC.md) for the RPC and authority contract.
