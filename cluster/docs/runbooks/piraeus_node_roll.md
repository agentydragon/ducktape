# Evacuate and reimage an OVH Talos node

Take one Piraeus node out of service, reset or reimage it, and restore its
application members: for a disk failure, a Talos reset, a system-layout change
or adding a new node. Every OVH node already runs the split system layout and a
whole-data-disk pool ([Piraeus block storage](../piraeus.md)); this runbook keeps
the gates and handoffs that worked during the migration
([lessons](../lessons_learned/2026_10_24_piraeus_ovh_migration.md)). Each
destructive window needs its own fresh inventory, disk identities and the
operator's go-ahead.

Roll one node at a time. One HDD worker may overlap one SSD control plane only
after both are evacuated and the union of their outages passes every gate below;
never overlap two control planes or two HDD workers. Shared services (Seaweed
masters, filer PostgreSQL, Kubernetes, LINSTOR, the network) are evacuated one
at a time even then.

## Gates

Record evidence immediately before the window, before each wipe, and after each
node returns. In a pair, evaluate against **both targets simultaneously**.

| Gate              | Required observation                                                                                                                                                                                                                                   |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Kubernetes / etcd | All three etcd members healthy before departure and after rejoin; at most one member unavailable, with both survivors healthy throughout. API, DNS, authentication, Flux and the Terraform state backend remain reachable off-target.                  |
| Seaweed masters   | Three healthy masters at boundaries, consistent leader and peer list; at least two healthy masters outside the outage set. No overlapping master handoffs. Preserve Raft state and cluster identity.                                                   |
| Seaweed data      | No missing or under-replicated volumes. Before retiring each source, every affected volume has two verified healthy copies outside the outage set in its required media/DC/rack. Keep sources online until evacuation and client refresh are accepted. |
| PostgreSQL        | Each affected replicated cluster has at least two healthy, caught-up members outside the outage set before disk destruction. Move every target primary first. Resolve singleton clusters explicitly.                                                   |
| Piraeus           | Controller metadata, operator and CSI control services survive off-target; the target is fenced from autoplacement. Existing strict-local application volumes remain on available nodes.                                                               |
| Other state       | ClickHouse Keeper retains a majority; replicated data services retain a healthy copy. Handle Valkey queues, WALs, ingesters, backup receivers and singleton local claims individually. A successful generic drain does not prove data safety.          |
| Capacity          | Remaining nodes can carry workloads and full evacuated data with agreed headroom. Bound copy concurrency and bandwidth across both lanes; inspect pool data and metadata, disk latency, memory pressure and replication lag.                           |

If a shared gate fails, pause both lanes and restore redundancy before continuing.
Do not add another node to the window. Finish restoration and all boundary gates
before the next node or pair.

## 1. Prepare the node-specific changes

Reimaging an existing node needs no configuration change: its Talos layout, its
Piraeus satellite (by-id data device) and its pool monitoring are already
declared. Re-read disk identities from the live node and compare them with
[`ovh-nodes.tf`](../../terraform/main/ovh-nodes.tf) and
[`piraeus.py`](../../cdk8s/piraeus.py) before the window; a replaced disk changes
its serial and by-id path.

A new node needs, reviewed and merged with the operator's go-ahead:

- An entry in `piraeus_talos_node_layouts` (system disk selector and role profile).
- A per-node satellite configuration in `piraeus.py` with the data disk's by-id
  path, selected by hostname plus `storage.allegedly.works/piraeus-ready`, and
  `ExtCmd/WaitTimeout` for HDDs.
- Pool monitoring in [`piraeus_metrics.py`](../../cdk8s/piraeus_metrics.py), with
  per-node absent/stale detection.
- Its Seaweed volume group, if it hosts one (step 4).

Before the window, fence the node in LINSTOR so new diskless tiebreakers do not
land on it: `linstor node set-property NODE AutoplaceTarget false`. Prepare
workload evacuation changes separately from the node layout, recording original
instance counts and claim sizes; temporary replicas are not a capacity decision.

Inspect a machine-readable Terraform plan. It must not reset another node,
replace persistent cluster identity, invoke the OVH rescue/dd installation path,
or apply partition changes to a serving machine. Do not run a broad apply to
start maintenance: partition changes go through the Talos maintenance API.

## 2. Evacuate application state while sources remain available

