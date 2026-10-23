# Piraeus production rollout

Work for [storage issue #4717](https://github.com/agentydragon/ducktape/issues/4717),
[dedicated Talos system volumes #7462](https://github.com/agentydragon/ducktape/issues/7462)
and [safe OVH node lifecycle #7431](https://github.com/agentydragon/ducktape/issues/7431).
Remove completed work from this plan as it lands.

The [migration evidence index](../../debug/piraeus_migration/README.md) links to
dated reports and carries the latest operational snapshot; refresh live gates
before acting on a captured report.

## Agreed direction, 2026-10-21

Use **the whole physical data disk for Piraeus**. Combine each node's conversion
with its desired Talos system partition layout. Start with the SSD control plane
`ovh-ns1001419`, then complete its acceptance and restore application redundancy
before selecting another node. The operator accepted the partition sizes below
and proceeding **without an etcd snapshot backup** for this node's rebuild.
Etcd recovery relies on the two surviving healthy members; preserve the cluster's
existing identity and credentials and rehearse member removal/rejoin. This
exception does not waive preservation of application data or SeaweedFS master state.

The first physical node, `ovh-ns1001419`, was converted on 2026-10-22. Attic and
Study Casino returned to three healthy members with their replacement replicas
on strict-local Piraeus claims. The new Piraeus SSD volume server passed a
bounded native move of one 239 MB volume and separate owned S3 write/read checks;
all 34 SSD volume IDs retain two copies. Bulk redistribution has not run.
Seaweed master-2 returned from its temporary HDD claim to a 4Gi strict-local
Piraeus claim on the first node, preserving Raft identity and all cold-copy file
hashes/metadata. Three-master agreement and owned S3 read/write checks passed;
operator/Helm/Flux management resumed. The HDD source remains retained.
The [node-roll runbook](../runbooks/piraeus_node_roll.md) records that handoff,
the next control-plane candidates, per-node readiness gates and conditional
HDD-worker/control-plane pairing. No later node roll has started.

The [disposable QEMU rehearsal](../../debug/piraeus_migration/first_node/README.md)
and physical first-node checks cover LVM_THIN lifecycle and control-plane
repartition/rejoin. Do not infer production DRBD-pool or metadata exhaustion
recovery from those checks. The previous shared-VG design with an ordinary
legacy `localpath` LV is superseded.

Keep the Talos rollout independent of NixOS, Atlas capacity and laptops. Begin
with single-copy, strict-local storage. Replication, remote access and automatic
movement are later opt-in capabilities with their own acceptance gates.

The [2026-10-19 inventory](../../debug/piraeus_migration/inventory/README.md),
[FILE_THIN canary](../../debug/drbd_canary/results.md),
[automatic-movement results](../../debug/drbd_canary/auto_diskful/results.md), and
[benchmarks](../../debug/piraeus_migration/benchmarks/README.md) are dated evidence.
The first-node consumers and sizing observations were refreshed read-only on
2026-10-21 PDT; refresh placement, usage and health again before execution.
FILE_THIN results and offline YAML validation do not establish LVM_THIN runtime,
performance, provisioning or recovery behavior.

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

| Proposed class      | Pool  | Diskful copies | Access policy                          |
| ------------------- | ----- | -------------: | -------------------------------------- |
| `piraeus-ssd-local` | `ssd` |              1 | Strict-local; initial SSD pilot        |
| `piraeus-hdd-local` | `hdd` |              1 | Strict-local; after HDD acceptance     |
| `piraeus-ssd-1`     | `ssd` |              1 | Remote-capable; later acceptance       |
| `piraeus-ssd-2`     | `ssd` |              2 | Remote-capable, synchronous protocol C |
| `piraeus-hdd-1`     | `hdd` |              1 | Remote-capable; later acceptance       |
| `piraeus-hdd-2`     | `hdd` |              2 | Remote-capable, synchronous protocol C |

Use Block and Filesystem, thin allocation, expansion, WaitForFirstConsumer and
Retain. Leave the cluster default unchanged. The strict-local classes use
`placementCount: "1"` and `allowRemoteVolumeAccess: "false"`, with no automatic
movement. These settings retain the default DRBD layer. Compare a distinct
canary class with `linstor.csi.linbit.com/layerList: "storage"` to measure local
LVM_THIN without DRBD; [LINSTOR documents storage-only volumes](https://linbit.com/drbd-user-guide/linstor-guide-1_0-en/).
Select the production layer stack from the acceptance results. Changing an
existing volume's layer stack is not an assumed in-place fallback.

For later remote-capable classes, the locality goal is auto-diskful with cleanup:
a one-copy volume follows its consumer; two-copy movement temporarily adds a
third copy and returns to two. This needs compatible local media and capacity.

The design below proposes LVM_THIN for production. Our accepted performance and movement
tests used FILE_THIN; they do not validate LVM_THIN or its failure behavior.
[LINBIT describes File/FileThin as mainly demonstration/experiment backends](https://linbit.com/drbd-user-guide/linstor-guide-1_0-en/).
Keep the existing file-backed canary separate during transition, and do not
silently mix backend types or reuse its discard tuning without new measurements.

### Scheduling locality and network budget

Later remote-access work; not a prerequisite for the strict-local pilot.

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

## Physical ownership and partition sizing

```text
System NVMe: Talos boot/state + EPHEMERAL + ETCD + CRI + LOG + KUBELET
Data NVMe: whole device -> Piraeus-owned VG/thin pool -> per-PVC thin volumes
```

Talos owns system partitions. Piraeus initializes a dedicated data VG/thin pool
from the explicitly selected raw device; LINSTOR owns its thin volumes. There is
no legacy local-path filesystem or ordinary LV on the converted data disk.
System volumes must not depend on a Piraeus PVC to boot kubelet or containerd.

### First node: `ovh-ns1001419`

Both NVMe devices are 512110190592 bytes, approximately **476.9 GiB** each.
The dated inventory identified these devices; verify them again before any wipe:

| Role        | Observed path  | Stable serial  |
| ----------- | -------------- | -------------- |
| System disk | `/dev/nvme0n1` | `190130800417` |
| Data disk   | `/dev/nvme1n1` | `1951A3449505` |

Proposed system-disk allocations, accepted by the operator on 2026-10-21:

| Talos volume                   |           Size | Purpose and sizing rationale                                                 |
| ------------------------------ | -------------: | ---------------------------------------------------------------------------- |
| EFI/META/STATE and boot layout | Talos defaults | Preserve the installer's required layout; allow roughly 4 GiB overall        |
| `ETCD`                         |         16 GiB | Approximately 0.9 GB directory today; room for database growth and WAL       |
| `EPHEMERAL`                    |         32 GiB | Remaining `/var` state after the major consumers have dedicated partitions   |
| `CRI`                          |        192 GiB | Container images, snapshots and writable layers; 27.3 GB reported today      |
| `KUBELET`                      |        128 GiB | Kubelet state and disk-backed `emptyDir`; room for temporary workload bursts |
| `LOG`                          |         16 GiB | Bound `/var/log` separately; approximately 290 MB today                      |
| Unallocated                    |   About 89 GiB | Leave capacity uncommitted; exact remainder depends on installer geometry    |

The five sized filesystems total **384 GiB**. These budgets are for this SSD
control plane, which retains its control-plane taint; they are not automatic
sizes for HDD workers or the smaller OptiPlex. Do not use spare system capacity
as an accidental local-path fallback after converting the data disk.

Sizing evidence, collected read-only around **2026-10-22 04:20 UTC**:

- Kubelet `stats/summary`: system filesystem capacity 507558514688 bytes,
  used 40281325568 bytes (37.5 GiB); runtime `imageFs.usedBytes`
  27295113216 bytes (25.4 GiB). The runtime value is reported image usage, not a
  complete independent `du` measurement of all containerd state.
- Across 42 Pods, reported `ephemeral-storage.usedBytes` summed to 481050624
  bytes (0.45 GiB). This is not a complete measurement of kubelet's directory.
- Separate `talosctl usage --depth 1 --humanize` queries reported `/var/lib/etcd`
  918 MB, `/var/lib/containerd` 18 GB, `/var/log` 290 MB and `/var/lib/alloy-node`
  228 MB. Talos usage and kubelet runtime accounting differ; do not sum or treat
  their figures as equivalent filesystem allocation measurements.
- Kubelet image GC thresholds were 85% high / 80% low, with hard eviction at
  `imagefs.available<15%` and `nodefs.available<10%`. A 192 GiB CRI filesystem
  leaves substantial headroom above current usage even before the GC thresholds.
  Container log rotation was 10 MiB per file and five files per container.

Use explicit system-disk selectors and fixed initial sizes in the node's
`VolumeConfig` documents. Set EPHEMERAL's bound explicitly so its default growth
cannot consume the space intended for the other partitions. Rehearse the actual
partition order, allocation and mount behavior. Unallocated tail space is not
interchangeable free VG space: extending a particular partition requires suitable
adjacent extents and a supported growth procedure. No arbitrary future resize or
relocation follows from these initial sizes.

### Talos LVM and future system-volume moves

Talos 1.14 supports declarative PV/VG and ordinary LV provisioning, but does not
expose a supported system-on-LVM installation. Its
[system-volume implementation](https://github.com/siderolabs/talos/blob/v1.14.0/internal/app/machined/pkg/controllers/block/internal/volumes/volumeconfig/system_volumes.go)
creates partitions for EPHEMERAL and for explicitly provisioned ETCD, CRI,
KUBELET and LOG. The [VolumeConfig reference](https://docs.siderolabs.com/talos/v1.14/reference/configuration/block/volumeconfig)
has a disk selector and partition sizes, not a VG/LV backing selector. The
[LVM guide](https://docs.siderolabs.com/talos/v1.14/configure-your-talos-cluster/storage-and-disk-management/lvm)
describes preparing storage for consumers such as CSI drivers; it does not
establish system-volume boot and mount ordering on LVs. Do not use nested
partitions inside LVs or overmount reserved system paths as an unproven workaround.

The [system-volume guide](https://docs.siderolabs.com/talos/v1.14/configure-your-talos-cluster/storage-and-disk-management/disk-management/system)
allows selecting another physical disk at provisioning. Moving an existing
system volume later remains a maintenance/data-migration operation. An LVM LV
also only changes I/O isolation when its extents actually move to another device.
The proposed partitions isolate capacity; etcd and runtime I/O still share the
system NVMe. This does not achieve #7462's original physical separation onto the
second NVMe. Achieving that later needs another device or revisiting whole-device
Piraeus ownership. Preserve etcd's dedicated placement when moving runtime data;
EPHEMERAL includes etcd whenever ETCD has not been separated explicitly.

Splitting KUBELET, LOG and CRI also requires checking the running kubelet's
filesystem identification, ephemeral-storage accounting, eviction and image GC.
Successful mounts alone do not prove the expected `nodefs`/`imagefs` behavior.

### Piraeus pool creation and capacity

Give Piraeus exclusive ownership of the **whole data device**, including its
metadata and capacity reserve. This does not specify the thin-data LV's exact size.
Use the existing operator's `LinstorSatelliteConfiguration` with `lvmThinPool`
and `source.hostDevices` against the verified device identity. Talos's old
whole-disk XFS `UserVolumeConfig` must be removed before Piraeus initializes it.
The [Piraeus reference](https://piraeus.io/docs/v2/reference/linstorsatelliteconfiguration/)
documents this raw-device initialization path; omitting `source` only registers
a pool that must already exist.

The operator's creation arguments apply only to new storage. LINSTOR 1.35.2's
[thin-pool creation code](https://github.com/LINBIT/linstor-server/blob/v1.35.2/satellite/src/main/java/com/linbit/linstor/layer/storage/lvm/utils/LvmCommands.java)
requests `100%FREE`; it is not a desired-size reconciler or an implementation of
an arbitrary reserve budget. During the disposable rehearsal, record actual
thin-data size, chunk size, metadata size, metadata spare and remaining VG
extents. Select and prove the metadata/headroom policy before production use;
creation arguments are not a proven mechanism for overriding pool size or growth.
Do not reintroduce shared Talos/Piraeus VG ownership to solve this.

Monitor physical thin-data and metadata utilization independently of PVC logical
limits. Thin allocation is not an aggregate capacity reservation. Prove discard,
near-full behavior and recovery; never assume deleting files releases thin blocks
without discard. Automatic pool extension is not enabled by this plan. Future
pool growth needs an explicit owner and capacity procedure; additional capacity
can also be introduced through other prepared nodes followed by verified moves.

## First-node conversion sequence

Prepare a reviewed, node-specific implementation and rehearse the system layout
and LVM_THIN lifecycle on disposable Talos storage before production changes.
Keep the existing FILE_THIN canary separate. Select any necessary stable
controller placement independently of satellite/data-node eligibility.

### Rehearsal on existing capacity

Use existing compute and disposable virtual disks for the rehearsal; an additional
OVH server is not a prerequisite or a planned purchase. First inventory available
RAM, virtualization support and free backing storage on existing Linux/KVM or
Proxmox hosts. Using a host for temporary VMs does not enroll that host in Piraeus
or make the production rollout depend on its NixOS/Atlas storage conversion.
[Talos supports a local QEMU cluster](https://docs.siderolabs.com/talos/v1.14/platform-specific-installations/local-platforms/qemu).

- Rehearse partition allocation, boots, etcd member replacement and Piraeus
  lifecycle in an isolated disposable Talos cluster with the intended versions
  and extensions. Several VMs on one host suffice for the membership procedure;
  they do not demonstrate independent-host availability.
- Use sparse virtual disks matching the target system-disk geometry for partition
  layout checks. Bound writes by actual host free space. Exercise pool exhaustion
  on a separate small disposable pool, without filling the backing filesystem.
- Measure production performance on the actual first OVH node after evacuation,
  using disposable claims before restoring application data. VM results establish
  lifecycle behavior, not OVH NVMe latency, throughput or hardware boot behavior.

If existing capacity cannot support a required rehearsal or evacuation, record
the measured RAM/storage/virtualization blocker and consider temporary allocations
on other existing hosts first. Propose paid capacity only for a demonstrated gap
and obtain the operator's approval before ordering it.

### 1. Evacuate onto existing storage

Before the first pool exists, destinations are the other two OVH SSD nodes or
independently accessible temporary storage. Refresh space, per-volume placement,
replica roles, actual claim UIDs and controller ownership. The most recent live
check found four local-path consumers on `ovh-ns1001419`:

| Consumer                                        | Evacuation and recovery gate                                                                                                                                                                       |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `nix-cache/attic-db-1`, current primary         | Bring up an additional off-node replica if needed, verify catch-up and switch primary before retiring the target member. Maintain two healthy off-node copies where the operator workflow permits. |
| `study-casino/study-casino-db-2`, replica       | Verify the other two instances are healthy/current; retire only this member through the CNPG migration workflow.                                                                                   |
| `seaweedfs/mount0-seaweedfs-volume-ssd-2`       | Evacuate data to the other SSD servers, checking physical headroom, volume slots and required healthy copies of every volume.                                                                      |
| `seaweedfs/seaweedfs-master-seaweedfs-master-2` | Quiesce and preserve Raft state off-node, verify two healthy surviving masters and rehearse restoring the same member identity on a new claim.                                                     |

Retain source data until verified recovery material exists elsewhere. Hold the
appropriate reconciler during each PVC/StatefulSet handoff, and prevent new
local-path claims from provisioning onto the target. Explicitly deny its legacy
path; removing only a nodePathMap entry can expose a default fallback. Freeze
Seaweed placement-changing maintenance as needed. If a volume-server endpoint is
retired, prove client cache safety while the evacuated endpoint is still alive;
see the [existing runbook](../runbooks/rolling_seaweedfs_volume_pvc.md).

The [Seaweed master persistence migration](../lessons_learned/2026_10_19_seaweedfs_master_persistence.md)
required a controlled operator/StatefulSet handoff because claim templates were
immutable. Volume-server evacuation does not migrate master Raft state. Confirm
actual leader agreement plus application S3 reads/writes; readiness alone is not
a recovery gate. CNPG storage replacement similarly needs its operator-specific
procedure; changing a Cluster's storage class does not rewrite bound claims.

### 2. Rebuild this node's system layout

Cordon/drain under application and PDB gates. Verify both surviving etcd members
are healthy and the cluster's existing credentials/configuration are available.
The operator explicitly accepts **no etcd snapshot backup for this rebuild**.
Use the rehearsed member removal/rejoin process; do not bootstrap a new cluster.

Use a per-node operation with verified physical disk identities and a reviewed
plan containing no other node's rescue boot, raw write or replacement. Resolve
that boundary from #7431 before executing; a broad Terraform apply is not the
rebuild mechanism. Reprovision the system disk with the table above, preserving
host identity, networking, access material and existing extension requirements.
This initial SSD enrollment also needs the matching DRBD extension if the DRBD
canary arm is used, plus the required thin-pool kernel modules.

Bring the node back under restricted pilot placement. Verify system mounts,
etcd membership/quorum and kubelet storage accounting before ordinary workloads.

### 3. Initialize the data disk and qualify the pool

After every source-data preservation gate passes, remove Talos's old data-volume
owner and initialize the whole secondary NVMe through Piraeus. Keep the node
excluded from legacy local-path provisioning after it becomes schedulable.
Verify the VG/pool geometry, metadata/spare and protected monitoring.

Use disposable Block and Filesystem claims to compare local-path/direct storage,
one-copy DRBD/LVM_THIN and storage-only LVM_THIN. Match durability settings and
record filesystem, media, placement and active layer stack. Include database
fsync/tail latency, Seaweed volume-server I/O and representative application
operations. Exercise limit enforcement, sparse allocation, expansion, discard,
retention, restart/reboot and volume/controller recovery on disposable capacity.
Select the production stack from these results. Do not extend the control-plane
maintenance window for a long soak: finish the short node/pool admission gates,
then keep application data on verified off-node copies through the step 5 stop.
Use disposable claims for extended observation before the step 6 production
handoffs.

### 4. Maintain production service on off-node application copies

Keep evacuated application data on its verified off-node replacements during
node/pool acceptance. Keep Seaweed master 2 on its temporary HDD-backed claim
in the original master topology; verify three healthy masters, leader
agreement, and application S3 reads/writes. Keep the affected Attic and Study
Casino CNPG clusters at two healthy off-target instances while their evacuated
members remain off-node. Do not restore the third instance or create production
Piraeus PVCs yet. Study Casino's instance claim template remains 1Gi.

Keep the two existing SSD local-path volume servers and their claims serving
the existing Seaweed volumes. Do not edit their immutable claim template or
move SSD volumes onto Piraeus in this step. Applications using existing
SeaweedFS CSI claims keep those claims and their service endpoints. Keep
LINSTOR controller metadata/recovery independent of the first data pool so the
pool can be recovered without itself.

Observe service health and replica convergence while production state remains
off-node. Use disposable claims for pool headroom, restart, and recovery
observations. Retain verified off-node sources until the corresponding
step 6 replacements have been accepted.

### 5. Complete node and pool acceptance, then pause

Complete the checks in [First SSD node, strict-local mode](#first-ssd-node-strict-local-mode),
including observed thin-pool data and metadata alerts, while application data
remains on its off-node copies. The [first-node execution
record](../../debug/piraeus_migration/first_node/README.md) stops before step 6.
Stop here, present the acceptance evidence, and wait for the user's explicit
approval before creating any production Piraeus PVC or restoring application
data onto Piraeus.

### 6. Restore production members and add the Piraeus-backed SSD server

Proceed only after step 5 acceptance and explicit user approval. This is the
first step that may create production Piraeus PVCs. Keep verified off-node data
and temporary HDD sources until each replacement is accepted.

Restore the CNPG members and SSD volume server first. Seaweed master 2 is healthy
on its temporary HDD claim and can return in a separate handoff afterward; it
does not block restoring SSD capacity or admitting ordinary workloads.

When returning the evacuated Seaweed master 2, use its application-specific handoff:
copy the verified Raft state from the temporary HDD-backed claim to a
replacement Piraeus claim, preserve the member identity, and verify three
healthy masters, leader agreement, and application S3 reads/writes before
retiring the source. Rebuild each evacuated Attic and Study Casino CNPG member
on a new strict-local Piraeus claim through its operator's native replica
workflow, one member at a time. Keep the other two healthy off-node instances
serving while CNPG creates and catches up the replacement; require all three
healthy before completing that member handoff. Keep Study Casino's instance
claim template at 1Gi.
Applications continue to use their existing SeaweedFS CSI claims and service
endpoints. Do not add a second Piraeus copy by default when the application
already provides replication.

Keep the two existing `local-path-ovh-ssd` volume servers and their claims,
and add a separate Seaweed `volumeTopology` group, such as `ssd-piraeus`, with
one filesystem-backed volume-server claim on the accepted rebuilt node:

| Setting                      | Proposed value                                  |
| ---------------------------- | ----------------------------------------------- |
| Kubernetes topology group    | `ssd-piraeus`                                   |
| Replicas                     | `1`                                             |
| Storage class                | `piraeus-ssd-local`                             |
| Volume-server PVC request    | `250Gi` (initial sizing proposal)               |
| Seaweed disk argument        | `-disk=ssd`                                     |
| Seaweed data center and rack | `hil` / `hil-ovh-h108b01`                       |
| Seaweed default replication  | `001` (two copies across the SSD placement set) |
| Piraeus placement            | One local copy; remote access disabled          |

Keep the existing `ssd` group's `local-path-ovh-ssd` claim template unchanged;
StatefulSet claim templates are immutable. The new Kubernetes topology group
name must not create a separate Seaweed placement domain: retain the existing
Seaweed `disk=ssd`, `dataCenter=hil` and `rack=hil-ovh-h108b01` identity so
`defaultReplication: "001"` places two Seaweed copies across the existing and
new SSD servers. Piraeus supplies one local backing copy for the new server;
Seaweed supplies the second data copy.

An empty new server registers with the normal Seaweed heartbeat but does not
automatically attract volumes that already have two healthy copies. Start with
a bounded canary set moved using Seaweed's native volume-move operations. Verify
reads and writes, two healthy copies, expected SSD placement, and adequate thin
pool data and metadata headroom before each further batch. Then rebalance SSD
volumes gradually, checking the same gates as placement changes.

Before creating the new claim or moving data, reconfirm that the node and pool
passed step 5 and that the data/metadata alerts report fresh samples with usable
headroom. Ensure Seaweed volume-server monitoring covers both SSD topology
groups. Review generated pod labels and anti-affinity across both groups: the
current helper matches `seaweedfs/topology` using the disk value, which may not
match a new group named `ssd-piraeus`. Extend the selector so all SSD volume
servers remain separated across hosts; do not assume the distinct Kubernetes
group name changes Seaweed's replication placement.

Use the [volume-server runbook](../runbooks/rolling_seaweedfs_volume_pvc.md)
for replication convergence and the alive-empty-server/FUSE-client gate if a
server endpoint is later retired. This step retains both existing local-path
servers; their later retirement is a separate reviewed change. Once replacement
claims accept writes, old copies are stale; rollback requires write quiescence
and data reconciliation rather than simply switching claim names.

### SeaweedFS and later conversions

For later converted disks, move or rebuild each remaining local-path consumer.
Move Seaweed volume-server claims to Piraeus-backed groups rather than retaining
them on a legacy `localpath` LV. Use application-native replication or a quiesced
final copy for other state, keeping each conversion serial and independently
reviewable.

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

These classifications come from the [dated PVC inventory](../../debug/piraeus_migration/inventory/pv-consumers.tsv)
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

### Capacity changes and later hardware

Evacuating a Seaweed volume server relocates its data and does not reduce the
cluster-wide dataset. Moving an application out of Seaweed only frees its old
storage after cutover and cleanup; this does not shrink an existing server PVC.
Kubernetes PVC shrink and thin-pool shrink are not assumed operations. Use new
claims or verified replica relocation when reducing an allocation.

After the first node passes, refresh the inventory to choose the next SSD node.
The earlier candidate order was `104952` then `104963`; the latter has a testing
PostgreSQL singleton requiring a preserved-data migration or accepted outage.
Choose the first HDD conversion from current consumer/evacuation evidence, then
size its system partitions independently. Do not carry the old 1536 GiB legacy
filesystem budgets forward into the whole-data-disk design.

OptiPlex's proposed [single-SSD replacement layout](optiplex_piraeus.md) preserves
all active and retained local PV data off-node before rebuilding bounded Talos
system partitions and a raw partition for Piraeus. Its CPAP VM is stateless;
Home Assistant's data claim and VolSync cache need an explicit storage handoff.
The proposal needs separate review and execution; no additional SSD is assumed.
Wyrm2 needs dedicated
SSD/HDD virtual disks after Atlas capacity/slots are verified; existing OpenEBS
VGs remain owned by OpenEBS. Inventory iguana/rugged online, including encryption
and unlock behavior, before their later enrollment. Unmatched wyrm2 LVs and
Released PVs require their own ownership/data audit and do not fund this rollout.

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
      Add the strict-local pilot classes first and the remote/replicated classes only
      after their acceptance, with suitable host-network/Nebula firewall reachability,
      a port allocation range larger than the 20-port canary, and protected monitoring.
      Account for control-plane taints and temporarily offline satellites.
- [ ] **System layout and LVM canary:** rehearse the accepted system partition
      budgets, emitted disk geometry, identity preservation, etcd member rejoin,
      kubelet filesystem accounting and the exclusive Piraeus data VG/thin pool.
      Compare single-copy DRBD and storage-only LVM_THIN, prove initialization,
      interrupted initialization, reboot, logical limits and near-full recovery.
      Establish metadata/spare/headroom policy without assuming declarative thin-pool
      growth. Arrange disposable capacity independently of NixOS/Atlas enrollment.
- [ ] **First-node implementation:** prepare the per-consumer off-node evacuation,
      Seaweed master handoff, local-path exclusion and explicit one-node rebuild
      for `ovh-ns1001419`. No etcd snapshot is required for this operator-accepted
      procedure. Preserve application state and restore every application/etcd
      replica before another node's conversion.
- [ ] **Later pool conversions:** one node per reviewed destructive conversion;
      refresh exact devices, consumer UIDs, writer/replica state, measured copy
      sizes, staging capacity and recovery target. Apply the chosen system layout
      in that node's maintenance window, with role/hardware-specific sizing.
      Restore CNPG, SeaweedFS, ClickHouse and other state fully between nodes.
- [ ] **Recovery/operations:** establish the local-volume restart, recovery and
      node-maintenance procedure first. Gate later replicated HA/fencing/quorum
      separately; do not simply enable the canary-disabled HA controller. Establish LINSTOR metadata
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
[benchmark report](../../debug/piraeus_migration/benchmarks/README.md) are supporting evidence.

## Acceptance before admitting production data

### First SSD node, strict-local mode

- [ ] Prove the system partition layout, restricted placement, kubelet storage
      accounting and etcd member rejoin on disposable storage before the rebuild.
- [ ] Prove exclusive raw-device pool initialization, interrupted initialization,
      metadata/spare/headroom policy and no competing Talos/local-path disk owner.
- [ ] On LVM_THIN, create a sparse 50 GiB Block and Filesystem volume; measure
      physical allocation, enforce logical limits, expand, discard, snapshot/restore
      and exercise Retain/delete. Compare DRBD and storage-only paths explicitly.
- [ ] Record representative application and database fsync/tail-latency results
      against a same-node direct-storage baseline, with matching durability settings.
- [ ] Prove controller/satellite restart, node reboot and volume reattachment with
      contents preserved. Test pool-full/metadata alerts and recovery on disposable
      capacity; recover a volume plus LINSTOR metadata from independent backups.
- [ ] Rehearse Seaweed master identity-preserving claim replacement and confirm
      its recovery gate with leader agreement and actual S3 reads/writes.
- [ ] Restore expected etcd, Seaweed and database replica counts and complete the
      first-node soak before converting another node.

The operator's no-etcd-backup decision is limited to this rebuild; it does not
remove the application or LINSTOR recovery gates above. NixOS and additional
hardware acceptance remain separate from this Talos SSD pilot.

### Later remote access, replication and automatic movement

- [ ] Repeat lifecycle acceptance for each new media tier and operating system.
- [ ] On three stable same-site nodes, move a two-copy Filesystem and Block volume
      to the third node under writes: same PVC UID, remote access, automatic local
      sync, temporary third copy, return to two, checksums and placement constraints.
- [ ] Exercise node loss, network partition, controller/satellite restart and node
      return. Verify quorum/tie-breaker/fencing prevents concurrent writers and
      measure recovery; a planned Pod move alone is insufficient.
- [ ] Exercise large sparse filesystem resync, including an idle diskless primary.
      Resolve stalls before accepting performance measurements during a sync.
- [ ] Demonstrate no unintended WAN synchronous peer or ineligible-node placement;
      record behavior when the consumer lacks compatible local media or free space.

Remaining inputs are the disposable rehearsal, implementation of a bounded
per-node reprovisioning path, pool metadata/headroom policy and application
handoffs. The system partition sizes are the operator-accepted proposal above.
Later nodes need refreshed evacuation capacity and role-specific sizes. Atlas,
NixOS kernels and laptop inventory do not block the first Talos node.
