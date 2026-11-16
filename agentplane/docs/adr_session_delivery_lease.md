# ADR: one PostgreSQL lease per Session for delivery

- **Status:** Accepted (operator, 2026-11-15 00:06 UTC, card "Record it": "grabbing a lock per
  dequeue") and implemented (#16852, #16858); the per-Sandbox ownership table and Lease RBAC are
  gone (#16865, #16867)
- **Scope:** Dispatcher delivery ownership in the Sessions binary
- **Contract:** [Dispatcher specification § Session delivery lease](../session_dispatcher/dispatcher/SPEC.md#session-delivery-lease)

## Decision

A [`postgres/lease`](../postgres/lease/lease.go) per Session replaced the Dispatcher's per-Sandbox
Kubernetes ownership Lease and its database ownership generation. No replica owns a Sandbox or a
queue. Any Sessions replica dequeues a Session whose head is runnable and takes that Session's
lease, which covers the whole head step: waking the Sandbox, materializing the Session and
delivering the head command. Every write in the step fences on it, and the lease ends when the head
settles. Once a lease expires, any replica may take it and requeue that Session's in-flight work.

## Constraints that shaped it

- **Keyed by Session, not command:** the Session is the ordering unit and has at most one head in
  flight, so a command key buys no parallelism. The step starts while the head is still queued
  (wake, Session materialization), before any command is claimed. A Session key also needs no
  Sandbox, so it holds for Sessions without an environment.
- **A timed lease, not a lock held in a transaction:** the claim must outlive the delivery RPC, and
  the delivery attempt commits before that RPC (`MarkDeliveryAttempt`).
- **Unchanged:** per-Session order still comes from the head rule (a command waits while an earlier
  one is queued or delivering). The Runtime's command-ID dedupe still makes a late duplicate
  harmless; no lease fences an RPC already in flight.
