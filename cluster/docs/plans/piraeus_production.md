# Piraeus production rollout

Work for [storage issue #4717](https://github.com/agentydragon/ducktape/issues/4717),
[dedicated Talos system volumes #7462](https://github.com/agentydragon/ducktape/issues/7462)
and [safe OVH node lifecycle #7431](https://github.com/agentydragon/ducktape/issues/7431).
Remove completed work from this plan as it lands. The deployed design is in
[Piraeus block storage](../piraeus.md); node maintenance follows the
[node runbook](../runbooks/piraeus_node_roll.md).

## Status, 2026-10-26

Every OVH Talos node (`ovh-ns1001419`, `ovh-ns104952`, `ovh-ns104963`,
`ovh-ns103656`, `ovh-ns103711`, `ovh-ns102453`) and the OptiPlex run the split
system layout and a whole-data-disk Piraeus pool, and Terraform state matches
their live configuration. The OVH local-path classes and their records are gone,
and a `volume.fix.replication` dry-run finds no SeaweedFS volume short of two
copies after the `ovh-ns102453` reimage. Parked manifests under `cluster/parked/`
still name OVH local-path classes; repoint them to Piraeus on revival.

Remaining: close the operational gaps below, the replicated-class and
Seaweed-reduction work, and the separate Wyrm2/NixOS phase.

## Operational gaps

- **LINSTOR recovery:** export LINSTOR metadata off-cluster and rehearse a
  controller restore; establish a volume backup/restore path for strict-local
  claims whose application has no replica.
- **Alerts:** degraded or diskless-only resources and stalled resync, once the
  replicated class carries production data. Thin data/metadata alerts exist.
- **Thin-pool exhaustion:** production DRBD-pool and metadata exhaustion
  recovery has not been exercised; only disposable canaries have.

## Replicated class and Seaweed reduction

Track this separately in [#11753](https://github.com/agentydragon/ducktape/issues/11753).
Owner decisions, 2026-10-24: small single-writer Seaweed consumers move to
`piraeus-ssd-2-autodiskful`. CLIProxyAPI, Claude session sync, GitHub API proxy
capture and Matrix media have moved, and their old Seaweed claims were deleted
on 2026-10-26 with owner approval. The Haku UI WALs stay on
Seaweed until Haku UI is retired. Forgejo's shared Git volume and Grocy's backup
destinations stay on Seaweed.

Before more consumers move, run primary-node loss with quorum suspend-io, resync
and expansion on the class. Review Matrix and GitHub API capture's RWX needs
before choosing RWO for similar apps, and inventory direct S3 consumers as well
as PVCs. Keep a moved consumer's old claim until the owner releases it.

Goal, owner 2026-10-24: multi-writer (RWX) storage on Piraeus eventually, so
Forgejo-style shared volumes can leave Seaweed too. Candidates are linstor-csi's
NFS export (drbd-reactor failover) if the deployed version supports it,
otherwise a separate shared-filesystem design.

## Scheduling locality (later)

Goal, 2026-10-21: prefer nodes that already hold a diskful copy, with remote
attach as fallback. `allowRemoteVolumeAccess` controls who may attach, not a
soft preference. Upstream's separate `linstor-scheduler` extender provides that
preference, but its source pins Kubernetes scheduler libraries at v1.25.6 against
this cluster's v1.35.1 and needs mTLS LINSTOR client credentials; a
compatibility spike comes first (estimate 2026-10-21: 1–2 days, then 4–8 days to
deploy and validate). Measure Nebula latency and sustained throughput between
eligible nodes, including during DRBD resync, before setting the preference or
resync limits.

## Wyrm2 first; Atlas reinstall is a separate later decision

The owner wants Piraeus on Wyrm2 with **both SSD and HDD drives attached**.
First migrate storage while **keeping Wyrm2 virtualized on Atlas/Proxmox**.
Co-plan stable device attachment, pool sizing and data preservation within that
existing arrangement. Piraeus qualification and the application migration must
not depend on reinstalling Atlas or moving Wyrm2 to bare metal.

Separately, the owner reports little use of Atlas's other VMs and may later
reinstall Atlas with Wyrm2 as the top-level host OS. Revisit that architecture
only after the VM-based Piraeus migration is accepted. Preserve the distinction
between attaching/preparing explicit devices for the VM and erasing Atlas's
host or guest disks; no Atlas reinstall or whole-host wipe is approved here.

For the Piraeus-first phase, review inventory, SSD/HDD device attachment,
OpenEBS/application migration, capacity, recovery and host compatibility below.
The host-reinstallation parts of the worksheet are future joint planning, not
prerequisites for this first phase.

Prepare a concrete worksheet for joint review:

1. Inventory physical drives by serial/WWID, media, capacity and health; map
   Proxmox pools, VM disks, passthrough devices, encryption and every filesystem/LV
   to those drives. Check actual SSD/HDD connectivity and GPU/IOMMU needs rather
   than assuming the current VM layout maps cleanly to bare metal.
2. Inventory Atlas and every VM, including stopped guests, datasets, snapshots,
   host configuration and credentials. Identify unique authored data and current
   consumers; low VM usage does not establish that their disks are disposable.
   Include ActivityWatch and existing OpenEBS-backed Kubernetes claims on Wyrm2.
3. Propose explicit system/runtime capacity and separate Piraeus SSD/HDD pool
   budgets, device ownership and encryption/boot/unlock behavior. Show what fits
   using measured live bytes, copy staging space and growth reserve. Preserve
   home-site placement semantics; do not extend synchronous replication across
   the home/OVH link by default.
4. Specify how each retained dataset leaves the disks to be erased, where its
   verified recovery copy lives, how it returns and what application checks
   establish success. A backup on another VM disk in the same wipe set is not
   off-machine recovery. Establish staging capacity before choosing a wipe order.
5. Design installation/remote-console access, network and mesh identities,
   Kubernetes node identity, GPU workloads and the outage/rollback procedure.
   Decide whether Atlas's non-Wyrm2 services remain needed and where each runs.
6. Review the exact device map and migration/preservation procedure together
   before preparing the VM's Piraeus devices. Review any later host reinstall
   separately, including its full wipe set and recovery evidence. This plan
   grants no Atlas/Wyrm2 wipe approval.

Reuse the existing LINSTOR cluster and media StorageClasses after host/kernel,
DRBD, CSI and pool acceptance. Keep existing OpenEBS VGs under OpenEBS ownership
until their consumers are migrated and an explicit ownership handoff is accepted;
never initialize a Piraeus pool over a live OpenEBS device. Choose the VM's dedicated virtual disks versus physical-device passthrough
jointly. Keep the later Proxmox retirement decision independent of both this
VM-based migration and the current OVH work.

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

## NixOS rollout

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

## Acceptance for later remote access, replication and automatic movement

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
