# SeaweedFS SQLite Latency Forensics

Status: observed 2026-07-07.

This note explains why SQLite over `seaweedfs-ovh-ssd` was slow in the
20260707 SQLite storage benchmark, using live link measurements, SeaweedFS
source inspection, and a targeted SQLite phase timing probe.

## Question

The confusing result was that `seaweedfs-ovh-ssd` behaved worse than local HDD
for several SQLite write workloads. The specific question was whether the result
was explained by bandwidth limits, Nebula/inter-node latency, SeaweedFS
round-trips, or something else.

## Current Topology

The original benchmark scheduled every SeaweedFS-backed SQLite job on
`ovh-ns103656`.

Relevant live placement during the follow-up investigation:

| Role                                    | Pod or node                        | Host           |
| --------------------------------------- | ---------------------------------- | -------------- |
| SQLite writer / SeaweedFS CSI mount pod | `seaweedfs-csi-driver-mount-22rfw` | `ovh-ns103656` |
| Filer                                   | `seaweedfs-filer-0`                | `ovh-ns103711` |
| Filer                                   | `seaweedfs-filer-1`                | `ovh-ns102453` |
| Filer DB primary                        | `seaweedfs-filer-db-ssd-2`         | `ovh-ns104952` |
| SSD volume                              | `seaweedfs-volume-ssd-1`           | `ovh-ns104952` |
| SSD volume                              | `seaweedfs-volume-ssd-0`           | `ovh-ns104963` |

Physical rack notes from `cluster/k8s/seaweedfs/cluster/seaweed.yaml`:

| Node           | Rack      |
| -------------- | --------- |
| `ovh-ns102453` | `H109A09` |
| `ovh-ns103656` | `H109B04` |
| `ovh-ns103711` | `H109B04` |
| `ovh-ns104952` | `H108B01` |
| `ovh-ns104963` | `H108B01` |

So the writer was not on the SSD rack. The two SSD nodes are same-rack with each
other, but the SQLite writer was in `H109B04` while SSD volume and metadata DB
traffic went to `H108B01`.

## Measured Links

Measurements were made from temporary `nicolaka/netshoot:v0.13` pods and from
the actual SeaweedFS CSI mount pod on `ovh-ns103656`.

### RTT From Writer Side

From `seaweedfs-csi-driver-mount-22rfw` on `ovh-ns103656`:

| Target                             | Path type              | RTT min/avg/max        |
| ---------------------------------- | ---------------------- | ---------------------- |
| `10.42.0.14` (`ovh-ns103711`)      | node internal / Nebula | `0.263/0.545/1.928 ms` |
| `10.42.0.15` (`ovh-ns102453`)      | node internal / Nebula | `0.304/0.733/5.472 ms` |
| `10.42.0.16` (`ovh-ns104952`)      | node internal / Nebula | `0.246/0.926/6.427 ms` |
| `10.42.0.17` (`ovh-ns104963`)      | node internal / Nebula | `0.306/0.625/2.759 ms` |
| `10.244.2.108` (`filer-0`)         | pod overlay            | `0.323/0.937/4.625 ms` |
| `10.244.4.124` (`filer-1`)         | pod overlay            | `0.236/0.754/3.447 ms` |
| `10.244.0.252` (`volume-ssd-1`)    | pod overlay            | `0.345/1.215/5.518 ms` |
| `10.244.3.203` (`volume-ssd-0`)    | pod overlay            | `0.396/1.564/7.563 ms` |
| `10.244.0.79` (`filer-db` primary) | pod overlay            | `0.311/1.462/8.436 ms` |

Longer 100-sample pings from a pod pinned to `ovh-ns103656`:

| Target         | Address          | RTT min/avg/max/mdev          |
| -------------- | ---------------- | ----------------------------- |
| `ovh-ns103711` | `10.42.0.14`     | `0.185/1.386/9.419/2.031 ms`  |
| `ovh-ns102453` | `10.42.0.15`     | `0.183/1.376/9.272/1.935 ms`  |
| `ovh-ns104952` | `10.42.0.16`     | `0.254/0.711/8.228/0.880 ms`  |
| `ovh-ns104963` | `10.42.0.17`     | `0.257/1.022/13.272/1.909 ms` |
| `ovh-ns103711` | `147.135.39.176` | `0.094/0.311/2.937/0.428 ms`  |
| `ovh-ns102453` | `147.135.37.175` | `0.138/0.282/2.324/0.249 ms`  |
| `ovh-ns104952` | `147.135.104.5`  | `0.149/0.351/2.251/0.239 ms`  |
| `ovh-ns104963` | `147.135.104.16` | `0.159/0.334/2.193/0.275 ms`  |

