# Next Piraeus nodes: evacuation and capacity evidence

Preparation on 2026-10-22 for SSD control plane `ovh-ns104952` and a possible
paired HDD worker. No subsequent node shutdown, reset, wipe or reformat has
started. Use the [node-roll runbook](../../../docs/runbooks/piraeus_node_roll.md)
and refresh its gates before those operations.

## Completed early SSD evacuation

The operator authorized starting Seaweed evacuation before the node roll.
[The accepted proof](ssd_evacuation_proof.json) records 33 native, serial moves
from `seaweedfs-volume-ssd-1` on `ovh-ns104952` to the Piraeus SSD server on
`ovh-ns1001419`, capped at 20 MiB/s. The source remains online, empty and in
maintenance; existing source/peer Pod UIDs are unchanged.

The original 34 logical SSD volumes all remain on the two survivors. Live
writes allocated IDs 1117 and 1118 during evacuation: the final inventory is
**36 logical SSD volumes, each with two copies** on `ovh-ns104963` and
`ovh-ns1001419`. The replication dry-run had no findings, three masters agreed
on the leader, and Attic/Study Casino remained 3/3 healthy. A frozen initial-ID
equality check rejected those legitimate new allocations; the final acceptance
required every original ID plus both copies of the current live set.

Keep the old endpoint available until long-lived FUSE clients are refreshed and
its request counters are quiet. Empty storage alone does not complete the
application drain or authorize deleting the StatefulSet/PVC.

## HDD worker comparison and first-copy boundary

The [read-only worker audit](worker_pair_audit.md) compares `103656` and `102453`,
actual data-disk layouts, local claim usage, the 496-volume/400-slot obstacle,
physical copy budgets and PostgreSQL/ClickHouse/Keeper gates. Prefer `103656`
once those gates pass: last HDD ordinal, fewer database handoffs, and no
ClickHouse data replica on that node.

PR [#11577](https://github.com/agentydragon/ducktape/pull/11577) proposes 600 HDD
slots per server. That changes the HDD Pod template and requires a healthy
rollout and re-registration before copying. It creates no physical space.

For an early partial evacuation, copying the approximately 428 GB headed to
the 4 TB node `102453` has substantial headroom. The approximately 621 GB headed
to the smaller `103711` is a separate gate: the snapshot projects only about
35 GB beyond its 10% free-space threshold before overhead and new writes.
The prepared first batch targets only `102453`, keeps source `103656` online,
requires the reviewed 600-slot rollout to be healthy first, caps moves at
20 MiB/s, and rechecks Pod identities, master agreement and destination space.
It stops before consuming a 15% free-space reserve on that destination. Copies
headed to `103711` remain on the source until its headroom is resolved.

The HDD source also contains older/unmatched directories. Resolve their owners
and retention before destruction. Coder workspace directory sizes may include
sparse disk images; preserve their data and sparse allocation during migration.
ClickHouse data replicas and Keeper need their explicit native handoffs when
their nodes are migrated; generic claim deletion is not a migration.
