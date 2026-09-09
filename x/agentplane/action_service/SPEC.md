# Action Service

The service owns durable ActionRequests, Decisions, and single-shot Executions. Authenticated
callers see only their own requests; the separate operator interface can review and decide them.
Caller-provided provenance never assigns authority. Retry with the same caller-scoped idempotency
key recovers the same request, not another execution.

## Cancellation

Only the authenticated owning caller can cancel a request. Cancellation takes no expected version;
operator-all review authority does not grant a cancellation override. Pending Decisions and approved
but unclaimed Executions can be cancelled atomically against approval and dispatch. Success guarantees
the request cannot subsequently execute, including after restart or late approval. The dispatch
claim is the cutoff, even before the executor has physically started; dispatching, running and
execution-unknown requests refuse cancellation. No interruption is propagated to executors.

Already-cancelled requests return idempotent success; denied, succeeded and failed requests return
unchanged finished receipts. Cancellation preserves prior Decisions and records the authenticated
actor and time in the canonical event history. Reusing the original submission idempotency key
recovers the cancelled request; an intentional new attempt requires a new key.

## Bounded receipt waits

A receipt wait can return immediately or wait up to 30 seconds for either a resolved Decision or
an Execution outcome. Allowed but undispatched/running work satisfies the Decision predicate only.
Denied, cancelled, succeeded, failed, and execution-unknown receipts satisfy both predicates.
Execution-unknown is a returnable outcome, not proof of success or permission to replay.

Waits use pushed committed-state invalidations, including updates written by another process.
The durable receipt remains authoritative; duplicate/coalesced notifications do not manufacture
transitions. Setup cannot miss a concurrent commit. A deadline returns the current receipt, while
disconnect or wait cancellation only releases the wait and never cancels or resubmits the Action.
Loss of the notification channel fails waiting explicitly; immediate reads remain available and
there is no periodic state-query fallback.
