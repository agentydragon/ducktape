# Roll an OVH Talos node onto Piraeus

Convert an evacuated node's whole physical data disk into a Piraeus thin pool,
split its Talos system volumes, then restore application members. This runbook
carries forward the first physical SSD conversion; it is not evidence that a
later node has been drained or converted. Each destructive window needs its own
accepted inventory and disk identities. Future PRs require the operator's
go-ahead before merge.

The [Piraeus migration evidence index](../../debug/piraeus_migration/README.md)
tracks the latest status separately from dated inventories and proofs. Refresh
its gates from live state before any operation.

## Next candidates and ordering

The 2026-10-22 inventory suggests `ovh-ns104952` next, then `ovh-ns104963`.
Refresh all placements before choosing a window.

| Node            | Reason / complication                                                                                                                                                                                               |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ovh-ns1001419` | First converted SSD control plane. Attic and Study Casino replicas returned on Piraeus; Seaweed passed a bounded native SSD move and separate S3 checks. Master-2 also returned to its 4Gi Piraeus claim.           |
| `ovh-ns104952`  | Hosts old SSD volume server `ssd-1`, so retiring the last ordinal permits shrinking the old group from two to one. Several PostgreSQL primaries, Seaweed master-1, and Forgejo Valkey member-1 must move first.     |
| `ovh-ns104963`  | Hosts old SSD volume server `ssd-0`, Seaweed master-0, Forgejo Valkey member-0, and the single-instance `agentplane-testing/postgres`. The singleton needs an explicit native-replication handoff or agreed outage. |

Finish the control planes first by default. One HDD worker may accompany one
control plane under the paired-roll gates below. Other workers follow; OptiPlex
and wyrm2 need separate hardware, capacity, local-data and site-dependency plans.
Do not apply this OVH disk recipe to those machines unchanged.

### Paired HDD worker and SSD control plane

Different media separate the bulk-data failure domains, but both lanes depend on
Seaweed masters, filer PostgreSQL, Kubernetes, LINSTOR and the physical network.
Evacuate shared services one at a time. Overlap the node reset/reformat/rejoin
windows only after **both nodes pass evacuation and the union of their outages
passes every gate**. Do not roll two control planes or two HDD workers under this
profile.

Worker inventory, 2026-10-22:

| Worker         | Relevant dependencies before pairing                                                                                                                                                                                                                                                                                                             |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ovh-ns102453` | Temporary Seaweed master-2, HDD volume-1, filer-0, ClickHouse data/Keeper and multiple DB replicas. Cannot accompany `ovh-ns104952` while master-2 remains here: master-1 is on that control plane. Returning master-2 to the first converted node removes this particular conflict.                                                             |
| `ovh-ns103656` | HDD volume-2 is the last ordinal, convenient for retirement. Currently hosts LINSTOR controller, CSI controller and affinity controller. Its configured LINSTOR datastore is Kubernetes; move the control Pods and verify metadata availability before taking it down. Also inspect Keeper-0, local backup receivers, Haku WAL and other claims. |
| `ovh-ns103711` | HDD volume-0, filer-1, ClickHouse data/Keeper, multiple DB members and application claims. Shrinking the old HDD group removes its last ordinal, not volume-0; use an explicitly reviewed ordinal-preserving handoff.                                                                                                                            |

No worker is selected solely from this table. Inventory every bound PV on both
targets, classify its actual backend and consumers, and choose the pair with the
smallest verified shared failure budget. A Seaweed CSI claim mounted on a target
does not mean its backing data resides on that target.

## Gates

Record evidence immediately before the window, before each wipe, and after each
node returns. In a pair, evaluate against **both targets simultaneously**.