Nebula/internal addressing was several times slower and much spikier than the
public OVH path, but still around 1 ms average rather than tens of milliseconds.

### Filer To Metadata DB

From pods pinned to the filer nodes:

| Source         | Target                       | RTT min/avg/max/mdev          |
| -------------- | ---------------------------- | ----------------------------- |
| `ovh-ns103711` | DB primary pod `10.244.0.79` | `0.242/1.434/15.381/2.516 ms` |
| `ovh-ns102453` | DB primary pod `10.244.0.79` | `0.287/1.055/10.917/1.632 ms` |

This matters because SeaweedFS filer metadata writes are in the SQLite fsync
critical path.

### Throughput

`iperf3` from a pod on `ovh-ns103656` to a pod on `ovh-ns104952` measured:

```text
0.00-10.03 sec  532 MBytes  444 Mbits/sec receiver
```

The same run had many TCP retransmits, so the path is not clean. Still, the
available throughput is orders of magnitude higher than the SQLite write rate
observed through SeaweedFS:

| Workload                                      | Approx data |         Time |  Effective rate |
| --------------------------------------------- | ----------: | -----------: | --------------: |
| `seaweedfs-ovh-ssd` `activitywatch_batch_100` |   ~12.4 MiB |        ~50 s |       ~2 Mbit/s |
| `seaweedfs-ovh-ssd` WAL checkpoint            |     ~14 MiB | ~0.45-0.68 s | ~170-260 Mbit/s |

The slow write workloads are therefore latency/serialization bound, not raw
bandwidth bound.

## SQLite Phase Timing Probe

A temporary Kubernetes job was run on `seaweedfs-ovh-ssd`, scheduled to
`ovh-ns103656`, to split a 100-row transaction into:

- Python/SQLite row execution time.
- `conn.commit()` time.
- explicit `PRAGMA wal_checkpoint(TRUNCATE)` time.

The workload used `journal_mode=WAL`, `synchronous=FULL`, and 120 transactions
of 100 rows.

| Mode                              | Execute p50 | Execute p95 | Commit p50 | Commit p95 | Commit max |                   Explicit checkpoint |
| --------------------------------- | ----------: | ----------: | ---------: | ---------: | ---------: | ------------------------------------: |
| default `wal_autocheckpoint=1000` |     0.69 ms |     0.93 ms |   18.39 ms |  110.60 ms |  158.86 ms | 88.79 ms after prior auto-checkpoints |
| `wal_autocheckpoint=0`            |     0.73 ms |     1.39 ms |   19.30 ms |   54.12 ms |   73.89 ms |                             693.11 ms |

The row work is sub-millisecond. The wait is in commit/fsync and checkpoint.

The default auto-checkpoint run had its slowest commits at transactions
`19, 38, 57, 76, 95, 114`. Each slow commit coincided with a WAL size around
4.3 MiB, matching SQLite's default `wal_autocheckpoint=1000` threshold. That
explains the large p95/max write latencies in the original benchmark.

## SeaweedFS Source Path

Exact versions used by the cluster:

- SeaweedFS image: `chrislusf/seaweedfs:4.29`
- CSI driver: `chrislusf/seaweedfs-csi-driver:v1.4.14`
- Mount image: `chrislusf/seaweedfs-mount:v1.4.14`

The relevant source path was inspected from those tags.

CSI mount setup:

- `seaweedfs-csi-driver/pkg/driver/mounter.go`
- CSI runs `weed mount` with `-filer=...`, `-cacheDir=...`, and StorageClass
  parameters such as `diskType` -> `-disk` and `replication` -> `-replication`.

SeaweedFS FUSE write/fsync:

