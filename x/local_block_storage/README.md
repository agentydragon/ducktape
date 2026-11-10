# Local block storage research

Draft requirements and provider evaluation for
[#4717](https://github.com/agentydragon/ducktape/issues/4717).
Research and inventory dated 2026-10-18. No provider is selected; compatibility,
performance, and migration behavior remain unvalidated on this cluster.

## Requirements

- One StorageClass usable across prepared Talos nodes, `wyrm2`, `optiplex`, and
  roaming NixOS workers. The class must not encode region or machine-type
  restrictions. Initial provisioning follows workload placement; a bound local
  volume still constrains subsequent scheduling until its data moves.
- Node-local data access and a mode with one authoritative durable local copy.
  Outside migration, writes and `fsync` must not require a remote storage server's
  acknowledgment or flush. A configured replica count of one is not sufficient
  evidence of this behavior.
- `volumeMode: Block`, plus ordinary filesystem PVCs.
- Hard per-volume capacity enforcement: a claim cannot silently exceed its size.
- Thin allocation: a `50Gi` volume consumes physical space as written, plus
  metadata. Whether total promised capacity may exceed physical capacity is a
  separate policy decision.
- Declarative ownership of host preparation, backing-pool placement, provisioner,
  StorageClass, and monitoring. Refuse provisioning if an intended backing mount
  is missing instead of allocating on the system filesystem accidentally.
- Persistence across reboots and pod recreation, with explicit retention,
  deletion, backup, and recovery procedures.

Desired capabilities:

- Explicit moves between nodes preserving the existing PVC and data.
- Keep the workload running during initial synchronization, followed by a short
  final cutover. This does not require live migration of process or VM memory.
- Online expansion and physical-space reclamation through discard/TRIM.
- Capacity-aware scheduling and observable physical usage, logical allocations,
  thin-pool metadata, and headroom.
- Optional media selection without requiring every workload to choose SSD or HDD.

## Migration contract

Temporary duplication during an explicitly requested move is acceptable:

```text
one local copy on X -> synchronize X to Y -> cut over -> one local copy on Y
```

The source stays authoritative until a safe handoff. After completion, remove the
source data copy and restore local-only acknowledgment behavior. Planned relocation
does not recover data after loss of the only durable copy.

Acknowledgment policy while migration is active remains open. The single-copy
requirement does not establish whether a temporary synchronous peer is acceptable
during synchronization. Compare local acknowledgments with asynchronous copying
against synchronous replication, including convergence, failure recovery, and
application latency. Preserve protection against conflicting authoritative writers;
do not assume globally disabling quorum or fencing is an acceptable solution.

| Move procedure                           | Benefit                          | Cost / unresolved behavior                                                                               |
| ---------------------------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Stop writers for the whole copy          | Simpler consistency and recovery | Outage includes transfer and restart                                                                     |
| Synchronize while running, then cut over | Desired: shorter final outage    | Concurrent-write tracking, transfer convergence, acknowledgment policy, and safe handoff need validation |

Acceptable final downtime and whether any workloads need online migration as a
hard requirement remain undecided. A volume synchronization feature does not itself
migrate a running application or VM.

## Node layouts and placement options

Talos devices, volumes, mounts, and filesystem usage were read live on 2026-10-18,
as were `wyrm2` block devices, filesystems, and LVM state. Roaming nodes were
unreachable; their layouts are repository configuration only. Atlas's physical
pool inventory could not be verified because SSH authentication failed.

Talos sizes/free space below are decimal GB/TB; `wyrm2` values are GiB. Free space
is a changing observation, not a reservation for this project.

| Node               | Current backing storage                                         | Available at observation | File-backed placement candidate                                                             |
| ------------------ | --------------------------------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------- |
| `ovh-ns103656`     | Separate 2TB HDD `/dev/sdb`, whole-device XFS                   | ~595GB                   | New directory under `/var/mnt/local-path-ovh-hdd`                                           |
| `ovh-ns103711`     | Separate 2TB HDD `/dev/sdb`, whole-device XFS                   | ~202GB; ~90% used        | Same arrangement, subject to a tight capacity budget                                        |
| `ovh-ns102453`     | Separate 4TB HDD `/dev/sdb`, whole-device XFS                   | ~1,798GB                 | New directory under `/var/mnt/local-path-ovh-hdd`                                           |
| `ovh-ns104952`     | Separate 450GB NVMe `/dev/nvme1n1`, whole-device XFS            | ~409GB                   | New directory under `/var/mnt/seaweedfs-data`                                               |
| `ovh-ns104963`     | Separate 450GB NVMe `/dev/nvme1n1`, whole-device XFS            | ~410GB                   | New directory under `/var/mnt/seaweedfs-data`                                               |
| `ovh-ns1001419`    | Separate 512GB NVMe `/dev/nvme1n1`, whole-device XFS            | ~499GB                   | New directory under `/var/mnt/seaweedfs-data`                                               |
| `optiplex`         | One 256GB NVMe; `/var` on XFS EPHEMERAL partition               | ~61.6GB                  | Directory under `/var`, sharing capacity with node services and existing local-path volumes |
| `wyrm2`            | 500GiB ext4 `/dev/vda` at `/var/local-path-provisioner`         | ~448GiB                  | New directory on this separate filesystem                                                   |
| `rugged`, `iguana` | Configured btrfs on LUKS, sharing capacity with OS/user storage | Not verified             | Dedicated subvolume/directory on the existing encrypted filesystem                          |

OVH data disks already serve local-path consumers, including SeaweedFS. A new
directory shares their capacity and I/O. `optiplex` is particularly constrained:
fully writing another `50Gi` consumes about 53.7GB, leaving only about 8GB of its
observed free space for everything else.

`wyrm2` also has native LVM capacity:

| Device     | Role                            | Observed capacity                                              |
| ---------- | ------------------------------- | -------------------------------------------------------------- |
| `/dev/vdc` | 500GiB VG `openebs-proxmox-ssd` | ~470GiB unallocated in VG; existing 30GiB thin pool            |
| `/dev/vdg` | 500GiB VG `openebs-proxmox-hdd` | ~68GiB unallocated; existing ~431GiB pool ~72% physically used |

The VM definition maps `/dev/vda` and `/dev/vdc` to `local-zfs` SSD storage, and
`/dev/vdg` to `tank-hdd`; physical Atlas pool headroom is unverified. Existing
kubelet, containerd, cache, model, and scratch disks have distinct owners and are
not proposed for repurposing.

Native LVM/ZFS pools elsewhere need additional raw capacity or a disk-layout
migration. Filesystem free space is not unallocated capacity for a native VG or
ZFS pool. Existing btrfs subvolumes do not independently reserve disk capacity.

`seaweedfs-data` is a legacy mount name, not the desired ownership model for the
new storage. A neutral future layout such as `local-storage` with separate
consumer directories is preferable. Repartitioning and renaming are deferred
decisions. Renaming an existing Talos `UserVolumeConfig` can reprovision its disk;
the repository's existing mount-renaming procedure treats that as a data migration.

Repository sources:

- [OVH disk ownership](../../cluster/terraform/main/ovh-nodes.tf)
- [OptiPlex machine configuration](../../cluster/terraform/main/home-nodes.tf)
- [Last local-path provisioner source before retirement](https://github.com/agentydragon/ducktape/blob/2c8ccaac0d5e85e27294181e07eb68363c230ae9/cluster/cdk8s/local_path_provisioner.py)
- [`wyrm2` mounts and LVM setup](../../nix/nixos/hosts/wyrm2/default.nix)
- [`wyrm2` VM disks](../../cluster/terraform/main/proxmox-vms.tf) and
  [default datastore](../../cluster/terraform/main/variables.tf)
- [`rugged` filesystems](../../nix/nixos/hosts/rugged/hardware-configuration.nix)
- [`iguana` filesystems](../../nix/nixos/hosts/iguana/hardware-configuration.nix)
- [Prior thin-pool exhaustion](../../cluster/docs/lessons_learned/2026_07_17_lvm_thinpool_exhaustion_emergency_ro.md)

Inventory can be refreshed with `kubectl get nodes -o wide`, per-node
`talosctl --endpoints=<ip> --nodes=<ip> get disks`, `get volumestatus`,
`get mountstatus`, and `mounts`; NixOS inspection used `lsblk`, `df`, `vgs`, and
`lvs`. Use the configured credentials and explicit Talos endpoint/node pairs.

## Provider landscape

This is a survey of relevant open-source families, not an exhaustive evaluation
of every CSI driver. Documentation and limited source review do not establish
cluster compatibility or measured performance.

| Provider                                                                                                                                                    | Fit                                                                                                                                           | Main constraint / remaining evidence                                                                                                                                  |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [LINSTOR/Piraeus](https://github.com/piraeusdatastore/piraeus-operator)                                                                                     | Strong candidate for logical volumes, single-copy operation, and managed relocation; sparse file-backed pools can retain existing filesystems | DRBD9 kernel integration, controllers/satellites, local acknowledgment behavior, and migration recovery need validation                                               |
| [OpenEBS Rawfile](https://github.com/openebs/rawfile-localpv)                                                                                               | Thin loop-backed block volumes on existing filesystems                                                                                        | Pre-v1; no established built-in same-PVC relocation workflow found                                                                                                    |
| [OpenEBS LVM](https://github.com/openebs/lvm-localpv) / [TopoLVM](https://github.com/topolvm/topolvm)                                                       | Native thin LVs, block/filesystem PVCs, expansion; OpenEBS already configured on `wyrm2`                                                      | Prepared VGs across hosts; no established managed relocation workflow found for this contract                                                                         |
| [ZFS LocalPV](https://github.com/openebs/zfs-localpv) / [democratic-csi local zvol](https://github.com/democratic-csi/democratic-csi#zfs-local-datasetzvol) | Zvols and ZFS features                                                                                                                        | Pool preparation on every host and Talos ZFS support; transfer and Kubernetes placement still need orchestration                                                      |
| [HwameiStor](https://github.com/hwameistor/hwameistor)                                                                                                      | Explicit `LocalVolumeMigrate` resource                                                                                                        | The thin + raw-block + migration combination appears constrained; Talos/NixOS support is not established                                                              |
| [Longhorn](https://longhorn.io/docs/1.13.0/high-availability/data-locality/)                                                                                | Single-replica/locality modes and managed block storage                                                                                       | Locality, migration, and engine/version interactions need a concrete procedure and validation; remote attachment alone is not data relocation                         |
| [Mayastor](https://openebs.io/docs/4.1.x/user-guides/replicated-storage-user-guide/replicated-pv-mayastor/rs-configuration)                                 | One-copy configuration exists                                                                                                                 | NVMe-oF/SPDK and [io-engine hugepage requirements](https://openebs.io/docs/Solutioning/openebs-on-kubernetes-platforms/EKS/eks-isv) are a substantial host commitment |
| [Carina](https://github.com/carina-io/carina) / [Alibaba open-local](https://github.com/alibaba/open-local)                                                 | Additional local-storage managers surveyed                                                                                                    | No established cross-OS same-PVC relocation advantage found; not validated                                                                                            |
| [Ceph RBD](https://docs.ceph.com/en/latest/rbd/rbd-kubernetes/) / external SAN                                                                              | Logical block storage attachable across nodes                                                                                                 | Does not inherently keep stored data local to the workload node; different architecture                                                                               |

Hostpath quota provisioners are excluded by the `volumeMode: Block` requirement.
Backup/copy tools that populate a replacement claim do not by themselves satisfy
preserving the existing PVC.

### LINSTOR/Piraeus evidence

The [LINSTOR guide](https://linbit.com/drbd-user-guide/linstor-guide-1_0-en/)
documents single-copy placement, local-only accessibility, and resource migration
using `resource toggle-disk --migrate-from`. Migration synchronizes destination
storage before removing the source disk. These components still need testing as
one combined contract.

The [pool reference](https://piraeus.io/docs/v2/reference/linstorsatelliteconfiguration/)
supports node-specific configuration and `fileThinPool` on sparse-capable
filesystems. This may avoid repartitioning for a canary. The
[Talos integration](https://piraeus.io/docs/v2/how-to/talos/) requires a
kernel-matching DRBD extension and satellite configuration overrides. Exact
Talos and NixOS kernel/module compatibility is an adoption gate.

The [affinity controller](https://github.com/piraeusdatastore/linstor-affinity-controller)
handles placement changes after migration. Reviewed
[released `v1.3.0` code](https://github.com/piraeusdatastore/linstor-affinity-controller/blob/v1.3.0/pkg/operations/replace_pv.go)
preserves the CSI source and PVC claim reference, including the claim UID, while
retaining backing storage and recreating the PV. Same PVC/backend identity is
intended; the same PV object UID is not promised. End-to-end behavior is untested.

### HwameiStor feature-combination concern

Its [migration guide](https://hwameistor.io/docs/volumes/migrate) documents a
`LocalVolumeMigrate` resource and a convertible LVM class. The
[thin-volume guide](https://github.com/hwameistor/hwameistor/blob/main/docs/docs/volumes/volume_thin.md)
configures `convertible: "false"` and currently excludes thin multi-replica
volumes. The
[migration implementation](https://github.com/hwameistor/hwameistor/blob/main/pkg/local-storage/member/controller/volume_migrate_task_worker.go)
rejects non-convertible published raw-block volumes. Inference: the desired
thin + raw-block + migration combination is problematic; do not claim support
without resolving these constraints. Its
[host prerequisites](https://github.com/hwameistor/hwameistor/blob/main/docs/docs/install/prereq.md)
do not establish Talos/NixOS compatibility.

## Media selection and deployment decisions

Preserve media identity per backing pool while deciding whether to expose an
unrestricted class, strict SSD/HDD classes, or a preference mechanism.

With [`WaitForFirstConsumer`](https://kubernetes.io/docs/concepts/storage/storage-classes/#volume-binding-mode),
pod node selectors/affinity influence initial provisioning. They select a node,
not its SSD versus HDD pool. On mixed-media nodes such as `wyrm2`, a node label
advertising SSD availability is insufficient to guarantee the provisioner uses
SSD. One designated pool per node makes this mapping simple; multiple pools need
provider-supported selection, often separate StorageClasses.

A PVC's label `selector` is for existing PVs and
[prevents normal dynamic provisioning when nonempty](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#selector).
[`VolumeAttributesClass`](https://kubernetes.io/docs/concepts/storage/volume-attributes-classes/)
is driver-specific and is not a portable media selector. A universal class across
HDD and SSD hosts cannot promise SSD performance everywhere.

Host preparation belongs in the existing Talos machine configuration / metal
ToFu root and NixOS modules, with host-level backing-pool choices. Kubernetes
controllers, node agents, classes, monitoring, and snapshot integration belong in
cdk8s-generated Flux resources. Confirm existing ownership before adding any
provider. Establish capacity budgets alongside existing consumers, especially
on `optiplex`, roaming nodes, and the nearly full OVH HDD.

## Decision gates and canary acceptance

Piraeus and Rawfile are the leading candidates to compare. Prefer investigating
Piraeus first if managed same-PVC relocation warrants the extra components;
Rawfile remains the simpler candidate if that capability is optional. Provider
selection, backing budgets, media policy, and migration acknowledgment policy are
open decisions.

1. Verify pinned DRBD9 artifacts/modules for the exact Talos and NixOS kernels.
   Failure here requires a concrete integration path before proceeding.
2. Design a disposable cross-OS canary using file-backed pools on one Talos and
   one NixOS node, with explicit backing paths and budgets. Do not use existing
   claims or infer that directory free space is reserved capacity.
3. Provision thin `Block` and filesystem PVCs. Check physical allocation, hard
   capacity boundaries, usage reporting, expansion, discard/TRIM, mount failure
   behavior, reboot persistence, deletion, and cleanup.
4. In single-copy mode, sustain writes and `fsync` while other storage peers are
   unreachable, keeping the workload node and ordinary Kubernetes control-plane
   dependencies available. Verify no remote data flush is required. Distinguish
   existing mounted I/O from provisioning and new attachment operations.
5. Move a disposable volume in both directions. Record PVC UID, CSI volume handle,
   data integrity, physical placement, and final copy count. Interrupt transfers
   and controllers; verify source authority and recoverability until handoff.
   Repeat the local-only acknowledgment test after a completed move.
6. Validate online synchronization under continued writes: convergence, actual
   transfer bytes, application latency, write acknowledgment semantics, final
   outage, and source cleanup. Compare a stopped-workload procedure as a fallback.
7. Compare representative workload and `fsync` performance with Rawfile and the
   existing LVM setup. Use the results to decide whether relocation and operating
   effort justify the chosen provider; sequential throughput alone is insufficient.

Installation, module changes, repartitioning, PVC migration, and workload cutover
are outside this documentation change. Adoption requires observed canary behavior.