| Gate              | Required observation                                                                                                                                                                                                                                   |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Kubernetes / etcd | All three etcd members healthy before departure and after rejoin; at most one member unavailable, with both survivors healthy throughout. API, DNS, authentication, Flux and the Terraform state backend remain reachable off-target.                  |
| Seaweed masters   | Three healthy masters at boundaries, consistent leader and peer list; at least two healthy masters outside the outage set. No overlapping master handoffs. Preserve Raft state and cluster identity.                                                   |
| Seaweed data      | No missing or under-replicated volumes. Before retiring each source, every affected volume has two verified healthy copies outside the outage set in its required media/DC/rack. Keep sources online until evacuation and client refresh are accepted. |
| PostgreSQL        | Each affected replicated cluster has at least two healthy, caught-up members outside the outage set before disk destruction. Move every target primary first. Resolve singleton clusters explicitly.                                                   |
| Piraeus           | Controller metadata, operator and CSI control services survive off-target; no live resource/snapshot depends on a target FILE_THIN directory. Existing strict-local application volumes remain on available nodes.                                     |
| Other state       | ClickHouse Keeper retains a majority; replicated data services retain a healthy copy. Handle Valkey queues, WALs, ingesters, backup receivers and singleton local claims individually. A successful generic drain does not prove data safety.          |
| Capacity          | Remaining nodes can carry workloads and full evacuated data with agreed headroom. Bound copy concurrency and bandwidth across both lanes; inspect pool data and metadata, disk latency, memory pressure and replication lag.                           |

If a shared gate fails, pause both lanes and restore redundancy before continuing.
Do not add another node to the window. Finish restoration and all boundary gates
before the next node or pair.

## 1. Prepare the node-specific changes

Current configuration is deliberately scoped to the first node. A label alone
does not prepare another pool. Review these changes before the maintenance
window; merge only with the operator's go-ahead:

- Extend [Talos configuration](../../terraform/main/ovh-nodes.tf) from first-node
  conditionals to an explicit converted-node map. Preserve the first node's
  accepted configuration and leave unconverted nodes unchanged. Generate the
  private machine configuration from the same desired source.
- Extend [Piraeus configuration](../../cdk8s/piraeus.py) with an inert, explicit
  per-node satellite selector and the target's actual data-disk by-id path.
  Preserve the existing operator, LINSTOR cluster, pool name and StorageClass.
  Enable raw-device initialization only after evacuation, old Talos data-volume
  removal and disk wipe have been verified.
- Extend [pool monitoring](../../cdk8s/piraeus_metrics.py) to the new node.
  Preserve per-node identity and absent/stale detection; one healthy node must
  not mask another missing pool.
- Exclude only the converted node from fresh local-path provisioning: empty
  nodePathMap entry and hostname exclusions in every applicable legacy SC,
  including deprecated aliases. Existing bound claims remain readable during
  evacuation. Remove only the target's legacy UserVolumeConfig at cutover.
- Prepare workload evacuation and restoration changes separately from the node
  layout. Keep original DB instance counts and claim sizes recorded; temporary
  replicas are not a decision to increase permanent capacity.

Inspect a machine-readable Terraform plan. It must not reset another node,
replace persistent cluster identity, invoke the OVH rescue/dd installation path,
or apply partition changes to a serving machine. Do not run a broad apply to
start maintenance. The first-node directory-to-partition conversion was rejected
by the live Talos configuration validator even in staged mode; perform that
transition through maintenance mode. Reconcile Terraform afterward and inspect
the remaining plan.

### Disk and system-volume worksheet

Live Talos inventory on 2026-10-22 matched Terraform:

| Node           | System disk serial   | Data disk serial     | Each physical disk |
| -------------- | -------------------- | -------------------- | -----------------: |
| `ovh-ns104952` | `BTPF8256006P450RGN` | `BTPF8304019P450RGN` | 450098159616 bytes |
| `ovh-ns104963` | `BTPF8256002V450RGN` | `BTPF8256009U450RGN` | 450098159616 bytes |

Both are Intel SSDPE2MX450G7 devices, smaller than the pilot's 512 GB disks.
Refresh serial, size, by-id mapping, partition geometry and actual system-disk
selection through Talos before execution; Linux device order is not identity.

Use the accepted SSD control-plane budgets if actual geometry fits:

| Talos volume |    Size |
| ------------ | ------: |
| ETCD         |  16 GiB |
| EPHEMERAL    |  32 GiB |
| CRI          | 192 GiB |
| KUBELET      | 128 GiB |
| LOG          |  16 GiB |

These total 384 GiB. On the 450 GB disks, the nominal remainder is about 35 GiB
**before** boot/STATE and alignment overhead. Record actual remaining space,
rather than carrying over the first node's free-space figure. Bind each volume
to the verified system serial, fixed min/max sizes, grow=false.

