# HDD thin-pool reclaim test, 2026-11-02

Two disposable 3 GiB ext4 PVCs tested the current OVH Piraeus HDD LVM_THIN
stack. The first used `piraeus-hdd-2` with synchronous copies on
`ovh-ns102453` and `ovh-ns103711`; the second used `piraeus-hdd-local` on
`ovh-ns102453` and ran an isolated SeaweedFS 4.46 server with its S3 endpoint
bound to pod loopback. No production objects or existing PVCs were modified.
Neither PVC had snapshots. Both filesystems were mounted without `discard`.

Values below are the individual thin LVs' `lvs data_percent`, multiplied by
3,225,419,776 bytes, their reported LV size. Percentages have two decimal places,
so MiB values are approximate and include DRBD and filesystem overhead. Pool-wide
percentages are much coarser and also include concurrent production activity.

## Direct replicated filesystem

| Phase                            | ovh-ns102453 | ovh-ns103711 |
| -------------------------------- | -----------: | -----------: |
| Fresh filesystem                 |     92.6 MiB |     28.6 MiB |
| After 512 MiB random file, fsync |    627.8 MiB |    563.8 MiB |
| After deleting file and sync     |    627.8 MiB |    564.1 MiB |
| After `fstrim /data`             |    116.0 MiB |     52.3 MiB |

Both replica states were UpToDate. Removing the file freed ext4 space but did
not return the data to either thin LV; explicit trim released approximately
512 MiB on each copy. `fstrim` reported 2.9 GiB submitted, which is not the
physical amount reclaimed.

## Isolated Seaweed S3 on a local Piraeus PVC

A loopback-only SeaweedFS server used the deployed 4.46 image, five 256 MiB
logical-volume slots, and a disposable bucket. An S3 PUT of 256 MiB of random
data succeeded, and HEAD verified its length. A DELETE returned successfully;
HEAD then returned 404. The test waited for all 64 chunks to be marked deleted,
then manually ran `volume.vacuum` for that collection before trimming the ext4
mount. The intentionally small five-volume limit also produced metadata-log
allocation errors in the isolated server; this was not a production load test.

| Phase                             | ext4 used | backing thin LV |
| --------------------------------- | --------: | --------------: |
| Empty server and bucket           |   0.9 MiB |       108.6 MiB |
| Object uploaded                   | 257.0 MiB |       361.1 MiB |
| Object deleted, before compaction | 257.0 MiB |       372.5 MiB |
| After collection vacuum           |   1.0 MiB |       372.5 MiB |
| After `fstrim /data`              |   1.0 MiB |       118.4 MiB |

After vacuum, the five Seaweed volumes contained only 40 bytes in total, but
LVM still retained their freed ext4 blocks. Trim released approximately
254 MiB from the thin LV. `fstrim` reported 2.9 GiB submitted, not reclaimed.

This demonstrates the delete → asynchronous chunk deletion → Seaweed vacuum →
filesystem trim chain for this disposable setup. It does not establish when the
production Seaweed cluster compacts or trims, whether it does so automatically,
or what happens with snapshots, retained object versions, replication, or other
object stores.

## Production automation, read-only inspection

The three production Seaweed HDD volume-server mounts were ext4 without the
`discard` mount option. The masters do not override the 30% garbage threshold
or one-vacuum-per-server default. SeaweedFS 4.46's
[master loop](https://github.com/seaweedfs/seaweedfs/blob/4.46/weed/topology/topology_event_handling.go)
scans on the leader, sleeping 14–16 minutes between scans; scans and compaction
can take additional time. On 2026-11-02 the leader's logs showed successful
vacuum commits on production HDD volume servers.

All three HDD nodes run Talos 1.14.0. `volumetrimschedules` was empty on each,
and their Talos `volumestatus` inventories did not include CSI PVCs. The
[Talos 1.14 trim scheduler](https://github.com/siderolabs/talos/blob/v1.14.0/internal/app/machined/pkg/controllers/block/volume_trim_schedule.go)
operates on eligible Talos-managed volume statuses, so simply enabling Talos's
global filesystem trim is not established to cover these CSI mounts. No
Kubernetes CronJob or running container command matched `fstrim` or Seaweed
vacuum; opaque scripts or another external trigger were not ruled out. We did
not observe an automated trim path for the Seaweed PVCs.

## Teardown

The namespace `reclaim-probe-20261102` contained only the two test pods and PVCs.
Both PVs were checked against their test claim UIDs and changed from `Retain` to
`Delete` before teardown. After deleting the namespace, both PVs and their
LINSTOR resources were absent.
