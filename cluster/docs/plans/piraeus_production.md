# Piraeus production rollout

Proposed work, based on the [2026-10-19 read-only inventory](../../debug/piraeus_inventory_2026_10_19/README.md).
This PR changes documentation and evidence only. Rollout, disk conversion and
application migration require subsequent scoped changes. Remove completed work
from this plan as it lands.

## Target behavior

Extend the existing Flux-owned operator and LINSTOR cluster, with CSI/satellites
on every prepared Talos and NixOS node. Pool membership follows actual media;
every node need not supply both tiers. Preserve the same class names across
node types and permit remote consumption on prepared nodes, subject to the
explicit site policy below.

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

Use LVM_THIN for the production target. Our accepted performance and movement
tests used FILE_THIN; they do not validate LVM_THIN or its failure behavior.
[LINBIT describes File/FileThin as mainly demonstration/experiment backends](https://linbit.com/drbd-user-guide/linstor-guide-1_0-en/).
Keep the existing file-backed canary separate during transition, and do not
silently mix backend types or reuse its discard tuning without new measurements.

## Proposed allocation and order

The following **starting acceptance budgets** support a sparse 50 GiB test volume.
They are not final capacity reservations for production workloads or a promise
that Forgejo's current 200 GiB claim will fit. Size application pools from
measured data, growth and resync headroom before a migration; choose any logical
overcommit explicitly and test physical-exhaustion behavior.

| Node               | Proposed first local pool                                      | Allocation route and prerequisite                                                                                                                                                                                                                                                         |
| ------------------ | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ovh-ns1001419`    | SSD: 64 GiB thin data in a 96 GiB raw region                   | First SSD candidate: only 12.06 GB observed data use; evacuate its SeaweedFS data and study-casino replica, then rebuild the **data disk** layout. Leave the install/etcd disk untouched.                                                                                                 |
| `ovh-ns104952`     | SSD: 64 GiB thin data in a 96 GiB raw region                   | Repeat after the first SSD succeeds. Preserve/restore legacy XFS for SeaweedFS, CNPG and Valkey.                                                                                                                                                                                          |
| `ovh-ns104963`     | SSD: 64 GiB thin data in a 96 GiB raw region                   | Repeat after replica health is restored; its testing PostgreSQL singleton requires an accepted outage or prior migration.                                                                                                                                                                 |
| `ovh-ns102453`     | HDD: 128 GiB thin data in a 192 GiB raw region                 | First HDD candidate: DRBD already prepared and the 4 TB data device has the most headroom. Evacuate/restore SeaweedFS, ClickHouse and all local PVC consumers before rebuilding its data layout.                                                                                          |
| `ovh-ns103656`     | HDD, size pending legacy capacity budget                       | Its 1890 GiB local-path requests exceed the roughly 1862 GiB data filesystem. Agree growth/reprovisioning limits before taking capacity away.                                                                                                                                             |
| `ovh-ns103711`     | HDD, size pending legacy capacity budget                       | Its 2368 GiB local-path requests exceed the roughly 1862 GiB data filesystem; this includes live 1800 GiB SeaweedFS and 500 GiB ClickHouse claims. Same capacity gate.                                                                                                                    |
| `wyrm2`            | New dedicated SSD and HDD virtual disks, initially 96 GiB each | Resolve Atlas read access/headroom and actual media, choose unused slots, then attach. Each can initially hold a 64 GiB thin pool plus metadata/reserve. Existing OpenEBS VGs stay with OpenEBS.                                                                                          |
| `optiplex`         | SSD after adding capacity                                      | Prefer an additional SSD if the hardware permits; otherwise plan a larger-NVMe replacement with backup/restore. The sole current 256 GB disk has only 60.75 GB filesystem free, insufficient for a new 96 GiB region without reducing existing usage. Client enrollment can precede this. |
| `iguana`, `rugged` | Local SSD pools after an online inventory                      | First enroll as clients after kernel validation. Establish dedicated capacity, encryption/unlock ownership and suspend/disconnect behavior before adding local pools.                                                                                                                     |

The raw-region budget includes VG metadata and unallocated extension reserve;
do not allocate its full capacity as thin data. Keep at least 25% of each new VG
unallocated after thin data/metadata creation. The exact device geometry and
thin-metadata sizes belong in each allocation change, verified before formatting.

For OVH, retain a legacy XFS partition at the existing mount path and carve a
separate raw LVM region on the **evacuated data device**. Talos owns partition
layout/mounts; LINSTOR owns its dedicated VG/thin pool. Use stable identities from
the inventory, explicit bounded sizes and one owner per region. Validate this
layout on a disposable disk first. Do not point LINSTOR automatic device setup at
an occupied whole disk or use an unsupported in-place filesystem shrink.

The SSD proposal leaves roughly 323 GiB legacy capacity on each 450 GB data
device, compared with 311/316 GiB of current declared local-path requests.
Those margins are small relative to declared requests; actual usage is much
lower, but it is not a durable capacity reservation. Recheck current usage and
growth, verify the preserved XFS size, and enlarge the legacy budget or reduce
the proposed Piraeus allocation if that gate fails. Do not force the example size.
The two smaller HDD nodes remain consumers until their capacity budget is settled;
the rollout is not complete until compatible local capacity is arranged there.

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

- [ ] **Host preparation:** generalize
      `cluster/terraform/main/drbd-canary.tf` into opt-in DRBD preparation for OVH
      and home Talos nodes. Preserve every node's existing extension/kernel settings.
      Add `dm-thin-pool` and required snapshot support for LVM_THIN. Roll one node at
      a time, control planes serially, using the existing Talos runbook and application
      restoration gates. Verify readiness before activating its Piraeus eligibility.
- [ ] **NixOS support:** add a shared opt-in host module with a DRBD 9 module built
      for each running kernel, utilities and udev integration; keep resource ownership
      in LINSTOR. The earlier build targeted 6.18.55 while wyrm2 still runs 6.18.54.
      Build/inspect a matching module or use a scheduled kernel roll; do not assume
      either reboot-free activation or compatibility with rugged's last-known 7.2 kernel.
- [ ] **Provider scope:** extend `cluster/cdk8s/piraeus.py`, preserving one operator
      and one LINSTOR owner. Separate stable controller placement, prepared-node CSI/
      satellite eligibility, OS-specific configuration and per-node pool selection.
      Add four explicit classes, suitable host-network/Nebula firewall reachability,
      a port allocation range larger than the 20-port canary, and protected monitoring.
      Account for control-plane/roaming taints and temporarily offline satellites.
- [ ] **LVM canary:** prove the bounded raw-region/legacy-XFS layout, NixOS/Talos
      interoperability and pool ownership before production disk conversion. Dedicated
      temporary virtual capacity is an alternative if Atlas access is restored first.
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

The Talos integration follows the [upstream Piraeus Talos guide](https://piraeus.io/docs/v2/how-to/talos/).
The repository's [canary preparation](../drbd_canary.md),
[canary observations](../../debug/drbd_canary/results.md),
[auto-diskful observations](../../debug/drbd_canary/auto_diskful/results.md), and
[benchmark report](../../debug/piraeus_storage_bench/README.md) are supporting evidence.

## Acceptance before admitting production data

- [ ] On each OS/tier, create a sparse 50 GiB volume; measure physical allocation,
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
- [ ] Demonstrate no unintended WAN synchronous peer or laptop placement, and
      record what happens when a client lacks the requested local media or free space.

Outstanding inputs are Atlas read access/backing headroom, smaller-HDD legacy
capacity budgets, OptiPlex's hardware allocation, online laptop inventory and the
two-copy site/locality policy. They block their respective allocation steps, not
the independent host-preparation and provider changes.