Give the separate data disk wholly to the existing `piraeus_ssd/data` LVM thin
pool with the accepted 4 GiB metadata, metadata spare and 64 KiB chunk sizing.
Record actual resulting capacity after creation. Worker system-volume and HDD
pool budgets need their own worksheet; do not copy the SSD disk identities or
assume an ETCD volume is required on a worker.

### Applied and proposed data-disk layouts

Live LVM reporting on 2026-10-22 confirmed that the first node has one whole-device
PV on `/dev/nvme1n1`, VG `piraeus_ssd`, with 4 MiB extents and no free VG extents.
Its thin-data LV is exactly 503517806592 bytes. Other rows are estimates using
the same layout; record the actual extent/alignment result after initialization.

| Node                          | Physical data disk |         Thin-data LV | Thin metadata + spare | Seaweed volume-server PVC |
| ----------------------------- | -----------------: | -------------------: | --------------------: | ------------------------: |
| `ovh-ns1001419`, applied SSD  |         476.94 GiB | 468.94 GiB, measured |             4 + 4 GiB |                     250Gi |
| `ovh-ns104952`, proposed SSD  |         419.19 GiB |     about 411.18 GiB |             4 + 4 GiB |                     250Gi |
| `ovh-ns103656`, candidate HDD |        1863.02 GiB |    about 1855.02 GiB |             4 + 4 GiB |   1700Gi, sizing proposal |

The HDD candidate's 2000398934016-byte HGST data disk has serial `K5H8UMLA`;
its separate system disk is `K5H5NGHA`. Keep every converted SSD Seaweed replica
at 250Gi. A common 1700Gi for converted HDD replicas is proposed pending actual
consumer-usage checks and layout review; existing HDD claims request 1800Gi.
Replace each old claim during its native migration rather than trying to shrink
an existing PVC.

These are thin logical volumes inside the shared pool, not fixed Seaweed-only
partitions. Other applications use the remainder through their own PVCs. The
first node's four application requests total 263Gi. On the HDD candidate, current
requests are 1800Gi Seaweed plus 90Gi for other local claims: 1890Gi exceeds the
estimated 1855Gi pool before DRBD overhead. Using 1700Gi leaves about 155Gi for
other claims, or 65Gi beyond those current requests before overhead. Measure
actual per-consumer usage: local-path requests were not enforced limits.

### HDD evacuation capacity checkpoint

On 2026-10-22 the HDD group had **496 logical volumes, 992 placements**, all with
exactly two copies. Evacuating any one of its three servers requires all 496
volumes on each survivor. The existing 400-slot cap therefore blocks complete
evacuation; increasing to 600 slots gives 104 spare slots per survivor at this
snapshot. A slot increase does not create physical space.

For the preferred `103656` candidate, the observed missing-copy sizes were
621350169600 bytes to `103711` and 427655280192 bytes to `102453`. Subtracting
these from reported free space predicts about 236 GB and 1957 GB free afterward.
The smaller survivor would be only about **36 GB above its 10% free-space
threshold**, before copy overhead and new writes. Refresh actual copy sizes,
other local usage, growth and headroom before declaring the space gate passed.
Do not infer adequate headroom from the enlarged slot limit.

## 2. Evacuate application state while sources remain available

Cordon the selected node after checking spare capacity. Inventory all Pods,
PVC/PV UIDs, backends, snapshots, application roles and owners. Protect source
PVs with Retain before releasing any claim. Retain does not protect files from
a physical disk wipe; off-node application copies are the recovery source.
Review emptyDir contents before allowing drain to delete them.

### PostgreSQL

Use CNPG's native streaming replication, one cluster at a time:

1. Record desired instance count, each Pod/PVC UID, size, SC and primary. Set
   the future claim class to `piraeus-ssd-local`, retaining size and disabling
   resize-in-use for this migration.
2. For a two-member cluster on the old SSD nodes, temporarily request a third
   member. Required hostname anti-affinity should place its new Piraeus claim
   on an available converted SSD node. Verify actual placement, healthy member
   count and replay of an observed primary WAL position before promotion.
3. Switch primary off-target and observe the new primary plus streaming
   standbys. This includes the filer DB and Terraform state DB before depending
   on their availability for other maintenance.
4. Remove only the identified target member using the CNPG-supported workflow.
   Check the operator's member selection; scaling down alone does not identify
   which ordinal it will remove. UID-guard Pod/PVC operations and retain the PV.
   Two healthy members must remain outside the outage set.
