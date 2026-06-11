# SeaweedFS Filer Metadata Loss on Pod Roll (leveldb2 → postgres2)

**Date**: 2026-05-27
**Status**: Resolved — filer is now CNPG-backed (commit `c048cbf0e`).

## Root Cause

The SeaweedFS filer was configured with `[leveldb2]` in `spec.filer.config`,
storing metadata at `./filerldb2` inside the pod's writable layer. The
`FilerSpec` had no `Persistence` field. Every filer pod restart wrote to
the ephemeral container filesystem, so any roll lost the bucket directory
tree even though the underlying `.dat` chunks in the volume servers
survived.

The `chrislusf/seaweedfs:3.93` → `:4.29` image bump (needed to fix
Bucket CR adoption via the IAM gRPC service that 4.x registers
unconditionally) rolled the filer StatefulSet on Flux reconcile and wiped
every bucket's metadata.

## Blast Radius

After the wipe, `fs.ls /buckets` returned **empty**:

- 6 Bucket CRs reported `Ready` with stale `USEDBYTES` (operator computes
  these from volume-server reports, not filer metadata — so the CR status
  looked healthy while the bucket dirs no longer existed).
- All 6 buckets' object data was **orphaned**: chunks remained in volume
  servers, but no filer entries pointed at them, so they were both
  invisible and uncollectible.
- `https://augur.allegedly.works/assets/ashton.jpg` returned 404 immediately
  after the upgrade.
- attic, loki, mimir-blocks, mimir-ruler, tempo writers eventually
  reconnected and recreated their own bucket dirs lazily — but anything
  already-written was orphaned.

## Resolution

1. **Re-seeded augur-assets** from the source-of-truth copy at
   `gaffer-private/k8s/augur/assets/*.jpg` via `aws s3 cp` against the
   port-forwarded SeaweedFS s3 endpoint. Bucket dir had to be created
   first via `weed shell fs.mkdir /buckets/augur-assets` because the s3
   gateway's auto-bucket-create races the identity actions.
2. **Migrated filer to postgres2** backed by a new CNPG cluster
   (`seaweedfs-filer-db`, OVH-HA profile per `cnpg_conventions.md`).
   Credentials wired via env vars `WEED_POSTGRES2_USERNAME` /
   `_PASSWORD` exploiting viper's `WEED_` env prefix with
   dot→underscore replacement (`weed/util/config.go`).
3. Other tenants' bucket dirs (attic, mimir-ruler, tempo) repopulate on
   their own when their writers reconnect; loki recreated immediately
   because it had active write traffic.

## What Now Prevents Recurrence

- **Filer metadata is on persistent storage** (postgres2 on a CNPG
  cluster with its own PVCs and streaming replication).
- **Image bumps no longer destroy state**: the filer pod can be rolled,
  recreated, scheduled on a different node, etc. without metadata loss.
- The `volumeServerDiskCount` + per-node PVC story for volume servers
  was already correct (1800Gi PVCs on `/dev/sdb`); the filer was the
  one missing piece.

## Still Open

- **No WAL/PITR archiving** on `seaweedfs-filer-db` (matches the
  cluster's overall posture — no OVH-HA CNPG has off-cluster backup
  yet). Gates scaling the filer to 2+ replicas.
- **Volume-server orphan vacuum**: the pre-cutover `.dat` chunks from
  wiped buckets are still on disk. Reclaim via `weed shell volume.vacuum`
  per orphaned collection when convenient.

## Misdiagnoses (kept for the record)

1. **"Operator stamps owner=''":** initial theory blamed an operator bug.
   Real cause was the 3.93 filer not registering the IAM gRPC service.
2. **"loki failed because of 0o471 mode":** the operator never checks
   mode; `bucket_controller.go:174` only inspects `bucket.Status.BucketName`.
   The fix was a status-subresource patch, not a chmod.

Confirmed by reading seaweedfs-operator 1.0.19 + seaweedfs 3.93/4.29
sources locally cloned at `~/code/seaweedfs-operator` and `~/code/seaweedfs`.
