# Reclaim orphaned SeaweedFS chunks

Use when a bucket's volume bytes exceed what its filer entries account for. Orphans
are needles on a volume server that no filer entry references. Observed on the
`forgejo` bucket (replication `001`): 68 GB of 4 MiB chunks, all on one replica,
written over a few weeks and never deleted.

All `weed shell` commands run on `seaweedfs-master-0` with
`-master=localhost:9333 -filer=seaweedfs-filer.seaweedfs.svc:8888`. `volume.fsck`,
`volume.check.disk` and `volume.vacuum` need `lock` first and `unlock` after; a dropped
`kubectl exec` stream releases the lock when its lease lapses.

## Size the problem

`collection.list` reports a collection's size as the average of its replicas, so
diverged replicas hide the gap. Compare `fs.du /buckets/<bucket>` (live bytes) with the
larger replica of each volume from `volume.list`.

```text
volume.fsck -collection <bucket> -v
```

Read-only. Per-volume output shows `orphan:N <pct>% <bytes>`; the filer is read from `/`
because any path can reference these volumes. It skips needles newer than
`-cutoffTimeAgo` (default 5h).

## Purge

```text
volume.fsck -collection <bucket> -reallyDeleteFromVolume
```

This purges only orphans present on **every** replica. A volume whose orphans exist on
one replica is skipped silently, with no warning in the output.

For the skipped volumes, first dry-run `volume.check.disk -slow -syncDeleted -v -volumeId N`
(no `-apply`). A one-sided divergence is safe to purge when it reports that the lagging
replica holds no unique live data and the number of needles missing there equals fsck's
orphan count for that volume. Never `-apply` check.disk or `volume.fix.replication` to
"repair" it: they would copy the garbage onto the lagging replica, or resurrect deleted
needles. Then purge per volume:

```text
volume.fsck -collection <bucket> -volumeId N -reallyDeleteFromVolume -forcePurging
volume.vacuum -collection <bucket> -volumeId N -garbageThreshold 0.3
```

Purge one volume first and verify it before the rest:

```text
volume.fsck -collection <bucket> -volumeId N -v
volume.fsck -collection <bucket> -findMissingChunksInFiler -volumeId N
```

Expect `no orphan data` and no missing chunks. Free space on the volume servers moves
only as vacuums finish.

## Timeline of the orphans

To date the orphans, run `weed export -dir=/data0 -collection=<bucket> -volumeId=N` in
the volume server pod. It lists each needle's key, size and `modified` time with no
output file; match its keys (the file id without the final 8 cookie hex digits) against
the fsck orphan list.
