# SeaweedFS CSI driver — Tana-workload benchmark

The CSI driver (chart v0.2.21 / image `chrislusf/seaweedfs-csi-driver:v1.4.14`,
FUSE-backed) was stood up to evaluate whether `seaweedfs-ovh` is suitable
storage for a Chromium-profile workload (`tana-mcp`'s `~/.config/tana`,
~200 MB, heavy small writes + sync from SQLite/IndexedDB).

The companion install lives at <../k8s/seaweedfs-csi/>; see
<seaweedfs_trial_baseline.md> for SeaweedFS hardware/topology.

## Test setup (2026-05-23)

Both PVCs bound to the same OVH kimsufi worker (`talos-kimsufi-worker-1`),
so the only variable is the storage class. Identical `fio` jobs against
each, run in `swfs-bench` namespace.

Hardware (per <seaweedfs_trial_baseline.md>): OVH KS-5 dedicated server,
Intel Xeon E3-1270 v6 (4c/8t), 32 GB RAM, 2× 2 TB SATA spinning HDD in
JBOD. Talos uses `/dev/sda` for system; user data (both the local-path
hostpath and the SeaweedFS volume server) lives on `/dev/sdb`, XFS,
~1.95 TB usable. So both storage classes ultimately hit the same
rotating disk — the numbers below are about the layers on top.

| PVC             | StorageClass     | Backing                                  |
| --------------- | ---------------- | ---------------------------------------- |
| `bench-ovh`     | `local-path-ovh` | XFS on `/dev/sdb` of the kimsufi box     |
| `bench-seaweed` | `seaweedfs-ovh`  | FUSE → in-cluster filer → volume servers |

## Results

| Workload                                 | local-path-ovh                     | seaweedfs-ovh                            |
| ---------------------------------------- | ---------------------------------- | ---------------------------------------- |
| Sequential write, 256 KB, 512 MB         | **156 MB/s, 609 IOPS**             | 30.7 MB/s, 120 IOPS                      |
| Sequential read, 256 KB                  | **145 MB/s, 566 IOPS**             | 64 MB/s, 249 IOPS                        |
| Random read, 4 KB, 4 jobs, 30 s          | 12 MB/s/job, p99 56 ms (real)      | 1.4 GB/s/job, p99 2 µs (FUSE page cache) |
| Random write, 4 KB w/ `fsync=4`          | 56 MB/s, p99 68 µs, **p99.9 1 ms** | 44 MB/s, p99 51 ms, **p99.9 5 000 ms**   |
| Synchronous 4 KB writes (`sync=1`, qd=1) | 251 IOPS, p99 33 ms, p99.9 87 ms   | **43 IOPS, p99 5 000 ms**                |

The seaweedfs random-read win is illusory — those 2 µs latencies are FUSE
page-cache hits; with the working set rotating (which the Tana profile
does) the same reads have to round-trip through the filer → volume server.

## Verdict

`seaweedfs-ovh` is **not** suitable for the Tana workload. The killer is
fsync tail latency: a p99.9 of ~5 s on small synchronous writes would
surface as Chromium UI freezes and IndexedDB transactions stalling, because
Tana's profile fsyncs Cookies / DIPS-wal / IndexedDB sqlite on every change.
A FUSE-over-network filesystem fundamentally can't serve SQLite-style
sync writes the way a local filesystem can.

The CSI driver is kept installed for blob-shaped workloads where SeaweedFS
is the right tool (large, mostly-append, no fsync-per-update). Tana stays
on `local-path-ovh`, with a periodic Volsync backup _into_ seaweedfs (which
plays to the backend's strengths).