- `weed/mount/weedfs_write.go`
- `weed/mount/weedfs_file_sync.go`
- `weed/mount/dirty_pages_chunked.go`

Important behavior:

- FUSE `Write` queues dirty pages; it does not necessarily synchronously hit the
  network for every SQLite write.
- FUSE `Fsync` is explicit and synchronous; it calls
  `doFlush(..., allowAsync=false)`.
- `doFlush` flushes dirty pages, then flushes metadata to the filer.

Data flush:

- `weed/mount/weedfs_write.go`
- `weed/operation/upload_content.go`
- `weed/server/filer_grpc_server.go`
- `weed/operation/assign_file_id.go`

The data path does:

1. Mount process asks filer for a volume assignment.
2. Filer asks master for the assignment.
3. Mount process HTTP POSTs the chunk to the selected volume server.
4. With `replication: "001"`, the volume server write includes replica work.

Metadata flush:

- `weed/mount/weedfs_file_sync.go`
- `weed/server/filer_grpc_server.go`
- `weed/filer/filer.go`
- `weed/filer/abstract_sql/abstract_sql_store.go`

The metadata path does:

1. Mount process sends `CreateEntry` to a filer.
2. Filer updates the chunk list/attributes in its metadata store.
3. The current metadata store is `postgres2`, backed by the
   `seaweedfs-filer-db-ssd` CNPG cluster.

## Live SeaweedFS Metrics

Metrics scraped from filer and volume pods corroborated the source-level model.

Filer Postgres store histograms:

| Operation | Filer          | Mean from sum/count | Bucket shape                    |
| --------- | -------------- | ------------------: | ------------------------------- |
| `insert`  | `10.244.2.108` |             ~6.8 ms | p95-ish bucket around `25.6 ms` |
| `update`  | `10.244.2.108` |             ~5.6 ms | p95-ish bucket around `25.6 ms` |
| `insert`  | `10.244.4.124` |             ~4.1 ms | p95-ish bucket around `25.6 ms` |
| `update`  | `10.244.4.124` |             ~5.0 ms | p95-ish bucket around `25.6 ms` |

SSD volume server histograms:

| Operation         | Volume pod     | Mean from sum/count |
| ----------------- | -------------- | ------------------: |
| `POST`            | `10.244.0.252` |             ~2.1 ms |
| `writeToReplicas` | `10.244.0.252` |             ~2.2 ms |
| `POST`            | `10.244.3.203` |             ~1.6 ms |
| `writeToReplicas` | `10.244.3.203` |             ~1.9 ms |

These are cluster-wide cumulative metrics, not isolated to the temporary probe,
but the magnitudes match the RTT and commit-time decomposition.

## Causal Model

For SQLite with `synchronous=FULL`, one normal WAL commit on
`seaweedfs-ovh-ssd` is roughly:

1. SQLite appends WAL data.
2. SQLite calls fsync/fdatasync during `COMMIT`.
3. SeaweedFS FUSE synchronously flushes dirty data.
4. Data flush performs filer/master assignment and volume-server POST.
5. The volume server handles replica write work.
6. SeaweedFS FUSE synchronously flushes file metadata.
7. Filer writes chunk metadata to Postgres.
8. Fsync returns to SQLite.

Minimum network crossings for a small dirty commit include:

- writer/mount -> filer
- filer -> master
- writer/mount -> SSD volume
- volume -> replica volume
- writer/mount -> filer for metadata
- filer -> Postgres primary

The measured links are individually short, mostly around 1 ms over the pod or
Nebula paths. But the operations are serialized, run through FUSE/userspace,
perform HTTP/gRPC work, touch SeaweedFS volume metadata, and persist filer
metadata through Postgres. Stacking these costs produces the observed
`seaweedfs-ovh-ssd` commit floor around 18-22 ms and p95 around 50-70 ms before
checkpoint effects.

When SQLite's default WAL auto-checkpoint fires, the commit also performs
checkpoint work against the main DB file. On SeaweedFS this means additional
dirty-data and metadata flushes over the same distributed path. That raises
commit p95 into 100 ms+ territory and explains the 1-2 second max phases seen in
the larger original benchmark.

## What The Link Speeds Do And Do Not Explain

The link measurements explain the observed behavior this way:

