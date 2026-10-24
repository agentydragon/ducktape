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

All three OVH SSD control planes are now converted and accepted. The HDD
evacuation is the next active lane; refresh its capacity, placements and shared
service gates before choosing a destructive window.

| Node            | Reason / complication                                                                                                                                                                                                                                |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ovh-ns1001419` | First converted SSD control plane. Attic and Study Casino replicas returned on Piraeus; Seaweed passed a bounded native SSD move and separate S3 checks. Master-2 also returned to its 4Gi Piraeus claim.                                            |
| `ovh-ns104952`  | Converted and accepted. Dedicated system volumes, whole-disk SSD pool, reboot/PVC acceptance, Seaweed SSD server and master-1 restoration are complete. PostgreSQL claims on this node now receive the final SSD control plane's evacuated replicas. |
| `ovh-ns104963`  | Converted and accepted. Dedicated system partitions, whole-disk pool, PVC/reboot checks, Attic/Study replicas, Seaweed SSD server and master-0 restoration passed. Terraform source/state are reconciled; the node is uncordoned.                    |

Finish the control planes first by default. Before evacuating the last legacy
SSD volume server on `104963`, accept a replacement server on the converted
`104952`. The current `104963` and `1001419` servers each hold all SSD volumes;
`1001419` alone cannot preserve two copies outside `104963`. Keep the final
legacy server available until that second Piraeus destination is ready.

One HDD worker may accompany one control plane under the paired-roll gates below.
OptiPlex has its own installed [single-disk layout](../optiplex_provisioning.md);
wyrm2 still needs a separate hardware, capacity, local-data and site-dependency
plan. Do not apply this OVH disk recipe to either machine unchanged.

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
| `ovh-ns102453` | HDD volume-1 and active evacuation destination, filer-0, ClickHouse data/Keeper and multiple DB replicas. The temporary masters 1/2 have returned to the converted SSD nodes. Keep this destination available while evacuating `103656`; refresh Raft placement before pairing any later control-plane window.                                   |
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

Current configuration explicitly prepares the three converted SSD nodes. A label
alone does not prepare another pool. Review these changes before the maintenance
window; merge only with the operator's go-ahead:

- Add the target to the explicit converted-node map in
  [Talos configuration](../../terraform/main/ovh-nodes.tf). Preserve the other
  accepted nodes and leave unconverted nodes unchanged. Generate the
  private machine configuration from the same desired source.
- Extend [Piraeus configuration](../../cdk8s/piraeus.py) with an inert, explicit
  per-node satellite selector and the target's actual data-disk by-id path.
  Preserve the existing operator and LINSTOR cluster. SSD nodes reuse their
  accepted pool name and StorageClass; the first HDD pool and class need their
  own reviewed media-specific configuration and acceptance.
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

Give the separate SSD data disk wholly to the existing `piraeus_ssd/data` LVM
thin pool with the accepted 4 GiB metadata, metadata spare and 64 KiB chunk
sizing. Record actual resulting capacity after creation.

Worker system-volume and HDD pool budgets follow in a separate worksheet; do not
copy the SSD disk identities or assume an ETCD volume is required on a worker.

### First HDD worker system-volume worksheet

Preparation snapshot, 2026-10-23: `ovh-ns103656` is the first HDD worker target.
It is drained and cordoned. The operator authorized reformat after waiving all
48 remaining historical source directories. The maintenance worksheet recorded
that no archive stream or source deletion had occurred; the waiver is not a
backup. Refresh the worker and shared-service gates above before the reset.

Both physical disks were verified at 2000398934016 bytes. Talos does not report
the system disk serial, so both system-volume and data-disk selectors use the
verified WWID. The installation path names the system disk by-id:

<!-- prettier-ignore -->
| Role | by-id path | WWID | Bytes |
| --- | --- | --- | ---: |
| System | `/dev/disk/by-id/ata-HGST_HUS726020ALA610_K5H5NGHA` | `naa.5000cca25ed0aa31` | 2000398934016 |
| Data | `/dev/disk/by-id/ata-HGST_HUS726020ALA610_K5H8UMLA` | `naa.5000cca25ed21ab1` | 2000398934016 |

Use these worker Talos volumes on the system disk; workers have no ETCD volume:

<!-- prettier-ignore -->
| Talos volume | Size |
| --- | ---: |
| EPHEMERAL | 32 GiB |
| CRI | 512 GiB |
| KUBELET | 1024 GiB |
| LOG | 32 GiB |

The system volumes total 1600 GiB, leaving about 263 GiB before Talos boot/STATE,
alignment and other geometry overhead. Keep that remainder unallocated; do not
use the system disk for Piraeus. Piraeus will use only the separate whole data
disk after its existing data has been evacuated and the disk has been wiped.

The prepared Terraform layout removes the `drbd-canary` FILE_THIN selector and
keeps `storage.allegedly.works/piraeus-ready` absent. Before resetting the
worker, inspect a machine-readable targeted plan: it must show the intended
system layout for this worker only, no changes to the accepted SSD nodes, and no
`null_resource.install_talos_kimsufi`, OVH rescue or `dd` installer action. Do
not apply it to the serving node. After reimage, verify the volume layout and
data-disk identity before preparing a separate readiness change or selecting the
data disk for Piraeus initialization.

### Second HDD worker identity and accepted layout

`ovh-ns103711` uses the same worker system-volume profile: EPHEMERAL 32 GiB,
CRI 512 GiB, KUBELET 1024 GiB and LOG 32 GiB. Talos inventory on 2026-10-23
verified two 2000398934016-byte HGST disks:

<!-- prettier-ignore -->
| Role | by-id path | WWID |
| --- | --- | --- |
| System | `/dev/disk/by-id/ata-HGST_HUS726020ALA610_K5H61MEA` | `naa.5000cca25ed0d7d6` |
| Data | `/dev/disk/by-id/ata-HGST_HUS726020ALA610_K5H48V6A` | `naa.5000cca25ed00a31` |

The maintenance layout, whole-disk pool, controlled reboot and Terraform
source/live/state agreement passed before admission on 2026-10-24; readiness is
enabled and the node is uncordoned. Do not repeat initial layout/reset or
rescue/dd installation on the serving node. Refresh the shared gates before
further maintenance.

For this node only, the operator authorized discarding remaining Seaweed source
copies after verifying a healthy surviving copy elsewhere, then restoring
replication after reimage. The normal two-copy gate outside the outage set is
waived for this window; no other node may be rolled concurrently. The operator
also accepted a temporary single ClickHouse data replica on `102453` and waived
preservation of the Anki volume. Preserve and move both Grocy configurations via
a final consistent VolSync copy to Piraeus, and preserve Langfuse queue state.
Flush replicated ingesters and check their survivors before retirement.

### Applied and proposed data-disk layouts

Live LVM reporting on 2026-10-22 confirmed that the first node has one whole-device
PV on `/dev/nvme1n1`, VG `piraeus_ssd`, with 4 MiB extents and no free VG extents.
Its thin-data LV is exactly 503517806592 bytes. Other rows are estimates using
the same layout; record the actual extent/alignment result after initialization.

| Node                          | Physical data disk |         Thin-data LV | Thin metadata + spare | Seaweed volume-server PVC |
| ----------------------------- | -----------------: | -------------------: | --------------------: | ------------------------: |
| `ovh-ns1001419`, applied SSD  |         476.94 GiB | 468.94 GiB, measured |             4 + 4 GiB |                     250Gi |
| `ovh-ns104952`, applied SSD   |         419.19 GiB | 411.18 GiB, measured |             4 + 4 GiB |                     250Gi |
| `ovh-ns104963`, applied SSD   |         419.19 GiB | 411.18 GiB, measured |             4 + 4 GiB |                     250Gi |
| `ovh-ns103656`, candidate HDD |        1863.02 GiB |    about 1855.02 GiB |             4 + 4 GiB |   1700Gi, sizing proposal |

The HDD candidate's 2000398934016-byte HGST data disk has serial `K5H8UMLA`;
its separate system disk is `K5H5NGHA`. Keep every converted SSD Seaweed replica
at 250Gi. A common 1700Gi for converted HDD replicas is proposed pending actual
consumer-usage checks and layout review; existing HDD claims request 1800Gi.
Replace each old claim during its native migration rather than trying to shrink
an existing PVC.

These are thin logical volumes inside the shared pool, not fixed Seaweed-only
partitions. Other applications use the pool through their own PVCs. The first
node's four application requests total 263Gi. On the HDD candidate, current
requests are 1800Gi Seaweed plus 90Gi for other local claims: 1890Gi exceeds the
estimated 1855Gi pool before DRBD overhead. A proposed 1700Gi Seaweed PVC plus
those 90Gi of other claims totals 1790Gi of logical capacity, 65Gi below the
estimate. That difference is not 65Gi of reserved physical free space. Measure
actual per-consumer usage: legacy local-path requests were not enforced limits.

### First HDD pool initialization retry

The first `ovh-ns103656` pool-creation failure was reported after about 61 seconds.
Its 45-second wait plus 15-second termination is inferred; the retry obscured the
original command failure. A later direct thin-pool `lvcreate` completed in 46.5
seconds, supporting an external-command timeout during 4 GiB metadata and spare
initialization as the leading explanation, though the original failed command is
unknown. The target satellite now sets `ExtCmd/WaitTimeout` to 180000 ms.

Before removing partial LVM state or retrying, verify that LINSTOR reports zero
resources on this satellite and zero cluster snapshots. Enable `piraeus-ready`
only after the physical data-disk and system-volume ownership gates pass; then
keep the node cordoned and prevent application PVCs until repair and media-specific
pool acceptance are complete.

### Accepted thin-pool overcommit policy

Approved policy (2026-10-22): aggregate logical PVC capacity may exceed the
physical thin-data pool, provided each PVC's declared size is bounded and actual
pool use and headroom are monitored. Logical capacity is not a physical
reservation, and this policy does not promise that all PVCs can be filled at
once. Treat any such overcommit as temporary capacity debt to resolve through
workload placement and balancing; do not assume extra disks or indefinite
overcommit. On 2026-10-24, `ovh-ns103711` has six Bound `piraeus-hdd-local`
claims: 1700Gi SeaweedFS, 500Gi ClickHouse, two 2Gi Valkey replicas and two 10Gi
monitoring claims, totaling 2224Gi against a measured 1855.015625GiB thin-data
pool. The 368.984375GiB excess (19.89%), before reserve and additional volume
overhead, is tracked in
[#11786](https://github.com/agentydragon/ducktape/issues/11786).

Enforce the planned per-PVC sizes. Monitor actual thin-data and thin-metadata
utilization independently of PVC capacity, with fresh samples and alerts for
each pool. Before evacuation or node maintenance, verify both metrics and the
destination's actual free-space reserve; pause new allocation or migration if
samples are stale, the pool is unhealthy, or available headroom falls below
the approved reserve. PVC totals and volume-slot limits do not establish
physical headroom.

When a live pool's aggregate provisioned PVC capacities exceed its
physical thin-data capacity, open a dedicated Ducktape GitHub issue and add
the issue link here. Record physical capacity, total provisioned PVC capacities,
actual data and metadata use, remaining headroom and alert state, contributing
claims (including ClickHouse), and concrete mitigation or growth options. Track
an exit plan such as rebalancing volumes or moving suitable workloads to SSD.
Close the issue only after placement or size changes bring aggregate provisioned
PVC capacities within the usable physical-pool budget, accounting for agreed
reserve and overhead, and fresh data, metadata, and free-space measurements
confirm the agreed headroom. Low current pool use alone does not resolve
overcommit. If the owner explicitly accepts a revised target, record that
target and its rationale in the issue before closing.

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
threshold**, before copy overhead and new writes. A later refresh projected
233.62GB free (11.7%), 66.29GB below the 15% reserve; that copy wave remained
held. The native ClickHouse-to-SSD proposal and current copy progress are in
[the evacuation evidence](../../debug/piraeus_migration/next_nodes/README.md#hdd-headroom-proposal).
Refresh actual copy sizes, other local usage, growth and headroom before declaring
the space gate passed. Do not infer adequate headroom from the enlarged slot limit.

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

1. For each of the seven SSD evacuation clusters listed in
   [CNPG conventions](../cnpg_conventions.md#current-compliance), work serially:
   finish and verify one before starting the next. Record its desired count
   (two), each Pod/PVC UID, size, StorageClass and primary. The prepared Git
   template keeps two instances, sizes the replacement for measured PGDATA
   plus headroom, uses `piraeus-ssd-local` for a new claim, and disables resize-in-use. Existing
   claims keep their StorageClass and size.
   Measure actual PGDATA, including WAL: legacy local-path requests were not
   enforced limits. Stop before requesting a replacement that cannot hold the
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
5. Restore the Cluster's desired count to two before removing its Flux hold.
   Verify Flux and CNPG agree on two members, then proceed to the next cluster.
   Repeat until all seven have been evacuated.
6. Attic and Study Casino are already at three members and are outside this
   seven-cluster procedure. Move a target primary first and retain two healthy
   off-target members. Study Casino stays **1Gi**.

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

Refresh long-lived FUSE clients while the old endpoint still exists, then check
that requests no longer depend on it. The [retirement caveat](rolling_seaweedfs_volume_pvc.md#caveat-retiring-a-volume-server-refresh-fuse-clients-before-you-delete-it)
describes the cached-location failure. Retire only the evacuated ordinal:
`ssd-1` on `ovh-ns104952` permits old-group replicas 2 to 1; a later empty
`ssd-0` permits removal of the remaining old group. The HDD group needs the same
ordinal check. Do not treat a generic PVC-roll drain as proof of evacuation.

### Retiring HDD ordinal 0 while preserving ordinal 1

For the approved `103711` single-copy window, preserve the legacy HDD peer
`seaweedfs-volume-hdd-1` on `102453`. A plain replicas 2 to 1 removes ordinal 1,
which is the peer that must survive. The pinned operator has no per-CR pause or
ordinal-start field and would reconcile the old group's replica count back.

First remove only `hdd` from the Seaweed CR's desired topology map. Operator
0.1.42 iterates configured groups and leaves omitted groups' existing resources
in place; verify the deployed operator version before relying on this. Wait for
the CR to observe the new generation and verify the old StatefulSet, both Pod
UIDs, services and claims remain unchanged. The old StatefulSet temporarily
holds the legacy group's desired state outside topology reconciliation.

Refresh every source volume's off-node survivor and the user's scoped waiver,
plus masters, filer/database, S3 and affected FUSE client health. Under exact
StatefulSet UID/resource-version guards, atomically set `ordinals.start: 1` and
`replicas: 1`; retain both PVCs and verify only ordinal 0 stops. Require the
original ordinal 1 Pod/claim identities, native server registration, surviving
volume placement and application reads to remain healthy. Never create ordinal
2 or delete the peer's PVC as part of this transition.

Keep this explicit legacy exception until `102453` is migrated. After `103711`
pool acceptance, add its own readiness-gated 1700Gi Piraeus HDD topology and
restore replication before touching the remaining peer. When that peer is
replaced, retire the legacy StatefulSet, services/monitor and obsolete claims
under their own acceptance gates. Do not re-add `hdd` while its ordinal-start
exception remains, and do not treat retained metadata as preservation after a
physical disk wipe.

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

#### Master-1: preserve the other ordinals

The second SSD evacuation exercised this procedure with master-1 moving from
`104952` to a fresh retained 4Gi HDD claim on `102453`:

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

Use the node's verified public IP directly with `talosctl` for maintenance.
Record its first maintenance certificate and pin subsequent calls with
`--cert-fingerprint`; a trusted console can independently verify that fingerprint
when available. Recording the first certificate trusts that initial connection;
pinning alone is not an independent identity check. Re-read disk serials, sizes
and partition/volume status. Require the
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
   `cnpg destroy --keep-pvc` leaves the detached PVC discoverable through its
   CNPG labels. Scaling up can reuse that local-path PVC even when the new-claim
   template selects Piraeus. Under bounded Flux/CNPG holds, verify the exact
   retired PVC/PV identities, no remaining consumers, Retain policy and accepted
   data-preservation source. Remove only that retired PVC's metadata before
   requesting the fresh claim; preserve its PV and files/archive. Then let CNPG
   create the new Piraeus claim and prove its class, affinity and native replay.
2. Add a separate node-specific Seaweed Piraeus volume group on the converted
   node. Keep `disk=ssd` (or accepted HDD), DC/rack, replication 001, metrics
   and required anti-affinity across old and new volume groups consistent.
   The first `ssd-piraeus` group hard-pins `ovh-ns1001419`; scaling it up without
   changing that placement cannot add a server on the next node.
   `ssd-piraeus-104952` requests a fresh 250Gi claim and requires both hostname
   `ovh-ns104952` and `storage.allegedly.works/piraeus-ready=true`. It remains
   unscheduled until that node passes pool admission and is uncordoned. Its
   addition preserves the running legacy and first Piraeus server templates.
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
