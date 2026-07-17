# Git storage latency & HA on the cluster

**Status:** complete 2026-07-17. All benchmark deployments torn down; the rook-ceph trial
scaffolding is kept suspended in `cluster/k8s/rook-ceph-trial/` for possible revival.

**Question.** Forgejo git writes on this cluster are slow (haku-ui feedback writes take
seconds and sometimes fail). Its git repo store is a `RWX` PVC on `seaweedfs-ovh-ssd`,
shared across multiple Forgejo replicas. What storage/architecture gives **fast git writes
without a single-node dependency**?

**Answer.** The lever is the storage _architecture_, not the storage class. A multi-replica
**RWX shared mount** (Forgejo's design) is the penalty — on SeaweedFS because every git
metadata op is a FUSE→filer→volume round trip, and even on fast CephFS because two mounters
force synchronous MDS capability coordination. **Single-writer-per-repo with app-layer
replication** (GitLab's Gitaly Cluster / Praefect, or GitHub's Spokes) is both faster and
genuinely HA. Measured HA-to-HA, GitLab Gitaly Cluster beats Forgejo's 2-replica RWX on
every operation and in every storage cell tested.

Reproduction harness and raw CSVs: <gitlab_gitaly_storage_bench/>.

---

## 1. SeaweedFS attribution (2026-07-10)

Is it Forgejo being slow or SeaweedFS being slow? Companion to
<sqlite_storage_bench/seaweedfs_latency_forensics.md>, which established the mechanism
(per-op FUSE→filer→volume round-trips dominate; disk class barely matters).

**Method.** End-to-end from a web container: timed `/api/v1/version` (floor), contents-API
reads/writes, and `git push` of tiny commits against a throwaway private repo. Plus a
storage A/B: identical git-shaped workload in two Jobs on the same node
(`ovh-ns104952`), one on `local-path-ovh-ssd`, one on `seaweedfs-ovh-ssd` (200 × 4 KiB
`dd conv=fsync`; 50 tiny `git commit`; local `git clone`; timing via `/proc/uptime`).

End-to-end (fresh empty repo):

| Operation                       | Time (n)                      | Increment over floor |
| ------------------------------- | ----------------------------- | -------------------- |
| `/api/v1/version` (floor)       | ~700 ms (3)                   | —                    |
| contents API read               | 0.8–1.6 s (3)                 | +0.1–0.9 s           |
| `git push`, tiny commit         | 2.1–3.4 s, median ~2.3 s (8)  | **+~1.6 s**          |
| contents API write (small file) | 3.3–5.8 s, median ~4.4 s (12) | **+~3.7 s**          |

Storage A/B, same node, same workload:

| Phase             | local-path-ovh-ssd | seaweedfs-ovh-ssd     | Slowdown |
| ----------------- | ------------------ | --------------------- | -------- |
| 200 × 4 KiB fsync | 340 ms (1.7 ms/op) | 5,360 ms (27 ms/op)   | **16×**  |
| 50 git commits    | 870 ms (17 ms ea)  | 26,650 ms (533 ms ea) | **31×**  |
| local `git clone` | 20 ms              | 5,090 ms              | **254×** |

**Attribution.** A plain `git commit` costs 533 ms on seaweedfs vs 17 ms on local SSD.
Forgejo's contents-API write does several commits' worth of git object/ref/lockfile ops,
so the ~3.7 s over floor is storage round-trips, not app time. Moving to the SSD class
didn't and can't fix it — the cost is per-op network/FUSE round-trips, and git is a
many-tiny-ops workload. This is why production forges keep repos on node-local disk with
application-level replication (GitHub Spokes, GitLab Gitaly Cluster; both explicitly moved
off network filesystems). Recommendation at the time: give the git store a single active
writer on fast local storage, with replication at the app layer.

## 2. Rook / CephFS trial (2026-07-11)

Does a _better filesystem_ fix it? A disposable Rook/Ceph cluster (Ceph v20.2.2, loop-backed
OSDs, kernel CephFS CSI, pod networking) ran two Forgejo replicas on a CephFS `RWX` PVC vs
production Forgejo on SeaweedFS. Two media arms ran sequentially (a single CephCluster can't
mix `dataDirHostPath` roots per node): a three-copy HDD arm and a media-controlled two-copy
SSD arm. All numbers are median wall time, lower is better.

### Direct filesystem path

Media-controlled SSD (both on the same OVH SSD nodes) is the strong comparison:

| Operation                 | SeaweedFS SSD | CephFS SSD | CephFS result |
| ------------------------- | ------------: | ---------: | ------------: |
| 200 × 4 KiB `fsync` files |          4.34 |       0.95 |   4.6× faster |
| 50 tiny Git commits       |         27.46 |       1.17 |    23× faster |
| local Git clone           |          5.75 |       0.16 |    36× faster |

On equal media CephFS is dramatically faster — including tiny `fsync`, where the HDD arm had
made it look 4.88× _slower_ (that penalty was three-copy loop-backed HDD BlueStore, not
CephFS). The kernel CephFS client's metadata/write path beats SeaweedFS's FUSE + filer +
volume-server round trips.

### Two-replica Forgejo path, and isolating the push penalty

Through Forgejo's HTTP API (media-controlled SSD):

| Operation          | SeaweedFS SSD | CephFS SSD (2-rep) |    CephFS result |
| ------------------ | ------------: | -----------------: | ---------------: |
| version request    |         0.349 |              0.307 |     1.14× faster |
| contents API read  |         0.499 |              0.354 |     1.41× faster |
| contents API write |         3.356 |              1.003 |     3.35× faster |
| tiny Git push      |         1.630 |              2.550 | **1.56× slower** |

Surprising: CephFS wins reads and API writes but _loses_ the tiny Git push — despite being
23×/36× faster at direct-fs commits/clone. Hypothesis: the two Forgejo replicas both mount
the repo `RWX`, so CephFS can't grant either exclusive caps, and git's push-time metadata
storm (quarantine objects, atomic ref renames, reflog, hook subprocesses) pays a synchronous
MDS cap revoke/regrant round-trip per mutation. Tested by scaling to a single replica:

| Operation         | CephFS 1-rep | CephFS 2-rep | SeaweedFS SSD |
| ----------------- | -----------: | -----------: | ------------: |
| version           |        0.282 |        0.307 |         0.349 |
| contents read     |        0.307 |        0.354 |         0.499 |
| contents write    |        0.793 |        1.003 |         3.356 |
| **tiny Git push** |    **0.915** |    **2.550** |         1.630 |

Confirmed: removing the second mounter cut Git push **2.79×** (2.55 → 0.92 s) while
single-request ops barely moved; single-mounter CephFS is then faster than SeaweedFS on
every op including push. **The two-replica RWX share, not CephFS, was the push penalty.**

**Implication.** Prod Forgejo needs the shared `RWX` PVC only to sync git state across
replicas — and that shared mount is the core problem, from both directions (SeaweedFS FUSE
round trips, and CephFS MDS cap coordination). The fix is a single active writer: either
single-replica Forgejo with app-layer replication, or a forge whose storage layer is already
single-writer-per-repo (Gitaly / Spokes). Section 3 benches the latter.

Operational notes from the trial: pod networking sufficed for MON/OSD/MDS/CSI; Rook
canonicalizes device symlinks so the CephCluster must select `/dev/loop3` directly, not the
friendly symlink; hard anti-affinity on exactly two Forgejo nodes deadlocks the chart's
default rollout (use `maxSurge: 0`, `maxUnavailable: 1`); fresh CephFS subvolume roots need
an `fsGroup` for a rootless writer. The loop-backed OSDs are a measurement mechanism, not a
production design.

## 3. GitLab Gitaly Cluster — HA single-writer (2026-07-17)

Single-writer is fast, but a single Gitaly on one node is exactly the single-point-of-failure
we want to avoid. **Gitaly Cluster (Praefect)** is the topology that should give both: 3
Gitaly nodes, every repo replicated across them with a single writer _per repo_ (no shared
mount, no cap coordination) and **async** replication (primary acks the push; secondaries
catch up), with automatic failover.

**Setup.** GitLab chart 9.11.8 (v18.11.7), helm-deployed throwaway. 3 stateless webservice
replicas + Praefect + 3 Gitaly nodes (`replaceInternalGitaly`). Only Gitaly's storage class
varies per cell; PG/Redis/MinIO stay on `local-path-ovh-ssd`. Same four operations / 20
samples as the Forgejo bench, hitting the webservice ClusterIP. Harness + per-cell run
recipe: <gitlab_gitaly_storage_bench/README.md>.

Full `{rook, seaweedfs} × {ssd, hdd}` matrix, median seconds
(<gitlab_gitaly_storage_bench/results/praefect_3node/>):

| Storage cell    | version | contents read | contents write | git push |
| --------------- | ------: | ------------: | -------------: | -------: |
| rook-cephfs-SSD |   0.053 |         0.100 |          0.702 |    0.790 |
| seaweedfs-HDD   |   0.046 |         0.099 |          0.870 |    0.970 |
| seaweedfs-SSD   |   0.044 |         0.099 |          0.885 |    1.155 |
| rook-cephfs-HDD |   0.058 |         0.082 |          1.897 |    1.490 |

The two multi-node HA shapes, head to head on `seaweedfs-ssd`:

| Topology (no single-node dependency) | version | contents read | contents write | git push |
| ------------------------------------ | ------: | ------------: | -------------: | -------: |
| GitLab Gitaly Cluster (Praefect)     |   0.044 |         0.099 |          0.885 |    1.155 |
| Forgejo 2-replica RWX                |   0.349 |         0.499 |          3.356 |    1.630 |

For reference, a **single** Gitaly (RWO, SPOF — not HA) on seaweedfs-ssd pushed at 1.010 s
(<gitlab_gitaly_storage_bench/results/single_gitaly_baseline/>): Praefect's 3-way async
replication adds only ~14% over that while providing HA.

## Findings

1. **Gitaly Cluster wins HA-to-HA, on every operation.** vs Forgejo 2-replica RWX on
   seaweedfs-ssd: git push 1.4×, contents write 3.8×, contents read 5×, version 8× faster.
   rook-cephfs-SSD pushes at 0.79 s — 2.1× faster than Forgejo-RWX. Both topologies avoid a
   single-node dependency; Praefect is simply a better way to get there.
2. **The storage backend has a modest effect under Praefect** — push spans 0.79 s (rook-ssd)
   to 1.49 s (rook-hdd), ~1.9×. Three-copy loop-backed HDD Ceph is the slowest cell
   (write 1.9 s, push 1.5 s), consistent with the rook HDD arm. But even that worst cell
   beats Forgejo-RWX (1.63 s); every cell sits below it. The RWX-vs-single-writer
   _architecture_ dominates the media/replication choice. Version/read floors
   (~0.05 / ~0.10 s) are storage-independent, as expected.
3. **The RWX shared mount was the villain, not the disk — confirmed from both sides.** The
   rook trial showed Forgejo is slow even on fast storage (2-mounter RWX cap coordination);
   this bench shows Gitaly Cluster is fast even on slow storage (no shared mount). Moving
   replication to the application layer (per-repo, async) is both faster and genuinely HA.

## Takeaway for the forge

If git-write latency and HA both matter, replace the multi-replica RWX share with
single-writer-per-repo + app-layer replication. GitLab/Gitaly Cluster does this out of the
box; an equivalent for Forgejo is single-writer instances with scheduled mirror/bundle
replication (§1 recommendation #1). This is a "worth building properly and re-measuring on
real raw devices" result, not a drop-in migration decision — the Ceph OSDs here are
loop-backed trial mechanisms. Independently, taking haku-ui feedback writes off the
synchronous path (write-behind: local clone + instant ack + async push) helps regardless of
storage. SeaweedFS remains fine for what it is good at — blob/object workloads via its S3
gateway; the anti-pattern is POSIX-FUSE-mounting it under many-tiny-ops workloads.

## Reproduction & incidental notes

- **Harness:** <gitlab_gitaly_storage_bench/> — values template, Praefect overlay, bench
  script, job template, `rook-hdd-arm.yaml`, and a README with the per-cell run recipe and
  the rook rebuild/teardown playbook (finalizer/mon-state gotchas from the SSD→HDD rebuild).
- **CSI scheduling landmine:** `seaweedfs-csi-driver-mount` runs only on the five `ovh-*`
  nodes; a seaweedfs-PVC pod scheduled elsewhere wedges in `ContainerCreating` with
  `CSINode ... does not contain driver`, no fail-fast. Constrain via the storage class's
  `allowedTopologies`.
- BusyBox `date +%s%N` silently yields seconds-only — alpine bench scripts need
  `/proc/uptime` or coreutils.