5. For Attic and Study Casino, already at three members, move a target primary
   first and retain two healthy off-target members. Study Casino stays **1Gi**.

Use a brief, resource-specific Flux hold if needed to prevent desired state
from undoing a handoff. A parent can undo suspension of a child Kustomization;
hold the managed resource instead. Pause the CNPG operator only for a bounded
member-removal transaction if necessary, not while waiting for CI or replication.
Record and remove every hold.

The singleton on `ovh-ns104963` needs a separate reviewed CNPG clone/promotion or
explicit outage procedure. Do not delete its only data claim.

### Seaweed volume servers

Keep the source running while using native locked evacuation, with new
allocations disabled on that source. Check current `weed shell` command help;
rate-limit moves and require two healthy destination copies per affected volume
outside the outage set before final retirement. Never delete surplus replicas
as an automatic part of the readiness check.

Refresh long-lived FUSE clients while the old endpoint still exists, then check
that requests no longer depend on it. The [retirement caveat](rolling_seaweedfs_volume_pvc.md#caveat-retiring-a-volume-server-refresh-fuse-clients-before-you-delete-it)
describes the cached-location failure. Retire only the evacuated ordinal:
`ssd-1` on `ovh-ns104952` permits old-group replicas 2 to 1; a later empty
`ssd-0` permits removal of the remaining old group. The HDD group needs the same
ordinal check. Do not treat a generic PVC-roll drain as proof of evacuation.

### Seaweed master-2: return it to the first Piraeus node

Before the next control-plane drain, return master-2 from `ovh-ns102453` to
`ovh-ns1001419`. As observed on 2026-10-22, all three masters are healthy and
agree on master-0 as leader. Master-2 uses the temporary retained 4Gi HDD claim.
The reverse handoff uses the same cold-copy mechanism as its evacuation:

1. Capture current peer/leader agreement, cluster identity, maximum volume ID,
   survivor Pod UIDs, claim/PV identities and normal controller settings. Require
   masters 0 and 1 healthy. Retain the source PV.
2. Hold the Seaweed CR and operator HelmRelease against Flux, suspend that Helm
   release and stop the operator. Temporarily reduce only the master StatefulSet
   from three to two. Leave the Seaweed CR at three and preserve the original
   three-peer Pod template; changing the CR to two can change peers and roll
   surviving masters. Verify master-2 stopped and both survivors unchanged.
3. Mount its HDD claim read-only in a bounded helper on the source node. Capture
   a **fresh** cold archive of the Raft directory off-node, preserving file
   ownership and modes and recording hashes. Do not reuse the earlier archive
   from before the first node's conversion.
4. Remove the helper and release only master-2's PVC name, with UID/resourceVersion
   guards. Keep the HDD PV and files. Create the same claim name,
   `seaweedfs-master-seaweedfs-master-2`, as a 4Gi Filesystem claim on
   `piraeus-ssd-local`.
5. A bounded restore Pod with nodeSelector `ovh-ns1001419` and the ordinary
   control-plane toleration becomes its first consumer. WaitForFirstConsumer
   provisions the volume there. Verify Retain, CSI handle and strict-local PV
   affinity before restoring. Compare every file hash, ownership and mode;
   preserve timestamps, including directory timestamps if extraction changes them.
6. Remove the helper, return the StatefulSet to three and let master-2 mount the
   restored claim. PV affinity keeps it on the first converted node. Preserve
   its name, peer DNS and Raft identity; never run two copies of that identity.
7. Require three Ready masters, leader/peer agreement, preserved cluster identity
   and no regression of the observed allocated volume-ID watermark. Verify an
   owned S3 object's read across the handoff and a fresh write/read afterward.
   Resume operator, Helm and Flux and verify normal reconciliation. Retire the
   HDD source only after acceptance.

The deployed operator exposes a shared master persistence template, not an
ordinal-specific SC selector. The pre-created master-2 claim can differ from
the existing template, as its temporary HDD claim already does. Pod deletion
reuses that claim; **PVC deletion does not preserve the override**. Record its
Piraeus class and retained PV for recovery. A future template-default change
must handle immutable StatefulSet claim templates without rolling unaffected
masters; do not casually change the shared SC during this handoff.

For later master handoffs, use the same cold-copy/identity checks with explicit
member control. The last-ordinal scale trick applies to master-2 only. Do not
shrink the StatefulSet to remove master-0 or master-1.

### Other local consumers

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

#### Remaining services

Forgejo Valkey combines cache and queue state: verify role, healthy replication,
offset catch-up and service cutover, or perform a consistent copy. Do not assume
its files are disposable because the component is called a cache. Inventory
worker WALs, Keeper/data replicas and local application/backup claims similarly.
For a worker with a Piraeus FILE_THIN canary, enumerate all live resources and
snapshots before removing its backing directory. Move LINSTOR/CSI control
services off the outage set and verify metadata persistence and recovery access.

## 3. Rebuild the evacuated node

Before destruction, accept the gates against the current outage set and save
sanitized evidence off-node. Preserve full private machine configuration,
cluster identity and credentials securely. Record the etcd snapshot policy;
the operator accepted no snapshot for the first-node rebuild. That choice does
not replace quorum checks or preservation of application/Raft state.

Use direct `talosctl` access to the node's IP from the maintenance machine.
Follow the [Talos upgrade runbook](talos_cluster_upgrades.md) for image handling;
prepare the accepted DRBD/iSCSI extensions and kernel modules while keeping the
legacy data volume and Piraeus eligibility disabled until cutover.

For a **legacy unsplit control plane only**, the first-node reset used:

```bash
talosctl -e "$target_ip" -n "$target_ip" reset \
  --graceful=true --reboot \
  --wipe-mode=system-disk --system-labels-to-wipe=STATE,EPHEMERAL
```

Check pinned CLI help and the actual volume inventory before use. A node already
using dedicated ETCD/CRI/KUBELET/LOG partitions needs its own explicit wipe-label
selection. Preserve EFI/META as required. Do not use a default reset that wipes
all disks. Do not bootstrap a second cluster.

In maintenance mode, verify the API certificate fingerprint through a trusted
console. Re-read disk serials, sizes and partition/volume status. Require the
data disk to differ from the system disk, be writable, have no active Talos
volume allocation and no remaining legacy UserVolumeConfig. Only then issue a
fingerprint-pinned FAST wipe of the explicitly identified data device. Apply
the preserved-identity machine configuration with the intended system split
through the maintenance API. Do not copy the pilot's fingerprint or by-id path.

Wait for the node to rejoin; verify etcd returns to three healthy members before
any further control-plane disruption. Keep workloads cordoned while accepting
the empty pool. Reconcile the node-specific Terraform state/configuration and
inspect drift without triggering unrelated machine changes.

## 4. Accept the pool and restore members

Repeat the checks that depend on this physical node:

- Correct system partitions and data-disk ownership, satellite registration,
  thin-pool health/capacity, and fresh data/metadata monitoring for this node.
- Disposable Filesystem and Block PVC creation, actual write/read hashes and
  enforced size; correct class, Retain policy and local PV affinity.
- A controlled reboot before application restoration, with disposable data
  readable afterward, healthy pool and all three etcd members healthy again.
- Headroom and absence of disk, memory, network or storage errors.

Reuse the accepted first-node lifecycle/compatibility evidence; do not repeat
the full QEMU matrix or benchmark suite on each SSD node. The first HDD pool
still needs media-specific canary, bounded I/O, discard and reboot acceptance.
Do not claim metadata exhaustion, full production DRBD-pool recovery or live
controller disaster restore were qualified by the first-node checks.

Before uncordoning, verify all legacy local-path classes exclude new allocations
on this node. Strict-local PVCs select their node through the workload's
scheduling constraints and first-consumer placement. Do not leave a healthy
node cordoned as the long-term storage policy.

Restore applications in bounded steps:

1. Restore native CNPG replicas onto target-node Piraeus claims, one cluster at
   a time. Verify actual node, size/SC/Retain/affinity, existing member/claim
   identities, streaming state and replay of an observed primary WAL position.
   Restore the recorded desired count, removing only explicitly temporary
   members after full redundancy returns.
2. Add a separate node-specific Seaweed Piraeus volume group on the converted
   node. Keep `disk=ssd` (or accepted HDD), DC/rack, replication 001, metrics
   and required anti-affinity across old and new volume groups consistent.
   The first `ssd-piraeus` group hard-pins `ovh-ns1001419`; scaling it up without
   changing that placement cannot add a server on the next node.
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
