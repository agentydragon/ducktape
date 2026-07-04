# Runbook: migrate a SeaweedFS RWX PVC across StorageClasses (read-zero-downtime)

A PVC's `storageClassName` is immutable, so moving data between SeaweedFS classes (e.g.
`seaweedfs-ovh` HDD → `seaweedfs-ovh-ssd` NVMe) is a **copy-cutover**: new PVC, copy, repoint
the consumer. Done first for Forgejo git → SSD (OVH storage tiering Phase 3, 2026-07-04).

Use **VolSync rsync-TLS** (the cluster's PVC-migration tool, cf.
`k8s/grocy/*/app/volsync-backup.yaml`) with a **pre-seed**, and cut over
**read-zero-downtime**: reads/clones stay up throughout, only writes freeze for ~1–2 min.

**Why a write-freeze is unavoidable:** a consistent cross-class swap needs a quiescent
point — a write landing after the final sync but before the repoint would split-brain the
two PVCs (old has it, new doesn't). Reads never have to drop, but writes must pause. On a
personal cluster this is coordination ("hold pushes"), optionally hardened by blocking the
write path at the HTTPRoute (note: raw-TCP/L4 paths like SSH-on-2222 can't be selectively
write-blocked — coordinate the window).

## Prerequisites

- The consumer's Deployment uses **`RollingUpdate` with `maxUnavailable: 0`** (+ a PDB) so
  the repoint keeps ≥1 replica serving. Verify before starting.
- `moverSecurityContext` UID/GID must match the data's file ownership (e.g. Forgejo git =
  `1000`), or the copied files won't be usable.
- `copyMethod: Direct` — no `VolumeSnapshotClass` exists for SeaweedFS, so the source mover
  bind-mounts the live PVC over RWX (a torn copy during pre-seed is corrected by the final
  sync).

## Procedure

1. **Scaffolding (committed, inert):** dest PVC on the target class + a `ReplicationDestination`
   (receiver) + a `ReplicationSource` shipped `paused: true`. Mover pinned to the storage's
   zone (`topology.kubernetes.io/zone=hil-ovh`) — the SeaweedFS CSI only runs there.
2. **Pre-seed (zero downtime):** set the source `paused: false` → rsyncs the bulk while the
   app serves. Verify volumes appear on the target servers (`volume.list`). Note: many small
   files (git objects) make rsync per-file-bound — the first pass can take a while.
3. **Write-freeze:** hold writes (coordinate; reads keep working).
4. **Final delta sync:** bump the source `trigger.manual` → fast delta against the quiesced
   source. Verify (e.g. repo count + a `git fsck`/checksum sample on the dest).
5. **Rolling repoint (reads stay up):** commit the consumer's `claimName` → the new PVC. The
   `RollingUpdate` brings up new pods on the new PVC before terminating old ones; with writes
   frozen both PVCs hold identical data, so there's no split-brain. **Do NOT scale to 0.**
6. **Unfreeze + verify:** allow writes; confirm read+write end-to-end, the new PVC `Bound` on
   the target class, and data on the target servers.
7. **Cleanup (after a few days):** delete the old PVC + the three VolSync objects. Rollback
   before cleanup = revert the `claimName` commit (the source PVC is untouched by `Direct`
   reads).

## Gotcha — verifying a git push from a partial clone

Testing a Forgejo push from the ducktape **blobless/partial clone** can fail with
`unpack-objects: eof before pack header` — that's the **client** failing to build the pack
because `pack-objects` couldn't lazily fetch a missing object from its github promisor (e.g.
a global `GIT_SSH_COMMAND -p 2222` forced the github fetch onto the wrong port). Not a server
fault. Use per-host SSH config, not a global port override.
