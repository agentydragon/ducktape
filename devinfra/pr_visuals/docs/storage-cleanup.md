# Storage cleanup

The publisher has no automatic retention policy. A one-time review cleanup expires
historical GitHub image and gallery links; choose that history window explicitly.
Retain current baselines and active review dependencies regardless of age.

## Establish the deletion set

1. Record a fixed UTC cutoff and verify the physical bucket, Bucket UID, versioning,
   object-lock status, writer identity, credential Secret reference, and Flux owner.
   For the current deployment these are `pr-visuals`, `flux-system/pr-visuals`,
   `pr-visuals-writer`, `flux-system/pr-visuals-s3-credentials`, and
   `ducktape-flux/seaweedfs-pr-visuals-bucket`. Recheck them before each operation;
   never print credential values or change ownership as part of data cleanup.
2. Save a complete S3 listing with key, size, modification time, and ETag. Group
   `commits/<sha>/` and `reviews/<sha>/` objects by their full commit SHA. A bundle
   is old only when its newest object predates the cutoff. Keep unknown prefixes.
3. Protect every commit reached by legacy and new baseline pointers
   (`baselines/<slug>.json`, `baselines/reviews/<slug>.json`), open PR heads and
   current merge bases, and recorded `base_sha`/`merge_base_sha` dependencies.
   Follow dependencies of retained metadata until no new protected commits appear.
   Also protect active CI/publisher work and recently closed PR heads that may
   still be publishing. A publish workflow run's own `head_sha` can identify the
   workflow source rather than the triggering CI commit; inspect the triggering
   run as well. Refresh these roots during cleanup.
4. Review the exact selected bundle prefixes, object counts, and bytes. Recheck
   protection roots and publication markers immediately before each deletion.
   Stop on incomplete listings, failed reference reads, malformed metadata, or
   evidence of renewed publication. An online scan is not an atomic snapshot.

Keep all baseline pointer objects and shared `assets/` images during this legacy
bundle cleanup. A shared image may be old and still referenced by a recent review.
Even an unreferenced old image can be reused by a publisher before the new review
metadata is visible. Age grace periods alone do not make image deletion safe;
automated shared-image collection needs coordination with publishers.

## Delete selected bundles

S3 `DeleteObjects` supports per-key ETag guards in SeaweedFS 4.46. Check the deployed
version's conditional-delete behavior before relying on it. It gives finer guards
than native directory removal but was much slower for this bucket's millions of
small objects.

For a reviewed whole-bundle set, SeaweedFS 4.46's `weed shell` `fs.rm -r` deletes
filer metadata and backing object data recursively. Connect to the verified
cluster, for example:

```bash
kubectl -n seaweedfs exec -i seaweedfs-filer-0 -- \
  weed shell -master=seaweedfs-master:9333 -filer=localhost:8888
```

Pass only approved, full SHA directories below the one authorized bucket, such as
`fs.rm -r /buckets/pr-visuals/commits/<approved-full-sha>`. Never pass the bucket
root, wildcard paths, or `-f`. Native directory deletion has no per-object ETag
guard; use it only after checking for concurrent writers to each selected bundle.
The shell can print `rm:` errors while exiting successfully: inspect its output
as well as its exit status, and stop on recursive/RPC errors. Journal each batch's
exact prefixes before starting and after successful completion. Resolve partially
completed batches from a fresh listing before resuming.

Then list the bucket again. Verify selected prefixes are absent and protected
objects are still present. Resolve live baseline pointers, read their target
metadata and candidate PNGs, check PNG signatures, and verify available content
hashes. These checks supplement the full protected-key comparison.

## Reclaim volume storage

Deleting S3 objects does not immediately release disk space. `collection.list`
reports collection storage before subtracting `deletedBytes`; the bucket operator
copies its size and record counters. Collection sizes normalize replicas, whereas
node filesystem usage includes actual copies and other collections. The operator's
`objectCount` is not a live S3 key count.

Before compaction, check volume-server health, replica availability, and free disk
space for rewriting retained data. In the same verified `weed shell`, record
`collection.list`, then run:

```text
lock
volume.vacuum -collection=pr-visuals -garbageThreshold=0.3
unlock
```

Use the explicit collection filter. Compare fresh counters after volume heartbeats
propagate; the Bucket CR usage refreshes separately. If deleted bytes remain,
inspect `volume.list` and master logs. The collection sweep can skip read-only
volumes. SeaweedFS 4.46 supports explicit `-volumeId=<comma-separated-ids>` requests,
but first verify each ID belongs to this collection and investigate unhealthy
disks or missing replicas. Do not make a volume writable merely to force vacuum.
Volumes below the garbage threshold can remain for normal maintenance.

After compaction, repeat baseline image reads and check workload readiness. Record
S3 payload totals and collection-size reduction separately; ongoing publication
means a net size change is not exact accounting of this operation alone.

## Measured cleanup, 2026-10-19

The approved one-time cutoff was `2026-10-16T03:57:48Z`. No recurring expiry rule
was installed and shared images were not deleted.

| Observation                                          | Result                |
| ---------------------------------------------------- | --------------------- |
| Expired unprotected bundles deleted                  | 8,726                 |
| Deleted S3 objects / payload                         | 3,287,493 / 328.29 GB |
| Remaining S3 objects / payload                       | 450,550 / 46.44 GB    |
| Protected original objects verified present          | 69,549                |
| Live baseline pointers and sample PNGs checked       | 87                    |
| Collection storage before / after compaction         | 378.05 GB / 49.44 GB  |
| Net collection-size reduction, before replica copies | 328.61 GB             |

Deletion completed without reported errors. The first compaction pass reclaimed
only part of the deleted storage; a second pass explicitly selected the remaining
12 high-garbage volume IDs after verifying both replicas. Their skip reason was
not confirmed. Post-compaction baseline reads passed, all 18 running SeaweedFS
pods were Ready, and the Bucket CR confirmed 49,439,272,928 bytes at
`2026-10-19T04:34:05Z`. These measurements are historical, not current usage.

Verified implementation references for that deployment:
[recursive deletion](https://github.com/seaweedfs/seaweedfs/blob/4.46/weed/shell/command_fs_rm.go),
[vacuum command](https://github.com/seaweedfs/seaweedfs/blob/4.46/weed/shell/command_volume_vacuum.go),
[volume sweep](https://github.com/seaweedfs/seaweedfs/blob/4.46/weed/topology/topology_vacuum.go),
[collection counters](https://github.com/seaweedfs/seaweedfs/blob/4.46/weed/shell/command_collection_list.go),
and [operator usage](https://github.com/seaweedfs/seaweedfs-operator/blob/seaweedfs-operator-0.1.42/internal/controller/bucket_usage.go).
