# SQLite Storage Benchmark

Status: completed 2026-07-07.

This report answers [ducktape#2959](https://github.com/agentydragon/ducktape/issues/2959):
whether SQLite-backed applications such as ActivityWatch and Grocy should run on
node-local OVH storage or SeaweedFS CSI volumes.

## Method

The benchmark assets live in this debug directory. Run from the repo devshell:

```bash
debug/sqlite_storage_bench/run_bench.sh
```

The runner creates a disposable `sqlite-storage-bench` namespace, applies the
Kustomize `configMapGenerator` containing the benchmark script, then runs one
Kubernetes Job at a time. Each run gets a fresh PVC and the Job/PVC are deleted
before the next run.

Storage classes:

| StorageClass         | Purpose                                        |
| -------------------- | ---------------------------------------------- |
| `local-path-ovh-ssd` | OVH node-local KS-GAME NVMe tier               |
| `local-path-ovh-hdd` | OVH node-local KS-5 HDD tier                   |
| `seaweedfs-ovh-ssd`  | SeaweedFS CSI with `diskType=ssd`              |
| `seaweedfs-ovh`      | Default SeaweedFS CSI class, HDD/bulk baseline |

The workload uses Python's stdlib SQLite binding, `journal_mode=WAL`, and
`synchronous=FULL`. It records node, mount, kernel, SQLite version, disk free
space, Kubernetes object snapshots, and raw JSONL logs.

## Run

Run ID: `20260707T002921Z`

Artifacts:

- Full generated summary: <results/20260707T002921Z/summary.md>
- CSV summary: <results/20260707T002921Z/summary.csv>

Generated ConfigMap YAML, namespace YAML, PVC/Job manifests, raw pod logs,
Kubernetes object snapshots, and cluster metadata snapshots are intentionally
not committed; rerun `kubectl kustomize`, `render_manifests.py`, or
`run_bench.sh` to regenerate them.

All 20 runs completed: 4 StorageClasses x 5 repeats. Each run used a fresh PVC,
and the Job/PVC were deleted before the next run.

Pod placement:

| StorageClass         | Nodes used                           |
| -------------------- | ------------------------------------ |
| `local-path-ovh-ssd` | `ovh-ns104963` x4, `ovh-ns104952` x1 |
| `local-path-ovh-hdd` | `ovh-ns103656` x5                    |
| `seaweedfs-ovh-ssd`  | `ovh-ns103656` x5                    |
| `seaweedfs-ovh`      | `ovh-ns103656` x5                    |

The SeaweedFS runs mounted `/data` through `fuse.seaweedfs`; local-path runs
mounted XFS on the node-local data disk.

## Results

Values below aggregate the five repeat-level measurements for each class. For
latency rows, units are milliseconds. For throughput rows, units are inserts/sec.

### Fsync-heavy writes

`autocommit_10000` is the strongest proxy for SQLite workloads that commit often.

| StorageClass         | autocommit p95 repeat p50 | autocommit p95 repeat p95 | autocommit max repeat max |
| -------------------- | ------------------------: | ------------------------: | ------------------------: |
| `local-path-ovh-ssd` |                       1.2 |                       1.6 |                      11.8 |
| `local-path-ovh-hdd` |                      25.0 |                      25.1 |                     279.9 |
| `seaweedfs-ovh-ssd`  |                      65.6 |                      67.3 |                    1005.3 |
| `seaweedfs-ovh`      |                      67.5 |                      68.9 |                   21265.8 |

### ActivityWatch-shaped writes

The `activitywatch_batch_100` workload inserts 100k timestamped event rows in
transactions of 100.

| StorageClass         | inserts/sec repeat p50 | write p95 repeat p50 | write p95 repeat p95 | write max repeat max |
| -------------------- | ---------------------: | -------------------: | -------------------: | -------------------: |
| `local-path-ovh-ssd` |               148002.9 |                  1.5 |                  1.5 |                 13.1 |
| `local-path-ovh-hdd` |                 5732.0 |                 90.1 |                 92.2 |                268.4 |
| `seaweedfs-ovh-ssd`  |                 1993.0 |                299.5 |                330.3 |               1798.0 |
| `seaweedfs-ovh`      |                 1926.3 |                270.3 |                280.1 |               1842.9 |

### Grocy-shaped writes

The `grocy_batch_100` workload inserts 100k indexed rows with small JSON/text
payloads in transactions of 100.

| StorageClass         | inserts/sec repeat p50 | write p95 repeat p50 | write p95 repeat p95 | write max repeat max |
| -------------------- | ---------------------: | -------------------: | -------------------: | -------------------: |
| `local-path-ovh-ssd` |               170102.8 |                  1.2 |                  1.4 |                 19.7 |
| `local-path-ovh-hdd` |                 5367.6 |                 24.7 |                 32.3 |                736.2 |
| `seaweedfs-ovh-ssd`  |                 2951.1 |                 41.8 |                 44.1 |               1907.1 |
| `seaweedfs-ovh`      |                 2886.7 |                 41.6 |                 42.8 |               1653.8 |

### Reads and reopen/checkpoint

The 1M-row query workload shows indexed query latency plus the cost to close and
reopen the DB before a count query.

| StorageClass         | 1M time-range p95 repeat p50 | 1M time-range p95 repeat p95 | 1M close/reopen repeat p50 | WAL checkpoint repeat p50 |
| -------------------- | ---------------------------: | ---------------------------: | -------------------------: | ------------------------: |
| `local-path-ovh-ssd` |                         10.5 |                         10.6 |                        6.2 |                       1.3 |
| `local-path-ovh-hdd` |                          9.3 |                         10.1 |                       10.6 |                     119.2 |
| `seaweedfs-ovh-ssd`  |                          7.7 |                         58.4 |                     6068.0 |                     453.9 |
| `seaweedfs-ovh`      |                         12.8 |                         71.2 |                     8300.9 |                     430.5 |

## Recommendation

Use `local-path-ovh-ssd` for ActivityWatch's query/write SQLite DB. It is the
only class with consistently low fsync latency, high insert throughput, sub-10 ms
close/reopen, and ~1 ms WAL checkpoint latency. The tradeoff is node-local
availability: the volume is stranded if that node is down, so pair it with
backups/export replication rather than pretending SeaweedFS is equivalent
durable SQLite storage.

Use `local-path-ovh-ssd` for Grocy if the operational goal is snappy UI and
predictable writes. `local-path-ovh-hdd` is acceptable for a lower-value Grocy
instance if SSD capacity is scarce: its Grocy-shaped p95 write latency was ~25-32
ms, but max batch latency reached ~0.7 s and throughput was ~30x lower than SSD.

Avoid both `seaweedfs-ovh-ssd` and `seaweedfs-ovh` for hot SQLite DBs. They are
acceptable only for low-write, low-risk SQLite apps where occasional multi-second
stalls are tolerable. The replicated/RWX property is real, but it comes through a
FUSE/network path whose SQLite-visible behavior is poor: p95 durable write
latency was ~40-70 ms, ActivityWatch-shaped batch p95s were hundreds of ms, max
write phases hit ~1-2 s, default SeaweedFS produced a 21 s autocommit outlier,
and close/reopen on 1M rows took ~6-9 s.

For replicated availability, prefer an alternate architecture instead of putting
hot SQLite directly on SeaweedFS: application-level export/sync, VolSync/restic
backups from local-path to SeaweedFS/object storage, or migrating the workload to
a replicated database where the app supports it.

No real aw-server-rust import smoke was run. The benchmark used synthetic
ActivityWatch-shaped durable SQLite workloads plus Grocy-shaped indexed rows.
