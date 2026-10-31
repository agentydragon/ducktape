# Electric rolling exception and optional high availability

Source audit, 2026-10-30 PDT. The operator accepts one Electric replica as an explicit exception to
the two-replica rolling target: a brief sync interruption, refetch, or page reload during replacement
is acceptable. The chart's current `Recreate` strategy may stay; no Electric deployment change is
scheduled. This is a reference for any later topology work, not a claim about live cluster state.

## Current source and protocol

The [Electric chart](../../cluster/cdk8s/agentplane/electric.py) pins Electric 1.8.1 and has one
`Recreate` Pod. Every Pod would use the same `agentplane_history` replication stream ID, while its
shape log and persistent state sit in a Pod-local `emptyDir`. The Service selects every Electric Pod.
The [App proxy](../app/go/proxy/electric.go) forwards each authorized shape request to that one
Service URL; it has no stable per-thread Electric destination. The [browser client](../app/frontend/threads/electric_sync.tsx)
holds a shape handle and offset, and handles `must-refetch` after a 409.

Electric's [upgrade guide](https://electric.ax/docs/sync/guides/upgrading) says one instance per
replication stream holds the Postgres advisory lock and actively replicates. A second instance with
the same stream waits in read-only mode: it can serve _existing_ shapes only when it shares the
shape store, and it returns 503 for new shapes until it becomes active. `/v1/health` can return 202
for both waiting and starting; Kubernetes HTTP readiness accepts both. Thus changing this chart to
two replicas and `RollingUpdate` alone would send some traffic to a waiting Pod with an empty store,
and a starting Pod could enter the Service before it can serve shapes.

The guide's shared-storage rolling example needs a common volume and
`ELECTRIC_SHAPE_DB_EXCLUSIVE_MODE=true`; it still has only one active replication writer. That is
not our current storage. The upstream [shared-store startup race](https://github.com/electric-sql/electric/issues/4637)
remains open as of this audit. The guide is a description of an upstream pattern, not evidence that
our pinned image and storage would safely run that pattern. Do not silently turn the current
`emptyDir` into a shared RWX volume or count a passive, empty-store Pod as a useful second replica.

## Optional separate HA work

Multi-active Electric HA is unscheduled and optional. Keep any future HA change independent of the
Agentplane service startup migration conversion.
The promising separate-storage topology gives each active Electric instance its own replication
slot/stream identity and shape store, as in Electric's
[separate-storage guide](https://electric.ax/docs/sync/guides/upgrading#separate-storage-ephemeral).
It needs a design and acceptance pass before a manifest change:

1. Choose stable instance identity and slot lifecycle. A second stream creates another publication
   and WAL-retaining slot. Confirm the chosen temporary-slot or explicit cleanup behavior in the
   **pinned 1.8.1 image**, and account for both slots under the [Postgres WAL cap](../../cluster/cdk8s/agentplane/database.py).
2. Route all requests for one thread's shapes to the same instance across App replicas and HTTP
   connections. The existing shared Service URL provides no such affinity. Specify failover and
   rebalance behavior when an Electric Pod is unavailable, including authenticated proxy routing.
3. Accept that different stores have different shape handles: a route change or Pod replacement
   can return 409. Verify the browser's refetch, subset and live continuation, and 503 retry through
   the real App proxy. Ensure an old handle is not mistaken for a valid shape on another instance.
4. Prove a rolling replacement and a replica failure with two useful active instances, live shape
   reads and Postgres slot/WAL observations. Verify Service readiness reflects the selected
   topology, and that stopping an instance releases or retires its replication resources.

The accepted state is one active Electric instance with the current replacement behavior. If a
single-replica rolling replacement is considered later, do not route traffic to a waiting empty-store
Pod as if it could serve existing handles; validate the resulting refetch or reload behavior before
changing the chart. This optional audit does not select a storage class, routing algorithm, slot
cleanup policy, or upgrade of the pinned image.
