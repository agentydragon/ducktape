# Next Piraeus nodes: evacuation and capacity evidence

Preparation on 2026-10-22 for SSD control plane `ovh-ns104952` and a possible
paired HDD worker. No subsequent node shutdown, reset, wipe or reformat has
started. Use the [node-roll runbook](../../../docs/runbooks/piraeus_node_roll.md)
and refresh its gates before those operations.

## Completed early SSD evacuation

The operator authorized starting Seaweed evacuation before the node roll.
[The accepted proof](ssd_evacuation_proof.json) records 33 native, serial moves
from `seaweedfs-volume-ssd-1` on `ovh-ns104952` to the Piraeus SSD server on
`ovh-ns1001419`, capped at 20 MiB/s. At that acceptance the source was online, empty and in
maintenance; existing source/peer Pod UIDs were unchanged.

The original 34 logical SSD volumes all remain on the two survivors. Live
writes allocated IDs 1117 and 1118 during evacuation: the final inventory is
**36 logical SSD volumes, each with two copies** on `ovh-ns104963` and
`ovh-ns1001419`. The replication dry-run had no findings, three masters agreed
on the leader, and Attic/Study Casino remained 3/3 healthy. A frozen initial-ID
equality check rejected those legitimate new allocations; the final acceptance
required every original ID plus both copies of the current live set.

The subsequent handoff refreshed both Forgejo and both Haku SSD FUSE clients
serially, preserving a Ready peer. An owned shared-file hash matched through
both Forgejo clients before/after, and source request counters were quiet for
64.8 seconds. PR #11586 then retired only the empty `ssd-1` Pod; its old PVC/PV
and data remain retained. Empty storage alone was not the retirement gate.

## SSD service handoffs — 2026-10-22

Master-1 moved from `104952` to a fresh retained 4Gi HDD claim on `102453`,
using a fresh cold archive and exact same-name claim replacement. Masters 0/2
kept their Pod UIDs; all three agreed afterward, Raft topology identity was
preserved, and retained/fresh owned S3 hashes matched. Removing the middle
ordinal required orphaning/adopting the StatefulSet with `OnDelete`, not scaling
the group down.

Attic and Study Casino promoted their existing caught-up Piraeus members on
`1001419`; their old `104952` standbys still need retirement. Study remains 1Gi.

Forgejo Valkey replica-1 now uses a new 2Gi Piraeus claim on `1001419`, with its
source PV retained. The initial Flux hold on RedisReplication propagated into
the Pod template and rolled the serving master. Both original claims recovered
before a revised attempt used the operator's native pause. That attempt kept
the recovered master's Pod UID unchanged, verified native offset replay and
resumed all controllers. A later check again found master/replica Ready, link
up and matching offsets. The shared claim template is still local-path: retain
the per-ordinal override for recovery. The runbook records both operator caveats.

The seven two-member PostgreSQL clusters use serial temporary-third-member
handoffs. Progress is recorded in [the sanitized acceptance proof](ssd_service_handoffs.json); old source claims remain detached and retained after acceptance.

## Data preservation before reformat

The read-only data-disk audit found 19 claim-shaped directories: 12 matched the
protected source PV inventory and seven had no current UID owner. The unmatched
paths report 1,784,682,386 bytes. Same-name replacement claims elsewhere do not
prove those old generations are disposable. The full tree reports 27.47GB,
while XFS reports 36.25GB used; the Talos directory API does not attribute the
roughly 8.78GB difference to individual allocated paths.

Quiesce remaining consumers and preserve all 19 directories off-node before
whole-disk destruction. Budget 50Gi on a node outside both proposed outage
members; preserve ownership, modes, timestamps, ACLs, xattrs, links and sparse
files, with hashes and a verified recovery copy. No such archive exists yet.
Native replication/cold-copy acceptance remains the current-service recovery
proof. No target data disk has been wiped.

## HDD worker comparison and first-copy boundary

The [read-only worker audit](worker_pair_audit.md) compares `103656` and `102453`,
actual data-disk layouts, local claim usage, the 496-volume/400-slot obstacle,
physical copy budgets and PostgreSQL/ClickHouse/Keeper gates. Prefer `103656`
once those gates pass: last HDD ordinal, fewer database handoffs, and no
ClickHouse data replica on that node.

PR [#11577](https://github.com/agentydragon/ducktape/pull/11577) merged the
600-slot HDD configuration; all three Pods were Ready and registered at 600
before copying. It creates no physical space.

For an early partial evacuation, copying the approximately 428 GB headed to
the 4 TB node `102453` has substantial headroom. The approximately 621 GB headed
to the smaller `103711` is a separate gate: a refreshed snapshot projects only
233.62GB free (11.7%), 66.29GB short of the 15% reserve before growth.
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

## HDD headroom proposal

Hold the approximately 621GB wave to `103711`. Its ClickHouse data replica uses
about 288.76GB allocated, and moving it to SSD before that wave would project
about 522.38GB free (26%) afterward. The other non-Seaweed claims cannot provide
the missing space by themselves. Unmatched directories are not reclaimable
without ownership/retention resolution.

Prepare a native third ClickHouse replica on `1001419` with a fresh 400Gi
Piraeus claim, preserving both old 500Gi claims and Keeper membership during
catch-up. Review the per-replica layout, schema/user state, physical pool
headroom and original member identities before applying. Only after native
acceptance may the exact old `103711` data replica and its retained files be
retired. Count reclaimed space only after filesystem verification.

At the proposal snapshot, CSI advertised 456.20Gi: the 400Gi individual request
fits that value; 500Gi does not. This report is not a reservation ledger of
logical claim limits. Actual thin-data use must be refreshed after PostgreSQL
clones; using the earlier 12.61Gi physical-use sample, projected free after
measured PostgreSQL and ClickHouse data is roughly 165Gi including a full 2Gi
Valkey allowance. The metric refresh timed out, so this is a conditional
projection, not current measured headroom. Keep an initial 95Gi
free envelope for merge/growth, derived from the largest observed partition
and sampled merge output, not a measured concurrent peak.

The proposed aggregate limits would reach 720Gi after all seven PostgreSQL
claims and ClickHouse bind, above the 468.94Gi pool. Create the requested
capacity-debt issue only when overcommit actually becomes live. This is a
proposal; no ClickHouse replica or claim has been added.