- Raw bandwidth is not the primary limit for ordinary write transactions. The
  measured path could move hundreds of Mbit/s, while SQLite-through-SeaweedFS
  write workloads achieved only a few Mbit/s.
- Nebula/internal routing adds real latency and jitter compared with public
  OVH addressing. In the sample, public RTTs were about `0.3 ms`, while
  Nebula/pod paths were often `0.7-1.5 ms` average with occasional `5-15 ms`
  spikes.
- That extra RTT/jitter hurts because SQLite commits are synchronous and
  SeaweedFS fsync stacks several network operations.
- Nebula alone is not enough to explain 20-70 ms commits. The larger cause is
  SeaweedFS's distributed FUSE/fsync path plus SQLite's WAL checkpoint behavior.

The practical result is that a same-datacenter SeaweedFS path can be acceptable
for blobs, object-like workloads, backups, and RWX convenience, but it is a poor
substrate for hot SQLite databases that rely on frequent durable commits.

## Methodology To Reproduce

1. Re-run the full storage benchmark:

   ```bash
   debug/sqlite_storage_bench/run_bench.sh
   ```

2. Confirm current pod and node placement:

   ```bash
   kubectl get nodes \
     -L topology.kubernetes.io/zone,storage.allegedly.works/tier,kubernetes.io/hostname \
     -o wide
   kubectl get pods -n seaweedfs -o wide
   kubectl get pods -n seaweedfs-csi-system -o wide
   kubectl get svc,endpointslices -n seaweedfs -o wide
   ```

3. Measure writer-side RTT from the actual CSI mount pod on the writer node:

   ```bash
   kubectl exec -n seaweedfs-csi-system seaweedfs-csi-driver-mount-22rfw -- \
     sh -lc 'for ip in 10.42.0.14 10.42.0.15 10.42.0.16 10.42.0.17 \
       10.244.2.108 10.244.4.124 10.244.0.252 10.244.3.203 10.244.0.79; do \
       echo "$ip"; ping -q -c 20 -i 0.1 "$ip" | tail -n 2; done'
   ```

4. Create temporary netshoot pods pinned to relevant nodes for longer RTT,
   tracepath, mtr, and iperf probes. Avoid `hostNetwork` in baseline namespaces;
   PodSecurity may block it.

5. Measure overlay throughput with `iperf3`:

   ```bash
   kubectl exec -n seaweedfs-latency-debug netshoot-104952 -- \
     iperf3 -s -1 -p 5201
   kubectl exec -n seaweedfs-latency-debug netshoot-103656 -- \
     iperf3 -c <netshoot-104952-pod-ip> -p 5201 -t 10
   ```

6. Run a small SQLite phase probe on a fresh `seaweedfs-ovh-ssd` PVC:
   - Use `journal_mode=WAL`.
   - Use `synchronous=FULL`.
   - Time row execution separately from `conn.commit()`.
   - Run once with default `wal_autocheckpoint`.
   - Run again with `PRAGMA wal_autocheckpoint=0`.
   - Time an explicit `PRAGMA wal_checkpoint(TRUNCATE)`.

7. Inspect the exact SeaweedFS and CSI versions used by the cluster:

   ```bash
   git clone --branch 4.29 https://github.com/seaweedfs/seaweedfs /tmp/seaweedfs-4.29
   git clone --branch v1.4.14 https://github.com/seaweedfs/seaweedfs-csi-driver \
     /tmp/seaweedfs-csi-driver-v1.4.14
   ```

8. Scrape SeaweedFS metrics for corroborating operation latencies:

   ```bash
   curl -fsS http://<filer-pod-ip>:9326/metrics |
     grep 'SeaweedFS_filerStore_request_seconds'
   curl -fsS http://<volume-pod-ip>:9328/metrics |
     grep 'SeaweedFS_volumeServer_request_seconds'
   ```

9. Clean up temporary debug namespaces/PVCs/jobs.

## Recommendation

Keep hot SQLite databases on node-local storage, preferably
`local-path-ovh-ssd`, and back them up or export them to replicated/object
storage asynchronously. Do not use SeaweedFS CSI as the primary filesystem for
SQLite databases that need low-latency durable commits.
