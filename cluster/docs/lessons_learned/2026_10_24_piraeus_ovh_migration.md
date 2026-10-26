# Piraeus OVH migration

Between 2026-10-21 and 2026-10-24 all six OVH Talos nodes moved from local-path
data disks to whole-disk Piraeus pools, one node at a time: the three SSD control
planes, then the HDD workers `ovh-ns103656`, `ovh-ns103711` and `ovh-ns102453`.
Each node was evacuated, reset with a split system layout, wiped and readmitted,
and its applications were restored from native replicas. The procedures that
worked are in the [node runbook](../runbooks/piraeus_node_roll.md); the design is
[Piraeus block storage](../piraeus.md).

## What cost the most

- **Seaweed evacuation dominated the timeline.** Native evacuation and repair ran
  at about 15 MiB/s over the Nebula mesh, so moving hundreds of GiB per HDD node
  took most of a day. The owner waived two-copy durability twice: for
  `ovh-ns103711`, and for the last node `ovh-ns102453`, which was reimaged with
  430 volumes temporarily single-copy and healed onto its new server afterward.
  Decide such waivers up front; they turn a day of copying into hours.
- **`volume.fix.replication` stopped silently.** SeaweedFS 4.46 exited with
  `lock is lost` after losing its renewable admin lock, and Kubernetes reported
  only `BackoffLimitExceeded`. A server added mid-run was also absent from the
  repair's topology snapshot. Check `GetAdminLockStatus` and real inventories,
  not Job status.
- **Stale FUSE mounts outlived their volume server.** A retired-but-alive Seaweed
  server proxies reads, so a client never re-resolves; a rolling restart that
  lands on the same node keeps the CSI mount staged. Scaling the consumer to zero
  and back respawned the mount. S3 gateways needed a serial refresh for the same
  reason.
- **Operators reacted to maintenance holds.** CNPG switched primaries away from a
  cordoned node on its own, so primaries had to move serially before cordoning.
  The Redis operator copied a Flux hold annotation into the Pod template and
  rolled the serving master. Hold the owning Kustomization, or use the operator's
  own pause, instead.

## What went right

- Strict-local, single-copy Piraeus classes under applications that already
  replicate (CNPG, Seaweed, ClickHouse, Valkey, Mimir/Loki) kept each node's
  conversion independent: no Piraeus-level resync, and every restore was the
  application's own native rebuild.
- Retain on every claim and UID-guarded deletes meant no PVC removal ever took
  data with it. Erased files were never mistaken for a backup because each
  handoff named its off-node recovery source before the wipe.
- Terraform stayed out of the destructive path. Each node was reset and applied
  through the Talos maintenance API, then reconciled with a targeted
  `refresh=false` plan whose live-versus-rendered diff was reviewed before
  apply. Nodes rebuilt this way leave Terraform state stale until that targeted
  apply; a later full plan showed only state drift, not node changes.

## Leftovers

Released and orphaned local-path records accumulated on every wiped node: 35 were
deleted on 2026-10-24 after checking each was Released, or Bound with no consumer
and no GitOps declaration. Claim names reused by replacement claims make a
Released PV look in use; compare the PV's `claimRef.uid` with the current claim
before trusting a name match. The OVH local-path classes were removed on
2026-10-25 once no claim used them.
