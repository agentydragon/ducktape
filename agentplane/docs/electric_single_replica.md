# Electric runs one replica

Electric is an operator-accepted exception to the two-replica rolling target (2026-10-30 PDT): a
brief sync interruption, refetch or page reload during replacement is acceptable, and the chart's
`Recreate` strategy stays. Multi-active HA is deferred
([`ELECTRIC_MULTI_ACTIVE_HA`](../plans/task_freezer.md#electric_multi_active_ha--more-than-one-useful-electric-replica)).

## Current source and protocol

The [Electric chart](../../cluster/cdk8s/agentplane/electric.py) pins Electric 1.8.1 and has one
`Recreate` Pod. Every Pod would use the same `agentplane_history` replication stream ID, while its
shape log and persistent state sit in a Pod-local `emptyDir`. The Service selects every Electric Pod.
The [App proxy](../app/internal/proxy/electric.go) forwards each authorized shape request to that one
Service URL; it has no stable per-thread Electric destination. The [browser client](../app/frontend/threads/electric_sync.tsx)
holds a shape handle and offset, and handles `must-refetch` after a 409.

## Why a second replica is not a replica count

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
was open as of the 2026-10-30 audit. The guide describes an upstream pattern, not evidence that our
pinned image and storage would safely run it. Do not silently turn the current `emptyDir` into a
shared RWX volume or count a passive, empty-store Pod as a useful second replica. A single-replica
rolling replacement must likewise not route traffic to a waiting empty-store Pod as if it could
serve existing handles; validate the resulting refetch or reload behavior before changing the chart.
