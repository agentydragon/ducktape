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

XFS retains the current filesystem and project-quota capability; it is not a
Piraeus requirement. Its inability to shrink means moving capacity from this LV
to the thin pool later still requires evacuation/recreation or removing the LV
after its consumers migrate. LVM makes unallocated space reusable without changing
partition boundaries; it does not make XFS shrinkable. Ext4 is an alternative if
offline shrinking becomes a requirement, with a separately tested maintenance path.

All sizes below are **GiB**, including the converted physical device capacities.
Thin-data sizes remain starting acceptance budgets, not final workload sizing or
a promise that Forgejo's current 200 GiB claim fits. Reserve 1 GiB thin metadata
and 1 GiB metadata spare initially; free extents are approximate after that allowance
and before small VG overhead. Recheck actual geometry during allocation.

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
filesystem/mount. A separately reviewed bootstrap creates `data_ssd/piraeus` or
`data_hdd/piraeus` with the fixed initial thin-data/metadata budgets. Talos's
`LVMLogicalVolumeConfig` supports ordinary/RAID LVs, not thin-pool creation;
do not declare the thin pool as a linear LV. LINSTOR owns the thin volumes inside
that pool. Verify shared-VG discovery, locking, reboot ordering and reconciliation
on disposable storage before using this division of ownership.

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
requires the backing thin pool to exist in this mode. Pool creation/extension
ownership and repeatable bootstrap are part of the LVM canary deliverable; do not
claim this registration manifest creates the proposed 64/128 GiB pool itself.

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
      production disk conversion. Verify host LVM reconciliation preserves LINSTOR
      volumes, locking and activation work across reboot, and mount readiness gates consumers.
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
