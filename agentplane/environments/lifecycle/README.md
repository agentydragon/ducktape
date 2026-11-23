# Environment lifecycle manager

Explicit resume belongs to [`environments/internal/resume`](../internal/resume/manager.go).
Automatic wake execution and its budget contract belong to
[`session_dispatcher/dispatcher/wake`](../../session_dispatcher/dispatcher/wake/manager.go). Only the latter
requires a shared budget and exposes `RequestWake`; explicit resume accepts no budget.

Both use `lifecycle.Policy` for read-only eligibility and the shared guarded-write loop, preserving
one implementation of authorization, optimistic conflict retry and uncertain patch outcomes. The
shared package neither handles RPC requests nor owns a budget. Both owners reread the exact Sandbox
UID and current execution binding through an uncached Kubernetes client. Explicit Identity grants
come from the consumer's typed policy reader and require healthy, synchronized watches.

Ordinary ServiceAccount self-resume is retired. Explicit resume accepts an Identity with a current
`environment` grant for `resume`, or an exact service SPIFFE ID in `service_operators`.
Delegated requests are authorized as their effective Identity; the actor does not inherit authority.
Identity grants never override a requested stop and fail closed when policy is unavailable.
Identity automatic wake uses the existing `spec.resumePolicy.allowOwnServiceAccount` opt-in only
when the Sandbox currently acts as that exact live Identity. Same-name ServiceAccounts no longer
inherit the Identity path. `spec.wakeOnSend` gates automatic requests. `wake_service_callers` is an
automatic-only broker grant. A ServiceAccount caller holds no wake or resume grant. Dispatcher maps rows with Action
proof to their service actor's broker grant, while retaining the effective Identity on the row;
Environments keeps an independent service-caller list for its own assessments.

Resume tokens bind the Sandbox namespace, name, UID and observed wake generation. They are
preconditions, not capabilities. Writes use resource-version optimistic locking and recheck policy,
readiness and live Identity incarnations after conflicts. A stale generation returns state changed.
An error after a patch may have reached the API server is reported as an unknown outcome and is never
retried by the service. Automatic `RequestWake` reserves a bounded attempt from the shared
`WakeBudget`, keyed by caller principal (ServiceAccount namespace/name/UID, Identity
namespace/name/UID, or exact service SPIFFE ID), Sandbox UID and next generation. Explicit
`ResumeEnvironment` does not reserve wake budget.

`REQUESTED` reports that desired state was accepted. The Sandbox Controller owns stopping, startup,
readiness and execution-incarnation transitions; lifecycle acceptance does not imply readiness.

It has no gRPC registration or separate process.

See [SPEC.md](SPEC.md) for the RPC and authority contract.