Check spare capacity and complete the serial CNPG switchovers below before
cordoning the selected node. Inventory all Pods,
PVC/PV UIDs, backends, snapshots, application roles and owners. Protect source
PVs with Retain before releasing any claim. Retain does not protect files from
a physical disk wipe; off-node application copies are the recovery source.
Review emptyDir contents before allowing drain to delete them.

### PostgreSQL

Use CNPG's native streaming replication, one cluster at a time:

CNPG attempts to switch primaries off an unschedulable node. If the target hosts
several primaries, perform their native switchovers serially before cordoning:
verify the candidate has replayed an observed primary WAL position, promote it,
then require healthy streaming members and the correct read-write Service
endpoint before the next cluster. Cordon after every primary is off-target;
retire and rebuild the source standbys serially afterward.

1. For each CNPG cluster with a member on the target (see
   [CNPG conventions](../cnpg_conventions.md#current-compliance)), work serially:
   finish and verify one before starting the next. Record its desired count,
   each Pod/PVC UID, size, StorageClass and primary. New claims use
   `piraeus-ssd-local`, sized for measured PGDATA plus headroom.
   Measure actual PGDATA, including WAL. Stop before requesting a replacement that cannot hold the
   current data and agreed reserve.
2. Put a brief, resource-specific Flux hold on that Cluster, then temporarily
   request a third member. Required hostname anti-affinity should place the new
   Piraeus claim on the available converted SSD node. Verify actual placement,
   three healthy members, and replay through an observed primary WAL position
   before promotion. Do not start another cluster during this sequence.
3. Switch primary off-target and observe the new primary plus streaming
   standbys. This includes the filer DB and Terraform state DB before depending
   on their availability for other maintenance.
4. Keep the newly seeded Piraeus member and retire only the exact old member
   on the target node using the CNPG-supported workflow. Check the operator's
   member selection; scaling down alone does not identify which ordinal it
   will remove. UID-guard Pod/PVC operations and retain the PV. Verify two
   healthy members remain outside the outage set.
5. Restore the Cluster's desired count before removing its Flux hold. Verify
   Flux and CNPG agree, then proceed to the next cluster.
6. Three-member clusters (Attic, Study Casino) need no temporary member: move a
   target primary first and retain two healthy off-target members. Study Casino
   stays **1Gi**.

Use a brief, resource-specific Flux hold for each temporary member change. A
parent can undo suspension of a child Kustomization; hold the managed resource
instead. Remove the hold only after the Cluster and CNPG agree on the restored
desired count. Pause the CNPG operator only for a bounded member-removal
transaction if necessary, not while waiting for replication. Record and remove
every hold.

### Seaweed volume servers

Keep the source running while using native locked evacuation, with new
allocations disabled on that source. Check current `weed shell` command help;
rate-limit moves and require two healthy destination copies per affected volume
outside the outage set before final retirement. Never delete surplus replicas
as an automatic part of the readiness check.

The hourly `replication-repair` AdminScript (<../../cdk8s/seaweedfs/cluster.py>)
runs copy-only `volume.fix.replication -apply -doDelete=false`. Before a planned
volume-server outage, set its `suspend: true` in Git and wait for any active Job
to finish; restore it once placement is stable. Without `-doDelete=false` (the
value attached to the flag), an applied repair also deletes surplus and
misplaced replicas. `volumeServer.evacuate` aborts on the first transient gRPC
error over the overlay, so rerun it until the source shows zero volumes.

For SeaweedFS 4.46, `volume.fix.replication` can exit with `lock is lost` after
the client loses its renewable admin-lock state. Treat that command error
separately from the Job deadline: inspect Pod termination and logs, and do not
read `BackoffLimitExceeded` as the underlying cause. Before starting another
manual repair, use the read-only `GetAdminLockStatus` and verify no repair is
active.

Refresh long-lived FUSE clients while the old endpoint still exists, then check
that requests no longer depend on it. The [retirement caveat](rolling_seaweedfs_volume_pvc.md#caveat-retiring-a-volume-server-refresh-fuse-clients-before-you-delete-it)
describes the cached-location failure: an alive-but-empty server still proxies
reads, so a stale client never re-resolves. A rolling restart that lands on the
same node keeps its CSI mount staged; scale the consumer to zero and back to
respawn the mount. Refresh `seaweedfs-s3` and `public-s3` Pods serially for the
same reason. Do not treat a generic PVC-roll drain as proof of evacuation.

Each node's volume server is its own `volumeTopology` group, so retiring it
removes a whole group rather than an ordinal. Avoid `volumeServer.leave` while
copies must stay preserved: it makes `/healthz` return 503 and liveness
restarts the server. If the owner waives two copies to shorten the window,
record the exact volume set, delete only those source copies under the admin
lock, and run copy-only repair onto the replacement server afterward; a server
added after repair started is absent from its topology snapshot, so start a
fresh run.

### Seaweed masters: identity-preserving claim moves

A master's Raft state moves by cold copy to a new claim with the same name;
masters never rebuild from peers. For master-2, the last ordinal, as when it
moved from a temporary HDD claim to `ovh-ns1001419`:

1. Capture current peer/leader agreement, cluster identity, maximum volume ID,
   survivor Pod UIDs, claim/PV identities and normal controller settings. Require
   masters 0 and 1 healthy. Retain the source PV.
2. Hold the Seaweed CR and operator HelmRelease against Flux, suspend that Helm
   release and stop the operator. Temporarily reduce only the master StatefulSet
   from three to two. Leave the Seaweed CR at three and preserve the original
   three-peer Pod template; changing the CR to two can change peers and roll
   surviving masters. Verify master-2 stopped and both survivors unchanged.
3. Mount its claim read-only in a bounded helper on the source node. Capture
   a **fresh** cold archive of the Raft directory off-node, preserving file
   ownership and modes and recording hashes. Never reuse an older archive.
4. Remove the helper and release only master-2's PVC name, with UID/resourceVersion
   guards. Keep the HDD PV and files. Create the same claim name,
   `seaweedfs-master-seaweedfs-master-2`, as a 4Gi Filesystem claim on
   `piraeus-ssd-local`.
5. A bounded restore Pod with a nodeSelector for the destination and the ordinary
   control-plane toleration becomes its first consumer. WaitForFirstConsumer
   provisions the volume there. Verify Retain, CSI handle and strict-local PV
   affinity before restoring. Compare every file hash, ownership and mode;
   preserve timestamps, including directory timestamps if extraction changes them.
6. Remove the helper, return the StatefulSet to three and let master-2 mount the
   restored claim. PV affinity keeps it on the destination node. Preserve
   its name, peer DNS and Raft identity; never run two copies of that identity.
7. Require three Ready masters, leader/peer agreement, preserved cluster identity
   and no regression of the observed allocated volume-ID watermark. Verify an
   owned S3 object's read across the handoff and a fresh write/read afterward.
   Resume operator, Helm and Flux and verify normal reconciliation. Retire the
   HDD source only after acceptance.

The deployed operator exposes a shared master persistence template, not an
ordinal-specific SC selector, and that template still names `local-path-ovh-ssd`.
Pod deletion reuses a pre-created Piraeus claim; **PVC deletion does not preserve
the override**. A template-default change must handle immutable StatefulSet
claim templates without rolling unaffected masters.

For later master handoffs, use the same cold-copy/identity checks with explicit
member control. The last-ordinal scale trick applies to master-2 only. Do not
shrink the StatefulSet to remove master-0 or master-1.

#### Master-0 or master-1: preserve the other ordinals

Master-1's move off `104952` exercised this procedure:

1. Use the controller holds, native health gates and fresh cold-copy checks
   above. Keep the Seaweed CR and master StatefulSet at three members; scaling
   to two would remove master-2.
2. Record the StatefulSet UID, Pod UIDs, original update strategy and controller
   revision. Set `OnDelete` without changing the Pod template. Delete only the
   StatefulSet with UID/resourceVersion preconditions, using the `Orphan`
   propagation policy. Wait for all three Pods to lose their owner reference,
   preserving all Pod UIDs.
3. Delete only master-1 with a UID precondition. Keep masters 0 and 2 serving
   while making the fresh cold copy and replacing the exact claim name. Mount
   the source read-only; retain the original PV and verify the destination's
   file contents and metadata before starting its member.
4. Recreate the original StatefulSet metadata/spec with three replicas and
   `OnDelete`, omitting server-assigned identity/status fields. Preserve the
   Pod template and peer identities. Require adoption of the two existing Pods,
   the new master-1 Ready, and the original controller revision matching both
   current/update revisions before restoring the original update strategy.
5. Require three-member agreement, unchanged survivor Pod UIDs, preserved Raft
   identity, volume-ID watermark and owned S3 read/write acceptance. Resume the
   operator, Helm and Flux; verify no helper or hold remains.

### Other local consumers

During HDD maintenance, keep metadata checks light: use same-user loopback HTTP
queries instead of repeatedly cold-starting the ClickHouse CLI, which adds disk
reads under contention. For stateless web workloads, use existing control-plane
overflow eligibility and Pod tolerations, and verify the replacement is Ready
before continuing.

#### ClickHouse data and Keeper

Include ClickHouse explicitly when preparing its worker nodes. Inventory each
data replica and Keeper member, the actual backend and usage of its PVC, shard
and replica identity, replication queues and any tables without replicated
copies. Requested local-path capacity is not evidence that existing data fits
the same limit on Piraeus.

Move one member at a time, preserving Keeper majority and a verified healthy
data replica outside the outage set. Prefer native replication to seed a new
data replica on its Piraeus claim, wait for catch-up and client-query checks,
then retire only the evacuated member. Use a consistent copy/restore procedure
for state that has no replicated source. Keeper needs its own identity/log and
membership handoff; a generic PVC deletion or empty replacement does not migrate
it. Verify both data and Keeper health before moving another affected member.

For fresh native MergeTree storage, replay captured `CREATE` definitions after
guarded `SYSTEM DROP REPLICA` for the exact retired, inactive replica.
The captured `traces_30d_amt_mv` and `traces_7d_amt_mv`
were already orphaned on the `102453` survivor: their source/target dependencies
were absent. Only for those verified orphans, restore the exact captured DDL/UUIDs
with client per-command
[`--allow_materialized_view_with_bad_select=1`](https://clickhouse.com/docs/reference/settings/session-settings/allow#allow_materialized_view_with_bad_select).
Keep the setting out of global configuration and compare `SHOW CREATE` with the
captured definitions before acceptance.

#### Forgejo Valkey replica

Forgejo Valkey combines cache and queue state. The pinned Redis operator copies
annotations from its RedisReplication resource into the StatefulSet Pod template;
adding a resource-level Flux hold there rolled the serving master during the
first attempt. Do not use that hold for this operator.

The revised replica-1 handoff preserved the serving master's Pod UID:

1. Hold the owning Flux Kustomization against its parent, then suspend it. Use
   the operator's native per-resource annotation
   `redisreplication.opstreelabs.in/skip-reconcile: "true"` to pause this
   RedisReplication. Leave its desired `clusterSize` at two and verify that
   neither the StatefulSet template nor the serving master's UID changed.
2. Scale the StatefulSet directly from two to one. Verify only replica-1 stopped,
   master-0 is writable with healthy AOF, and its service remains available.
   Retain the old PV and delete only the pinned replica PVC with UID guards.
3. Pre-create the same replica claim name with the reviewed Piraeus size and
   class; scale the StatefulSet back to two. Verify the new Pod/PVC/PV identities,
   intended node, Retain policy and strict-local affinity.
4. While the operator is paused, configure the new empty member to replicate
   from the serving master's current IP with native `REPLICAOF`. Require link-up,
   full-sync completion, replay of an observed master offset, one connected
   replica, healthy AOF and the shared master service's response. Never expose
   the unconfigured empty member as the serving master.
5. Remove the native pause and resume the owning Flux Kustomization, removing
   its temporary hold. Recheck native roles, link, offsets, master Pod UID and
   unchanged Pod template after reconciliation resumes.

The shared StatefulSet claim template still selects local-path. Pod recreation
reuses the replica's pre-created Piraeus claim; PVC deletion loses that override.
Record it for recovery until a separately reviewed immutable-template handoff
changes the shared default.

For a primary on the target node, use native coordinated `FAILOVER TO <replica-IP>
6379 TIMEOUT 5000` without `FORCE`, accepting the caught-up converted primary
and master Service before retiring the old Pod. Replacing ordinal zero requires
orphaning/adopting the StatefulSet while preserving ordinal one, rather than
scaling down. Seed the fresh Piraeus member with native `REPLICAOF` while the
operator is paused. After reconciliation resumes, accept the actual primary,
replica link/offsets, AOF state and Service; the operator may select ordinal zero
again. A copied Flux hold on the Pod template triggers a rollout on resume or
cleanup. Include that rollout in acceptance and remove copied holds too.

While the operator is paused, native failover does not update `redis-role` Pod
labels. Batch native role convergence, UID/resourceVersion-guarded role-label
updates and Service acceptance in one execution; do not pause for review between
promotion and routing correction. Change the demoted member's label to `slave`
before labelling the promoted member `master`, then verify both role-specific
EndpointSlices. Pin the master Service to the populated member before starting
an empty replacement. After seeding, verify both native roles and labels before
removing that pin and resuming the operator, so a primary change by the operator
cannot strand the Service on a fixed Pod name.

#### Remaining services

Inventory worker WALs, Keeper/data replicas and local application/backup
claims similarly. Treat Mimir TSDB heads/WAL and Loki writer WAL as recent data:
prepare native shutdown, flush to object storage, observe uploads/flush failures
and recent queries, then stop one ordinal gracefully. A flush endpoint's HTTP
success is not evidence that every upload succeeded. If that cannot be proved,
preserve the cold local state before replacing its claim. Restore and accept
both members before moving the other one. Preserve Alertmanager silence and
notification state through verified peer synchronization or a cold copy.

For cold Mimir/Loki copies, preserve and compare file contents, paths/types,
ownership, modes, nanosecond mtimes, symlink targets, ACLs and application
xattrs. The accepted procedure excludes only `security.selinux`, which records
host/mount context; do not repeat manual relabeling attempts to make that
attribute match. Keep the source files as rollback, verify the rebound claim
before starting the application, then verify application access and native
WAL/head replay. Prepare destination volumes and the complete executor before
stopping a member so model turns do not extend the ingestion-buffering window.

With reconciliation held, stop ordinal 0 of a two-member StatefulSet while
preserving ordinal 1: verify `whenScaled: Retain` and protect the source PV,
then atomically set
`spec.ordinals.start: 1` and `spec.replicas: 1`. A plain scale to one removes
ordinal 1. Restore the original ordinal range and replica count only after
verifying the replacement claim, and require the survivor UID and restart
count to stay unchanged. During acceptance, record Loki's live flush-queue
length; one queued flush under active ingestion is not a health failure.
Require healthy ring membership, completed WAL replay, no new corruption or
write/drop failures, and readable data across the move.

Changing a StorageClass default never migrates bound PVCs. A precreated
per-ordinal Piraeus claim can bridge an immutable StatefulSet template, but its
old local-path template remains a recovery hazard: migrate the authoritative
claim default with a controlled orphan/adopt handoff once all members are ready.
Update backup sources and verification Jobs with the application. A retained
PVC/PV with erased files is metadata cleanup, not an application migration;
verify its replacement before removing that exact obsolete record.
Move LINSTOR/CSI control services off the outage set and verify metadata
persistence and recovery access.

## 3. Rebuild the evacuated node

Before destruction, accept the gates against the current outage set and save
sanitized evidence off-node. Preserve full private machine configuration,
cluster identity and credentials securely. Record the etcd snapshot policy
the operator accepts for this window (the migration ran without snapshots,
relying on the two surviving members); that choice does not replace quorum
checks or preservation of application/Raft state.

Use direct `talosctl` access to the node's IP from the maintenance machine.
Follow the [Talos upgrade runbook](talos_cluster_upgrades.md) for image handling.
Read the node's current configuration from the `machineconfig` resource
(`talosctl get machineconfig -o json`, id `v1alpha1`); Talos 1.14 has no
on-disk config path to read.

**Gotcha:** on a worker, `talosctl upgrade --drain=true` installed the image and
then failed before reboot, because its drain called a Kubernetes API the worker's
Talos kubeconfig does not allow. Drain with `kubectl`, then
`talosctl reboot --drain=false`; do not repeat the installer.

Reset with an explicit `--wipe-mode=system-disk` and
`--system-labels-to-wipe` matching the node's actual volumes (read them first;
a split node has `STATE`, `EPHEMERAL` and its `ETCD`/`CRI`/`KUBELET`/`LOG`
partitions). Preserve EFI/META. Check pinned CLI help before use. Do not use a
default reset that wipes all disks, and do not bootstrap a second cluster.

Use the node's verified public IP directly with `talosctl` for maintenance.
Record its first maintenance certificate and pin subsequent calls with
`--cert-fingerprint`; a trusted console can independently verify that fingerprint
when available. Recording the first certificate trusts that initial connection;
pinning alone is not an independent identity check. Maintenance-mode flags
(`--insecure --cert-fingerprint <pin>`) go **after** the subcommand; before it,
`talosctl` exits with a usage error. Re-read disk serials, sizes
and partition/volume status. Require the
data disk to differ from the system disk, be writable, have no active Talos
volume allocation. Only then issue a
fingerprint-pinned FAST wipe of the explicitly identified data device. Apply
the preserved-identity machine configuration with the intended system split
through the maintenance API. Never reuse another node's fingerprint or by-id path.

Wait for the node to rejoin; verify etcd returns to three healthy members before
any further control-plane disruption. Keep workloads cordoned while accepting
the empty pool. Live label changes need a strategic-merge `talosctl patch mc`;
JSON6902 patches are rejected on multi-document configs.

Reconcile Terraform afterward with a `refresh=false` plan targeted at the one
`talos_machine_configuration_apply` instance. Diff its rendered config against
the live `machineconfig` and show the plan to the operator before applying it.
A rebuild through the maintenance API leaves Terraform state stale until then.

## 4. Accept the pool and restore members

Repeat the checks that depend on this physical node:

- Correct system partitions and data-disk ownership, satellite registration,
  thin-pool health/capacity, and fresh data/metadata monitoring for this node.
- Disposable Filesystem and Block PVC creation, actual write/read hashes and
  enforced size; correct class, Retain policy and local PV affinity.
- A controlled reboot before application restoration, with disposable data
  readable afterward, healthy pool and all three etcd members healthy again.
- Headroom and absence of disk, memory, network or storage errors.

Do not repeat the migration's QEMU matrix or benchmarks per node. Production
metadata exhaustion, DRBD-pool recovery and controller disaster restore remain
unqualified.

Before uncordoning, remove the LINSTOR `AutoplaceTarget` fence. Strict-local PVCs
select their node through the workload's
scheduling constraints and first-consumer placement. Do not leave a healthy
node cordoned as the long-term storage policy.

Restore applications in bounded steps:

1. Restore native CNPG replicas onto target-node Piraeus claims, one cluster at
   a time. Verify actual node, size/SC/Retain/affinity, existing member/claim
   identities, streaming state and replay of an observed primary WAL position.
   Restore the recorded desired count, removing only explicitly temporary
   members after full redundancy returns.
   `cnpg destroy --keep-pvc` leaves the detached PVC discoverable through its
   CNPG labels. Scaling up can reuse that old PVC even when the new-claim
   template selects another class. Under bounded Flux/CNPG holds, verify the exact
   retired PVC/PV identities, no remaining consumers, Retain policy and accepted
   data-preservation source. Remove only that retired PVC's metadata before
   requesting the fresh claim; preserve its PV and files/archive. Then let CNPG
   create the new Piraeus claim and prove its class, affinity and native replay.
2. Restore the node's Seaweed volume group: one `volumeTopology` group per node,
   hard-pinned by hostname plus `piraeus-ready`, so it stays unscheduled until
   the node passes pool admission. Keep `disk`, DC/rack, replication `001`,
   metrics and anti-affinity consistent across groups. A reimaged node's group
   comes back empty; repair or native moves refill it.
3. Verify server registration, metrics and claim/pool placement. Use a bounded
   native canary move plus owned S3 write/read verification before gradual,
   rate-limited redistribution. Existing Seaweed CSI application PVCs and
   endpoints continue unchanged.
4. Restore remaining native replicas or copied local services with their own
   acceptance checks. Remove every temporary reconciliation hold and helper.

Close the window only when all gates pass, app redundancy is restored,
Terraform/GitOps match reality, the node is schedulable, and the next operator
has the exact surviving claims and recovery sources. A paired window closes
only after **both** nodes pass.

## Stop and recovery

Before a source wipe, stop and continue serving from the unchanged source or
verified off-node replicas if any gate fails. After a wipe, recovery is from
those replicas or consistent off-node copies; retaining an old local-path PV
does not recreate erased files. Repair one lane at a time. Preserve Raft/member
identity, prevent duplicate live identities, and do not restore a stale master
copy into a new independent cluster.

Keep private archives, credentials and LINSTOR exports outside git. Record only
sanitized identities, hashes, gate results, exact PR heads and observed live
state in the owning cluster execution record.
