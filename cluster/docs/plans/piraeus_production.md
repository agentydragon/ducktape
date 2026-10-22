# Piraeus production rollout

Proposed work for [storage issue #4717](https://github.com/agentydragon/ducktape/issues/4717),
based on the [2026-10-19 read-only inventory](../../debug/piraeus_inventory_2026_10_19/README.md).
This PR changes documentation and evidence only. Rollout, disk conversion and
application migration require subsequent scoped changes. Remove completed work
from this plan as it lands.

## Handoff: deferred, 2026-10-19

**Production rollout deferred; disk layout not approved; migration not started.**
The occupied-disk conversion is a larger migration than the user wants to
undertake now. Resume by finding a smaller, less disruptive path, or revisit the
design after further consideration. The VG/LV sizes and XFS choice below remain
proposals, not settled decisions or an execution runbook.

Preserve the Talos-first scope: NixOS support is a separate later rollout.
The requested SSD/HDD and one-/two-copy variants, thin provisioning, Block and
Filesystem volumes, and automatic locality remain the desired behavior.

The [inventory](../../debug/piraeus_inventory_2026_10_19/README.md),
[FILE_THIN canary](../../debug/drbd_canary/results.md),
[automatic-movement results](../../debug/drbd_canary/auto_diskful/results.md), and
[storage benchmarks](../../debug/piraeus_storage_bench/README.md) preserve the
evidence. LVM_THIN runtime behavior and Talos/LINSTOR shared-VG ownership are
unvalidated; offline YAML validation does not establish either.

On resumption:

- First answer: **can we obtain useful production storage incrementally without
  rebuilding occupied data disks?** Compare candidate paths by required capacity,
  application downtime, production support and recovery behavior.
- If disk conversion remains necessary, reconsider allocation sizes and the
  legacy filesystem before committing to them. Growing from VG reserve is easier
  than reclaiming space from XFS or an existing thin pool. Ext4's offline shrink
  capability is a reason to evaluate it, not a tested Talos procedure here.
- Refresh disk identities, usage, PVC consumers and backup/restore evidence
  before any allocation decision. The inventory is timestamped, not live state.
- Review the selected design and its disposable-storage acceptance results before
  proposing a per-node conversion. No production conversion or workload migration
  follows from this documentation PR alone.

## Target behavior

Extend the existing Flux-owned operator and LINSTOR cluster, with CSI/satellites
on every prepared Talos and NixOS node. Pool membership follows actual media;
every node need not supply both tiers. Preserve the same class names across
node types and permit remote consumption on prepared nodes, subject to the
explicit site policy below.

Deploy on **Talos first**. Talos host preparation, pools, recovery acceptance and
initial production use must not depend on NixOS support, Atlas capacity or laptop
availability. Keep NixOS nodes outside Piraeus satellite/CSI eligibility and
Piraeus-consuming workload placement until their separate later rollout passes
acceptance. That rollout joins the same operator, LINSTOR cluster and storage
classes; it does not install a second provider or require migrating existing
Talos volumes.

| Proposed class  | Pool  | Steady-state diskful copies |
| --------------- | ----- | --------------------------: |
| `piraeus-ssd-1` | `ssd` |                           1 |
| `piraeus-ssd-2` | `ssd` |   2, synchronous protocol C |
| `piraeus-hdd-1` | `hdd` |                           1 |
| `piraeus-hdd-2` | `hdd` |   2, synchronous protocol C |

All four should support raw Block and Filesystem, thin allocation, expansion,
WaitForFirstConsumer, and Retain. Leave the cluster default unchanged. The
locality goal is auto-diskful with cleanup: one copy follows a one-copy consumer;
two-copy movement temporarily adds a third copy then returns to two. Local
copying requires a compatible pool and space on the consumer node.

The design below proposes LVM_THIN for production. Our accepted performance and movement
tests used FILE_THIN; they do not validate LVM_THIN or its failure behavior.
[LINBIT describes File/FileThin as mainly demonstration/experiment backends](https://linbit.com/drbd-user-guide/linstor-guide-1_0-en/).
Keep the existing file-backed canary separate during transition, and do not
silently mix backend types or reuse its discard tuning without new measurements.

### Scheduling locality and network budget

Scheduling goal added 2026-10-21: **prefer nodes that already hold a diskful
copy of a workload's volume, while keeping remote-capable nodes as fallback**.
This may reduce ordinary I/O over the Talos inter-node links and avoid triggering
a copy move just because a pod restarted elsewhere. The link bandwidth has not
been measured here.

Piraeus' `allowRemoteVolumeAccess` controls which nodes may attach a volume; it
does not express a soft preference for nodes that hold a copy. With remote
access disabled, Piraeus uses strict PV node affinity, so placement is a hard
constraint; the optional LINSTOR Affinity Controller keeps that affinity current
when diskful copies move. With remote access enabled, a pod may attach disklessly
to a remote copy and the auto-diskful policy can make its node diskful after the
configured delay. The canary demonstrated this follow-the-pod behavior on two
Talos nodes with FILE_THIN.

Upstream already provides a separate `linstor-scheduler` Helm chart and
scheduler extender. It uses LINSTOR placement information to prefer nodes with
local data, and workloads opt in by setting `schedulerName: linstor`. It is not
installed by the Piraeus Operator or declared in this repo. This is distinct
from the LINSTOR Affinity Controller above: that controller keeps PV node
affinity synchronized with placement, which is a hard constraint rather than a
soft preference. See the [scheduler chart release notes](https://github.com/piraeusdatastore/helm-charts/releases)
and [scheduler extender README](https://github.com/piraeusdatastore/linstor-scheduler-extender).

Do not deploy the upstream scheduler image unchanged yet. Its source currently
pins Kubernetes scheduler libraries at v1.25.6, while this cluster defaults to
Kubernetes v1.35.1; Kubernetes supports a kube-scheduler matching the API
server or one minor version older. A supported build/version is therefore the
main compatibility question. It also needs LINSTOR API client credentials: this
cluster exposes only the mTLS API on port 3371, while the upstream example uses
a plain-HTTP in-cluster endpoint. Start with explicit
`schedulerName: linstor` on a canary workload; the optional admission webhook
mutates pods cluster-wide and is not needed for a first trial.

Planning estimate (2026-10-21): reserve 1–2 engineering days for a compatibility
spike. If the scheduler can be rebuilt cleanly against the cluster's Kubernetes
version, expect another 4–8 days for declarative deployment, mTLS wiring,
opt-in workload integration, and validation of local preference and remote
fallback. If upgrading the old scheduler fork requires substantial API work,
budget 1–3 weeks total. The design must also handle pods with multiple PVCs
whose copies are on different nodes and tolerate copy placement changing while
a pod waits to be scheduled.

Before choosing that mechanism or its score, measure Nebula latency and sustained
throughput between eligible Talos nodes, including during DRBD resync with
application I/O and shared-disk contention. Existing movement tests used a
configured `32M` resync rate on small shared-HDD canaries; they do not establish
available link bandwidth or production resync times. Use the measurements to set
the locality preference and resync limits, then validate remote fallback and
local-copy behavior together.

### Storage and replication metrics

LINSTOR exposes Prometheus metrics for configured storage-pool capacity and
cluster resources/nodes. Scraping pool free/used capacity over time will show
consumption and growth rate; pool/resource counts can show how many storage
targets and diskful copies LINSTOR manages. Physical drive inventory/health and
LVM thin-pool data/metadata headroom still need node-level metrics. DRBD Reactor
exports per-peer bytes sent/received, bytes currently out of sync, and TCP
congestion state. Pair those with per-interface Nebula traffic metrics to see
actual link throughput during resync and ordinary I/O. See the [LINSTOR monitoring
guide](https://linbit.com/drbd-user-guide/linstor-guide-1_0-en/) and [DRBD Reactor
metric list](https://github.com/LINBIT/drbd-reactor/blob/master/doc/prometheus.md).

The current canary deliberately keeps the unauthenticated DRBD Reactor endpoint
on localhost and disables monitoring; it is not scraped. The LINSTOR controller
also has no ServiceMonitor/PodMonitor in this repo. Before relying on dashboards
or alerts, expose and scrape the controller and satellite metrics through the
existing monitoring stack while preserving the controller API TLS/network policy
and restricting access to the host-networked Reactor endpoint. Confirm the
per-peer byte metric behavior during a real resync before charting it as a
transfer rate.

## Proposed allocation and order

For OVH, replace the evacuated whole-device XFS data filesystem with one
whole-device LVM physical volume and VG, named `data_ssd` or `data_hdd`:

```text
Install disk: existing Talos partitions and /var, unchanged
Data disk: LVM PV -> data_ssd or data_hdd
  localpath      ordinary, fully allocated LV -> XFS -> existing local-path mount
  piraeus        thin pool -> LINSTOR-managed thin LVs and DRBD volumes
  free extents   explicit growth reserve for either consumer
```

There is no separate legacy filesystem partition or nested VG. The `localpath`
LV holds existing local-path PVC directories (SeaweedFS data, databases and other
consumers), not Piraeus files. Keep its current mount name for the first conversion:
`seaweedfs-data` on SSD, `local-path-ovh-hdd` on HDD. Names in new LVM objects are
provider-neutral. Any mount-path rename is a separate coordinated change.

The `localpath` LV is a migration bridge, not a permanent Piraeus requirement.
Keeping it for the first cut lets local-path consumers remain on their existing
provisioner while applications move in later changes. It does not preserve their
bytes through the initial device conversion: each node's consumers still need to
be evacuated before reformatting the data disk, then restored or rebuilt onto the
new storage. Omitting the LV is also valid, but requires moving or retiring every
local-path consumer on that node as part of its first conversion, including any
Seaweed volume server. Size a temporary LV from measured use and the workloads
that must remain there, not the sum of PVC requests; keep unallocated VG extents
for later growth.

XFS retains the current filesystem and project-quota capability; it is not a
Piraeus requirement. Its inability to shrink means moving capacity from this LV
to the thin pool later still requires evacuation/recreation or removing the LV
after its consumers migrate. LVM makes unallocated space reusable without changing
partition boundaries; it does not make XFS shrinkable. Ext4 is an alternative if
offline shrinking becomes a requirement, with a separately tested maintenance path.

### SeaweedFS and changing the capacity split

Keep SeaweedFS and Piraeus as separate storage layers for the initial rollout.
The current Seaweed volume-server PVCs are node-local `local-path` claims (up to
1800 GiB requested on HDD nodes); Seaweed's `defaultReplication: "001"` supplies
its own second copy. Seaweed already has `weed shell`'s `volume.move` operation
to move an individual Seaweed volume between volume servers, so it can drain a
server by moving only selected volumes when that is sufficient; `volumeServer.evacuate`
can drain all data from a server. These operations follow Seaweed's own placement
and replication model ([SeaweedFS shell command reference](https://github.com/seaweedfs/seaweedfs/wiki/weed-shell)).
Putting each volume-server directory on Piraeus is therefore optional, not a
prerequisite for moving Seaweed data. The existing S3 and Seaweed CSI services
remain in use.

If Piraeus becomes the preferred provisioner for new node-local application
PVCs, make that an explicit choice of new Piraeus StorageClasses on prepared
nodes. Keep the current SeaweedFS class as the app-data default and migrate
existing local-path claims individually after data and restore checks; changing
a StorageClass does not move already-bound PVs.

A one-copy Piraeus class for a Seaweed volume-server PVC is a reasonable later
pilot if relocating the whole server filesystem is useful. It would be a thin
LINSTOR volume backed by one diskful replica on one node's LVM thin pool, not a
separate physical disk and not another Seaweed data replica. Piraeus could move
that block volume to another node by copying it over the network; moving it can
transfer the whole server filesystem, and does not by itself make Kubernetes
prefer the node with its diskful copy. Seaweed's own replication remains
responsible for surviving loss of the server's disk. Avoid adding a second
Piraeus copy by default: that would duplicate the Seaweed data server filesystem
as well as Seaweed's per-volume replication. Keep the current volume-server PVCs
on `local-path` until an opt-in Piraeus pilot measures resync traffic,
interruption behavior, and failure recovery.

### Existing local-path workload candidates

The 2026-10-19 PVC inventory is a dated snapshot, not a current migration list.
For larger node-local workloads, the desired access policy is a dedicated
single-copy class with `placementCount: 1` and
`linstor.csi.linbit.com/allowRemoteVolumeAccess: "false"`. Because remote-access
permission is a StorageClass parameter, remote-capable and strict-local claims
need separate classes. A strict-local PV's node affinity keeps its pod on the
node with its diskful volume and avoids remote block I/O. With one diskful copy,
a node failure loses Piraeus' live copy; recovery must come from application
replication or backup. Relocating a healthy volume is a separate copy operation
over the network. For databases with application replicas, use one strict-local
Piraeus volume per member and rely on the database's own replication; do not add
a second Piraeus copy by default. Schedule and rate-limit deliberate relocations.

| Workload                 | Snapshot from the inventory                                                                                                     | Migration shape and data-preservation gate                                                                                                                                                                                                                                                                                                         |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Home Assistant config    | 32 GiB on `optiplex`, `local-path-home-ssd`; hardware-bound to that host.                                                       | Existing VolSync Restic backups go to SeaweedFS, and Home Assistant has its own backup integration. It is a reasonable restore-to-new-PVC candidate after restore validation, but the proposed OVH pools cannot host this node-pinned workload. Add a dedicated Piraeus pool on OptiPlex first; do not use its Talos `/var` as an improvised pool. |
| Grocy                    | Two 1 GiB household config claims on `ovh-ns103711`, with VolSync backup PVCs on SeaweedFS.                                     | Good first app-data migration candidate: provision a new strict-local PVC, stop/quiesce Grocy for a final copy, restore, run SQLite `PRAGMA integrity_check`, and verify the app before cutover. Existing direct-rsync backups are crash-consistent and can occasionally catch torn SQLite writes, so validate the selected restore point.         |
| Haku Anki collection     | 2 GiB on `ovh-ns103711`.                                                                                                        | The owner has said this collection is disposable. A future migration can recreate it empty; that statement is not an instruction to delete the live PVC now.                                                                                                                                                                                       |
| Mimir ingesters          | Two 10 GiB claims, one each on `ovh-ns102453` and `ovh-ns103711`; RF=2, with blocks in SeaweedFS S3.                            | These PVCs hold ingester WAL and recent head data, not merely disposable cache. Migrate one member at a time with Mimir's drain/flush path, verify the survivor and recent queries, then confirm the resulting blocks in S3. A fresh empty claim is only appropriate with an explicitly accepted recent-data loss window.                          |
| Loki write               | Two 10 GiB claims on separate OVH HDD nodes; write replicas use RF=2 and chunks/index live in SeaweedFS S3.                     | The PVCs hold ingester WAL, so treat them like recent write state. Drain and flush one writer at a time and verify queries before proceeding. Loki backend cache is already `emptyDir`; it does not need a Piraeus PVC.                                                                                                                            |
| ActivityWatch            | 10 GiB SQLite database on `wyrm2` via `local-path-proxmox`.                                                                     | Not in the initial Talos rollout; Wyrm2/NixOS is a later Piraeus phase. Device importers can rebuild only history still retained on those devices. Preserve and restore the database unless completeness of that source history has been checked.                                                                                                  |
| ClickHouse               | Two 500 GiB retained data claims on separate HDD nodes; one shard with two ClickHouse replicas, plus three 2 GiB Keeper claims. | Large, non-cache state. If moved, give each ClickHouse member one strict-local Piraeus volume and use ClickHouse replication to rebuild/validate one member at a time. Keep Keeper quorum healthy during the move and confirm queries before advancing; avoid duplicating ClickHouse's replica with Piraeus replication by default.                |
| PostgreSQL/CNPG replicas | Multiple per-instance claims across local-path SSD/HDD classes.                                                                 | Treat each database separately because backup configuration and recovery paths vary. A plausible target is one strict-local Piraeus volume per CNPG member, with controlled switchover/rebuild and each cluster's configured backup/WAL recovery gate verified first.                                                                              |
| Valkey replicas          | Multiple small per-member claims; the inventory includes Forgejo, Langfuse, and OCI-cache instances.                            | Decide per instance whether its contents are rebuildable cache or meaningful persisted state. For replicated instances, use one strict-local Piraeus volume per member and validate application replication or restore; do not assume that two Valkey pods are a backup.                                                                           |

These classifications come from the [dated PVC inventory](../../debug/piraeus_inventory_2026_10_19/pv-consumers.tsv)
and current checked-in workload definitions, including [Mimir](../../cdk8s/monitoring/mimir.py),
[Loki](../../cdk8s/monitoring/loki.py), [ActivityWatch](../../cdk8s/activitywatch/app.py),
[Grocy](../../cdk8s/grocy/app.py), [Home Assistant](../../cdk8s/home_assistant/app.py),
and [ClickHouse](../../cdk8s/clickhouse/installation.py). Refresh live placement,
actual bytes used, backup restoreability and app replication health before turning
any candidate into a migration sequence.

The local-path PVC requests are not disk quotas or reservations: they are
directories sharing the node's XFS filesystem. In particular, an 1800 GiB
Seaweed claim does not mean 1800 GiB is occupied, and the declared local-path
requests can exceed the physical filesystem. Base any future capacity split on
measured bytes, growth and restore headroom, Seaweed's replica placement, and
each node's other consumers, rather than adding PVC request sizes.

Moving application data from SeaweedFS to Piraeus and freeing one node's
Seaweed-backed disk are separate steps. The first reduces Seaweed's live dataset
only after cutover and cleanup; it does not resize the Seaweed server PVC or the
XFS/localpath LV. Seaweed `volume.move` or `volumeServer.evacuate` can relocate
server-side data to peer servers and clear a node, but does not reduce the
cluster-wide Seaweed dataset.

Treat capacity transfer as a staged node maintenance operation, not a PVC
shrink:

- Kubernetes does not support shrinking a bound PVC ([Persistent Volumes
  documentation](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#expanding-persistent-volumes-claims)).
  LINSTOR can shrink a volume definition when its storage layers support it, but
  only after shrinking the filesystem safely ([LINSTOR User Guide](https://linbit.com/drbd-user-guide/linstor-guide-1_0-en/));
  that lower-level capability does not make CSI PVC shrink a supported
  Kubernetes workflow. Migrate data to a newly sized PVC when the requested
  Kubernetes capacity needs to go down.
- LVM thin pools can be extended but not reduced in size. Reclaiming blocks
  inside a thin pool can make them reusable by its thin volumes when discard is
  supported and propagated; it does not release the pool's physical VG extents
  to the `localpath` LV. Do not size the initial pool on the assumption that it
  can later be shrunk ([LVM thin-pool administration](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/7/html/logical_volume_manager_administration/lv)).
- For this proposed VG layout, start with a conservative thin pool and leave
  free VG extents as the flexible reserve. Grow the pool from that reserve as
  Piraeus demand is demonstrated. To move more disk from the legacy XFS LV into
  Piraeus, first evacuate all of that node's local-path consumers. Seaweed data
  can be moved or evacuated with Seaweed's own placement/replication tools;
  other state needs its own migration and restore gate. If no local-path users
  remain, unmount and remove the whole `localpath` LV, then extend the Piraeus
  thin pool from the extents returned to the VG. This later capacity handoff
  does not reformat the physical disk or recreate the PV/VG; only the first
  conversion from today's whole-device XFS filesystem to LVM requires evacuating
  data and recreating the disk layout. If some local-path use remains, XFS cannot
  be shrunk: evacuate it, remove the old LV, create a smaller XFS LV, and restore
  the remaining users. That partial handoff needs temporary capacity or an
  offline maintenance window. Thin-pool growth is one-way, so retain some free
  VG extents unless the larger pool size has been accepted.

If retained, the proposed Talos `localpath` LV remains XFS so current project-quota
behavior is preserved. Choosing ext4 could enable an offline filesystem shrink,
but would add an untested filesystem and maintenance procedure; it still would
not enable shrinking a Piraeus PVC or its LVM thin pool.

All sizes below are **GiB**, including the converted physical device capacities.
Thin-data sizes remain starting acceptance budgets, not final workload sizing or
a promise that Forgejo's current 200 GiB claim fits. Reserve 1 GiB thin metadata
and 1 GiB metadata spare initially; free extents are approximate after that allowance
and before small VG overhead. Recheck actual geometry during allocation.
The `localpath` column is the staged-conversion option; if a node can retire all
local-path users in its first cut, those proposed extents stay as VG reserve until
thin-pool growth is justified. They are not an automatic increase to the pool.

| Talos hardware / nodes                     | Data disk GiB |   `localpath` LV GiB | `piraeus` thin data GiB |  Approximate free VG GiB | Decision                                                                         |
| ------------------------------------------ | ------------: | -------------------: | ----------------------: | -----------------------: | -------------------------------------------------------------------------------- |
| 450 GB SSD: `ovh-ns104952`, `ovh-ns104963` |         419.2 |                  320 |                      64 |                       33 | Growth-budget gate: existing requests total 311/316 GiB.                         |
| 512 GB SSD: `ovh-ns1001419`                |         476.9 |                  352 |                      64 |                       59 | First SSD conversion; smallest observed data footprint.                          |
| 4 TB HDD: `ovh-ns102453`                   |        3726.0 |                 2816 |                     128 |                      780 | First HDD conversion; existing requests total 2536 GiB.                          |
| 2 TB HDD: `ovh-ns103656`, `ovh-ns103711`   |        1863.0 |    1536, conditional |        128, conditional |                      197 | Illustrative target only; do not convert until legacy capacity/growth is agreed. |
| OptiPlex: single 256 GB system NVMe        |         238.5 | Keep existing `/var` |    None on current disk | None outside filesystems | Enroll as client first; add a dedicated SSD for a local pool.                    |

The 2 TB example would reduce the legacy filesystem below requests of 1890/2368
GiB, including an 1800 GiB SeaweedFS claim on `103711`. Measured use is lower, but
that alone does not justify reducing capacity. Keep both nodes' current disks
unchanged until per-consumer growth limits, migrations or additional capacity
resolve this. Likewise, the 450 GB SSD legacy margins need an explicit growth
check, not merely a successful restore today. Do not force any example size.

Keep at least 32 GiB of free VG extents on SSD and 64 GiB on HDD at initial
allocation, in addition to thin metadata/spare. This replaces the earlier dedicated
Piraeus-region percentage budget: the VG now also contains the large legacy LV.
Thin-pool extension and legacy-LV growth both spend that shared reserve and need
one capacity policy; do not let independent automation race to consume it.

### Talos configuration shape

Example for `ovh-ns1001419`, after evacuation and an explicitly reviewed wipe.
These documents replace its existing whole-disk `UserVolumeConfig`; they are not
an in-place conversion patch. Talos 1.14 documents both
[VG/LV provisioning](https://docs.siderolabs.com/talos/v1.14/configure-your-talos-cluster/storage-and-disk-management/lvm)
and [user volumes on LVs](https://docs.siderolabs.com/talos/v1.14/configure-your-talos-cluster/storage-and-disk-management/disk-management/user).
The current Terraform configuration-generation default is still Talos 1.12.3,
despite these nodes running 1.14.0. Advance the affected nodes' generation target
and verify provider support for these kinds before rendering the real machine
configs; preserve the separate durable machine-secrets version contract.

```yaml
apiVersion: v1alpha1
kind: LVMVolumeGroupConfig
name: data_ssd
provisioning:
  volumeSelector:
    match: disk.serial == '1951A3449505'
---
apiVersion: v1alpha1
kind: LVMLogicalVolumeConfig
name: localpath
type: linear
provisioning:
  volumeGroup: data_ssd
  minSize: 352GiB
  maxSize: 352GiB
---
apiVersion: v1alpha1
kind: UserVolumeConfig
name: seaweedfs-data
volumeType: disk
provisioning:
  diskSelector:
    match: "'/dev/mapper/data_ssd-localpath' in disk.symlinks"
filesystem:
  type: xfs
  projectQuotaSupport: true
```

Use the same documents with these substitutions. The selected serial is always
the inventory's data disk, never the install disk. Verify it again before wiping.

| Node            | Data-disk serial     | VG         |     Localpath GiB | User-volume / mount suffix |
| --------------- | -------------------- | ---------- | ----------------: | -------------------------- |
| `ovh-ns1001419` | `1951A3449505`       | `data_ssd` |               352 | `seaweedfs-data`           |
| `ovh-ns104952`  | `BTPF8304019P450RGN` | `data_ssd` |               320 | `seaweedfs-data`           |
| `ovh-ns104963`  | `BTPF8256009U450RGN` | `data_ssd` |               320 | `seaweedfs-data`           |
| `ovh-ns102453`  | `V3H370PN`           | `data_hdd` |              2816 | `local-path-ovh-hdd`       |
| `ovh-ns103656`  | `K5H8UMLA`           | `data_hdd` | 1536, conditional | `local-path-ovh-hdd`       |
| `ovh-ns103711`  | `K5H48V6A`           | `data_hdd` | 1536, conditional | `local-path-ovh-hdd`       |

All six substituted document sets passed `talosctl` 1.14.2 offline validation
with `--mode metal --strict`. This checks syntax/config constraints, not runtime
device matching, provisioning or Talos/LINSTOR interoperability.

Talos owns PV/VG membership, activation, the ordinary `localpath` LV and its
filesystem/mount. Creating and growing `data_ssd/piraeus` or `data_hdd/piraeus`
with the initial thin-data/metadata budgets needs a separate supported lifecycle.
Talos's `LVMLogicalVolumeConfig` supports ordinary/RAID LVs, not thin-pool
creation; do not declare the thin pool as a linear LV. LINSTOR owns the thin
volumes inside that pool. Prove shared-VG discovery, locking, reboot ordering
and reconciliation on disposable storage before using this division of ownership.

Retiring `localpath` is not a single machine-config deletion. First stop or
migrate every host-path consumer, then remove its `UserVolumeConfig` so Talos
unmounts the user volume and drops its mount/volume status. Remove the
`LVMLogicalVolumeConfig` so Talos stops reconciling the LV, then explicitly run
`talosctl wipe lv data_hdd/localpath` (or the matching SSD VG/LV) to delete the
on-disk LV and release its extents. Removing the user-volume config alone leaves
the LV data intact. `talosconfig` supplies `talosctl`'s client context and
credentials; the machine-config declarations themselves remain owned by the
checked-in Terraform source and must be removed there to prevent re-creation.
The LV wipe leaves the PV and VG intact. Extending the LINSTOR thin pool from
the freed extents is a separate operation.

#### Growing the prepared thin pool

At the LVM layer, `lvextend` grows an existing thin pool by allocating VG free
extents to its data LV; LVM also extends its metadata LV when needed. Check VG
free space before the change and retain the agreed reserve; do not consume all
free extents by default. This changes the backing capacity available for thin
volumes. It does not change any LINSTOR volume definition or Kubernetes PVC
request. Each node has its own VG and thin pool, so growing one adds placement
capacity on that node only; repeat for other nodes as their own capacity needs
justify it.

Talos 1.14 can declaratively grow ordinary LVs by raising `maxSize`, but its
`LVMLogicalVolumeConfig` supports only `linear`, `raid0`, `raid1`, and `raid10`
types. Its `LVMService` RPCs remove LVs/VGs/PVs; it does not expose an operation
to extend a thin pool. `talosconfig` therefore cannot request this resize through
`talosctl`. The proposed Piraeus `lvmThinPool` entry registers the named pool if
it already exists; it has no desired-size field in this mode. The current design
has no supported, declarative owner for thin-pool creation or growth on Talos.
Before production conversion, either add a Talos-supported thin-pool lifecycle
or choose a different storage ownership/layout that has one. Do not treat a
manual host command or an unreviewed privileged Kubernetes workload as the
operational solution.

After extending, verify the new thin-pool size and inspect data and metadata
usage through a supported status/metrics surface; then verify LINSTOR reports the
new storage-pool capacity and remains healthy without re-registering the pool.
The LVM docs warn to extend data and metadata before either reaches 100%. Prove
the operation with an existing LINSTOR resource in the canary, including its
availability during the change and the time LINSTOR takes to observe the updated
capacity. Do not enable automatic extension until Talos ownership, metadata/data
alerts, and protection of the shared VG reserve have been demonstrated together.

#### What upstream documents for Talos

The [Piraeus Talos guide](https://piraeus.io/docs/v2/how-to/talos/) is a guide to
loading DRBD and adapting the Satellite deployment to Talos. It calls out the
`dm-thin-pool` kernel module for LVM_THIN, but does not describe a Talos-native
PV/VG/thin-pool creation or resize lifecycle. The
[Talos 1.14 LVM guide](https://docs.siderolabs.com/talos/v1.14/configure-your-talos-cluster/storage-and-disk-management/lvm)
documents PVs, VGs and ordinary logical volumes; its supported LV types do not
include thin pools.

Piraeus's
[`LinstorSatelliteConfiguration` reference](https://piraeus.io/docs/v2/reference/linstorsatelliteconfiguration/)
documents two relevant paths: register an already existing VG/thin pool, or
initialize backing storage from `source.hostDevices` with creation arguments.
The latter is initialization from raw devices, not a desired-size declaration
for a pool Talos already manages. Existing-pool registration has no capacity
field, so it cannot declare or grow the pool. In the upstream material reviewed,
there is no end-to-end recipe for this plan's shared Talos-managed VG, ordinary
localpath LV, and Piraeus LVM_THIN pool lifecycle.

A [Piraeus maintainer response](https://github.com/piraeusdatastore/piraeus-operator/issues/437)
describes using a separate provisioning system to maintain the effective storage
pool, with LINSTOR concerned with the VG/thin-pool it consumes. This supports
keeping disk/VG/thin-pool lifecycle ownership explicit, but is maintainer guidance
rather than a Talos integration contract. Unofficial examples that run LVM
commands from privileged Satellite workloads do not establish a supported or
safe Talos lifecycle and are not a production recommendation.

Piraeus then registers the prepared pool, for example on SSD nodes:

```yaml
apiVersion: piraeus.io/v1
kind: LinstorSatelliteConfiguration
metadata:
  name: ssd-pools
spec:
  nodeSelector:
    storage.allegedly.works/piraeus-pool: ssd
  storagePools:
    - name: ssd
      lvmThinPool:
        volumeGroup: data_ssd
        thinPool: piraeus
```

The pool label is new, proposed configuration, applied only after pool preparation.
For HDD use `hdd`, `data_hdd`, and the matching selector. Omit `source.hostDevices`:
this is registration of a prepared pool, not permission for Piraeus to initialize
the disk or legacy LV. The operator's
[pool reference](https://piraeus.io/docs/v2/reference/linstorsatelliteconfiguration/)
requires the backing thin pool to exist in this mode. Neither registration nor
reconciliation declares its capacity. Pool creation and growth ownership and
repeatable bootstrap are unresolved requirements for the LVM canary; do not
claim this manifest creates or resizes the proposed 64/128 GiB pool.

For OptiPlex, retain every system partition and its current local-path directory.
Once an additional SSD is identified, select only that new disk into `data_ssd`,
omit `localpath` and its user-volume documents, and dedicate the VG to the thin
pool plus reserve. Initial thin data can use 75% of usable VG space minus metadata/
spare, leaving at least 25% free; choose the absolute size after hardware inventory.
If a second drive is impossible, a larger system-disk replacement gets a separate
layout proposal with bounded EPHEMERAL and a raw LVM partition. The current `/var`
free space is not raw unallocated capacity, so do not use it as a block pool.

Convert `1001419` first, then `104952` and `104963` serially after restoration
gates; `104963`'s testing PostgreSQL singleton needs an accepted outage or prior
migration. Convert `102453` first among HDDs after evacuating every consumer.
The two smaller HDDs remain clients until their capacity gate is resolved.
Keep later NixOS allocation independent: wyrm2 needs dedicated SSD/HDD virtual
disks after Atlas capacity/slots are verified, preserving OpenEBS ownership;
iguana/rugged need online disk and encryption/unlock inventory first.

Do not change the install-disk layout to obtain its apparently free `/var`
space as part of this rollout. Do not delete the eight unmatched wyrm2 SSD LVs
or the five Released PVs to fund it. Both require independent ownership/data audits.

## Placement policy to implement and prove

- Use explicit physical-site properties: OVH and home. Existing `hil-ovh`,
  `home-lan` and `atlas` zone labels do not directly express the desired grouping
  of OptiPlex and wyrm2 into one home site.
- Keep normal synchronous replicas within a site. Home has one declared HDD
  storage host, so two home-local HDD copies need another independent HDD host.
  Multiple virtual disks or VMs on Atlas do not supply independent host durability.
- Permit broad client access without interpreting it as permission to relocate
  durable replicas across the WAN. Validate whether the pinned LINSTOR version's
  auto-diskful and cleanup preserve the intended site constraints; initial
  autoplacer selectors alone are not proof. Enable automatic locality only for
  the tested domain. Until this is established, keep cross-site moves explicit
  and do not advertise unrestricted automatic two-copy relocation.
- Keep laptops out of automatic durable replica placement for server workloads.
  Test that policy together with local-copy behavior, not merely node labels.
  A one-copy volume intentionally moved to a laptop remains dependent on that
  laptop's availability; it does not retain an implicit server backup.

[LINSTOR documents auto-diskful and replica-count cleanup](https://linbit.com/drbd-user-guide/linstor-guide-1_0-en/).
Our one-copy auto-diskful exercise and separate two-copy benchmark support
testing this design; neither tested three-node two-copy movement or WAN boundaries.

## Implementation changes

### First rollout: Talos

- [ ] **Host preparation:** generalize
      `cluster/terraform/main/drbd-canary.tf` into opt-in DRBD preparation for OVH
      and home Talos nodes. Preserve every node's existing extension/kernel settings.
      Add `dm-thin-pool` and required snapshot support for LVM_THIN. Roll one node at
      a time, control planes serially, using the existing Talos runbook and application
      restoration gates. Verify readiness before activating its Piraeus eligibility.
- [ ] **Provider scope:** extend `cluster/cdk8s/piraeus.py`, preserving one operator
      and one LINSTOR owner. Separate stable controller placement, prepared-node CSI/
      satellite eligibility and per-node pool selection. Initially admit only prepared
      Talos nodes; keep Piraeus-consuming Pods on eligible nodes, including remote
      consumers. Add NixOS-specific configuration and eligibility in the later phase.
      Add four explicit classes, suitable host-network/Nebula firewall reachability,
      a port allocation range larger than the 20-port canary, and protected monitoring.
      Account for control-plane taints and temporarily offline satellites.
- [ ] **LVM canary:** prove the shared-VG/ordinary-localpath-LV/thin-pool layout,
      repeatable pool bootstrap and ownership on disposable Talos storage before
      production disk conversion. Establish a Talos-supported lifecycle for
      creating and growing the pool. Verify host LVM reconciliation preserves
      LINSTOR volumes, locking and activation work across reboot, and mount
      readiness gates consumers.
      Grow the existing thin pool from VG reserve and from extents released by a
      retired ordinary LV. Verify data/metadata headroom, existing resource
      availability, and LINSTOR's refreshed storage-pool capacity while preserving
      the reserve.
      Arrange that capacity independently of NixOS enrollment or Atlas availability.
- [ ] **Pool allocation changes:** one node per reviewed destructive conversion;
      record exact device identities, all current consumers, replica/writer state,
      measured copy size, staging capacity, restorable backup and the restore target.
      Copy and verify singleton/user-authored data before wiping. Prove SeaweedFS
      per-volume replication and client cache safety; restore CNPG, ClickHouse and
      other consumers fully before taking the next node down. Preserve mount ownership
      in the Talos configuration and local-path nodePathMap together.
- [ ] **Recovery/operations:** enable and test the HA/fencing/quorum policy rather
      than simply enabling the canary-disabled HA controller. Establish LINSTOR metadata
      export/recovery, volume backup/restore, upgrade/drain procedures, and alerts for
      thin data/metadata capacity, free space, degraded replicas and stalled sync.
- [ ] **Workload migrations:** separate PRs after pool sizing and acceptance.
      Forgejo's checked-in deployment is two active RWX app replicas on SeaweedFS SSD;
      direct Piraeus RWO requires an explicit single-active-app design, or a separately
      benchmarked NFS/RWX design. The existing SeaweedFS S3 service remains in use.

### Later rollout: NixOS

- [ ] **Host support:** add a shared opt-in host module with a DRBD 9 module built
      for each running kernel, utilities and udev integration; keep resource ownership
      in LINSTOR. The earlier build targeted 6.18.55 while wyrm2 still runs 6.18.54.
      Build/inspect a matching module or use a scheduled kernel roll; do not assume
      either reboot-free activation or compatibility with rugged's last-known 7.2 kernel.
- [ ] **Enrollment and capacity:** add OS-specific satellite/CSI configuration and
      admit hosts individually, starting with wyrm2 after its capacity prerequisites
      are resolved. Validate client access before adding dedicated local pools.
      Inventory laptops online and test suspend/disconnect behavior before admission.
- [ ] **Interoperability acceptance:** repeat Block/Filesystem lifecycle and recovery
      checks on NixOS, then test Talos/NixOS remote use and automatic movement within
      the approved placement domain. Preserve existing Talos service throughout.

The Talos integration follows the [upstream Piraeus Talos guide](https://piraeus.io/docs/v2/how-to/talos/).
The repository's [canary preparation](../drbd_canary.md),
[canary observations](../../debug/drbd_canary/results.md),
[auto-diskful observations](../../debug/drbd_canary/auto_diskful/results.md), and
[benchmark report](../../debug/piraeus_storage_bench/README.md) are supporting evidence.

## Acceptance before admitting production data

These gates apply to the Talos deployment first. NixOS acceptance is a separate
gate for admitting NixOS nodes, not a prerequisite for Talos production use.

- [ ] On Talos for each tier, create a sparse 50 GiB volume; measure physical allocation,
      enforce the logical limit, expand, discard, snapshot/restore and retain/delete.
- [ ] On three stable same-site nodes, move a two-copy Filesystem and raw Block
      volume to the third node under writes: same PVC UID, remote use, automatic local
      sync, temporary third copy, return to two, checksums and placement constraints.
- [ ] Exercise an actual node loss, network partition, controller/satellite restart
      and node return. Verify quorum/tie-breaker/fencing prevents concurrent writers,
      measure recovery and confirm contents; a successful planned Pod move is insufficient.
- [ ] Exercise large sparse filesystem resync on the chosen LVM configuration,
      including an idle diskless primary. Resolve stalls without silently accepting
      performance measurements while synchronization is incomplete.
- [ ] Test constrained pool exhaustion/metadata monitoring on disposable capacity,
      and recover a volume plus controller metadata from independent backups.
- [ ] Demonstrate no unintended WAN synchronous peer or ineligible-node placement, and
      record what happens when a client lacks the requested local media or free space.

Outstanding Talos inputs are smaller-HDD legacy capacity budgets, OptiPlex's
hardware allocation and the two-copy site/locality policy. They block their
respective allocation steps, not the independent host-preparation and provider
changes. Atlas read access/backing headroom, NixOS kernel support and online
laptop inventory belong to the later NixOS rollout and do not block Talos.
