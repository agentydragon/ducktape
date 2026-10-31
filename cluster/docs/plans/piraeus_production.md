# Piraeus production rollout

Work for [storage issue #4717](https://github.com/agentydragon/ducktape/issues/4717),
[dedicated Talos system volumes #7462](https://github.com/agentydragon/ducktape/issues/7462)
and [safe OVH node lifecycle #7431](https://github.com/agentydragon/ducktape/issues/7431).
Remove completed work from this plan as it lands. The deployed design is in
[Piraeus block storage](../piraeus.md); node maintenance follows the
[node runbook](../runbooks/piraeus_node_roll.md).

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
The Haku UI WALs stay on Seaweed until Haku UI is retired. Forgejo's shared Git
volume and Grocy's backup destinations also remain on Seaweed.

The remaining replicated-class acceptance gap is expansion. Test it before more
consumers move (see [the deployed design](../piraeus.md) and
[failover results](../../debug/drbd_canary/failover/results.md)). Review Matrix
and GitHub API capture's RWX needs before choosing RWO for similar apps, and
inventory direct S3 consumers as well as PVCs. Keep a moved consumer's old claim
until the owner releases it.

Longer-term goal: multi-writer (RWX) storage on Piraeus, so Forgejo-style shared
volumes can leave Seaweed too. Candidates are linstor-csi's NFS export
(drbd-reactor failover) if the deployed version supports it, otherwise a
separate shared-filesystem design.

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

## Wyrm2 and Atlas follow-ups

The deployed Wyrm2 and Rugged pool layouts, including Atlas's storage risks, are
in [Piraeus host-specific storage](../piraeus.md#nixos-worker-pools).

Remaining; deletions need the owner's approval at the time:

- **Alert** on `tank` and `rpool` free space, which LINSTOR's thin accounting
  cannot see.
- **Retirement:** detach and destroy `virtio0`, `virtio2` and `virtio6` after
  checking their contents and confirming the owner's approval at the time.

## Atlas reinstall (later, separate decision)

The owner may later reinstall Atlas with Wyrm2 as the bare-metal host OS. That
is not approved here and is planned only after the VM phase is accepted.

Data in the Piraeus zvols survives as long as its pool does: a bare-metal host
can import `tank` and give LINSTOR the same zvol under node name `wyrm2`, and
the VG reappears. The SSD zvol is on `rpool`, which a reinstall onto that NVMe
would erase unless the pool is kept; otherwise drain it first. Before then,
inventory Atlas's other VMs (including stopped VM 100 `wyrm`, 725 GB) for unique
data, keep recovery copies off the wipe set, and plan remote console, Nebula and
Kubernetes node identity, GPU passthrough replacement and rollback.

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

- [ ] **Iguana:** enable the same host module and admit it individually after
      confirming kernel/DRBD compatibility and testing suspend/disconnect behavior.
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
