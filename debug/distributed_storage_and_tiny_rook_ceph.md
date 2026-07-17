# Distributed storage and a tiny Rook/Ceph trial

**Status:** Complete. HDD arm and media-controlled SSD control arm both measured
2026-07-11; trial torn down 2026-07-17.

This is the implementation companion to <forgejo_git_write_latency.md>. The existing
SeaweedFS Forgejo attribution bench found 31x slower tiny commits and 254x slower local
clones than local SSD. The dominant cost was the FUSE, filer, metadata database, and
volume-server round trips rather than the physical disk class.

## Trial architecture

The trial deliberately leaves production Forgejo and all existing disks untouched:

```text
2 OVH SSD nodes
  -> one preallocated 50 GiB file per node
  -> privileged DaemonSet reserves /dev/loop3 for each file
  -> Rook v1.19.6 / Ceph v20.2.2, one OSD per host
  -> CephFS size=2, min_size=1, kernel CSI client, pod networking
  -> isolated two-replica Forgejo on a 10 GiB RWX PVC
```

The OSDs are loop-backed because the SSDs already hold SeaweedFS data. Rook explicitly
enables loop devices for this test. This
layout is useful for comparing filesystem paths but is not a production Ceph disk
design.

The measured HDD arm used three HDD hosts, three-copy pools, and
`/var/mnt/local-path-ovh-hdd`. The SSD control is a fresh two-host cluster using
two-copy pools and `/var/mnt/seaweedfs-data`. Rook exposes one `dataDirHostPath` per
CephCluster, and Talos does not provide the HDD mount root on SSD hosts, so the two
media arms run sequentially rather than as mixed OSD classes in one cluster.

The CephCluster selects `/dev/loop3` directly. Rook canonicalizes block-device
symlinks during inventory, so selecting the friendlier
`/dev/rook-ceph-trial-osd` symlink causes the available loop device to be
rejected as not matching the requested device list. The DaemonSet still creates
that symlink for readiness checks and operator diagnostics.

Manifests live under <../cluster/k8s/rook-ceph-trial/>. The deployment has no RGW,
RBD pool, public route, DNS record, OAuth integration, or production data.

## Fixed comparison

Both application tests use Forgejo 15.0.3 through chart 17.1.1, two replicas, matching
resource limits, PostgreSQL on the OVH SSD tier, shared Valkey, and ClusterIP access.
The benchmark Jobs run on `ovh-ns104952`.

Direct storage test, five alternating-order rounds:

- 200 x 4 KiB files with `conv=fsync`;
- 50 tiny Git commits;
- local Git clone.

End-to-end test, per Forgejo instance:

- 20 `/api/v1/version` floor requests;
- 20 contents API reads;
- 20 contents API writes to unique files;
- 20 tiny Git commits and pushes;
- fresh private repository, deleted by the Job on exit.

## Results

Raw CSVs are in <results/rook_ceph_forgejo_2026_07_11/>. The first run compared
three-copy CephFS on the HDD nodes with two-copy SeaweedFS on the SSD nodes, so it is
an architecture-plus-media comparison rather than a media-controlled result. Each
number below is the median wall time in seconds. Lower is better.

### Direct filesystem path: Ceph HDD versus SeaweedFS SSD

| Operation                 | SeaweedFS SSD | CephFS HDD | CephFS relative result |
| ------------------------- | ------------: | ---------: | ---------------------: |
| 200 x 4 KiB `fsync` files |          4.38 |      21.37 |           4.88x slower |
| 50 tiny Git commits       |         26.57 |      21.47 |           1.24x faster |
| local Git clone           |          5.65 |       1.11 |           5.09x faster |

CephFS removed most of the clone penalty and modestly improved commit batches, but
three-way replicated BlueStore on loop-backed HDD files was much worse for a stream
of individually durable tiny files. That is a real result for this trial topology,
not a general claim that CephFS has slower fsync than SeaweedFS on production media.

### Two-replica Forgejo path: Ceph HDD versus SeaweedFS SSD

| Operation          | SeaweedFS SSD Forgejo | CephFS HDD Forgejo | CephFS relative result |
| ------------------ | --------------------: | -----------------: | ---------------------: |
| version request    |                 0.444 |              0.419 |           1.06x faster |
| contents API read  |                 0.631 |              0.413 |           1.53x faster |
| contents API write |                 3.097 |              3.160 |           1.02x slower |
| tiny Git push      |                  1.57 |               2.00 |           1.27x slower |

The application comparison says CephFS is clearly better for read-heavy repository
access, essentially tied for contents writes, and worse for this tiny-push workload.
The two Forgejo deployments have matching topology and versions but separate
databases, so the direct filesystem test is the stronger storage attribution.

### Direct filesystem path: media-controlled SSD (CephFS SSD versus SeaweedFS SSD)

The SSD control arm removes the media confound: both CephFS and SeaweedFS run on the
same OVH SSD nodes (CephFS on two-copy loop-backed OSDs, SeaweedFS on its production
SSD volume servers). Raw data in
<results/rook_ceph_forgejo_2026_07_11/direct-storage-ssd-control.csv> (five alternating
rounds); medians below, lower is better.

| Operation                 | SeaweedFS SSD | CephFS SSD | CephFS relative result |
| ------------------------- | ------------: | ---------: | ---------------------: |
| 200 x 4 KiB `fsync` files |          4.34 |       0.95 |            4.6x faster |
| 50 tiny Git commits       |         27.46 |       1.17 |             23x faster |
| local Git clone           |          5.75 |       0.16 |             36x faster |

With media held equal, CephFS is dramatically faster across the board — including the
tiny-`fsync` workload where the HDD arm had made it look 4.88x _slower_. That inversion
confirms the HDD arm's fsync penalty was three-copy loop-backed HDD BlueStore, not
CephFS itself: the kernel CephFS client's metadata and write path clearly beats
SeaweedFS's FUSE + filer + volume-server round trips on the same disks.

### Two-replica Forgejo path: media-controlled SSD (CephFS SSD versus SeaweedFS SSD)

The application-path SSD comparison was originally lost — both e2e Jobs are one-shot
(`backoffLimit: 0`, `restartPolicy: Never`) and a transient Forgejo 500 during repo
setup killed them with no retry. Re-run 2026-07-17 against the still-live bench Forgejo
(CephFS) and production Forgejo (SeaweedFS); the transient 500 did not recur. Raw data
in <results/rook_ceph_forgejo_2026_07_11/forgejo-e2e-ssd-control.csv> (20 samples per
operation per arm); medians below, lower is better.

| Operation          | SeaweedFS SSD | CephFS SSD | CephFS relative result |
| ------------------ | ------------: | ---------: | ---------------------: |
| version request    |         0.349 |      0.307 |           1.14x faster |
| contents API read  |         0.499 |      0.354 |           1.41x faster |
| contents API write |         3.356 |      1.003 |           3.35x faster |
| tiny Git push      |         1.630 |      2.550 |           1.56x slower |

Even media-controlled, the application path is more mixed than the direct-fs path:
CephFS wins reads and (unlike the HDD arm) clearly wins the contents API write, but it
is meaningfully _slower_ for the tiny Git push. So the 23x/36x direct-fs commit/clone
advantage does **not** carry through Forgejo's push path — receive-pack, hooks, and the
separate per-instance database dominate there and erase the filesystem gain. Read and
contents-write latency are where CephFS would actually help this workload.

### Operational findings

- Pod networking worked for MON, OSD, MDS, and CSI traffic; host networking was not
  required.
- A single Rook CephCluster cannot use different `dataDirHostPath` roots per node.
  Mixing these HDD and SSD Talos hosts made SSD prepare pods fail before device
  discovery because `/var/mnt/local-path-ovh-hdd` is absent and cannot be created on
  the read-only root. Separate sequential clusters avoid host-path tricks.
- Rook canonicalizes device symlinks during inventory, so the CephCluster must select
  the reserved `/dev/loop3` rather than `/dev/rook-ceph-trial-osd`.
- A PVC submitted before CSI secrets and CephFS existed retained a poisoned retry;
  recreating the empty claim after the filesystem was healthy bound immediately.
- Hard anti-affinity on exactly two Forgejo nodes deadlocks the chart default rollout
  strategy. `maxSurge: 0` and `maxUnavailable: 1` keeps one replica serving while the
  other is replaced.
- Fresh CephFS subvolume roots require an `fsGroup` for a rootless direct writer.
- Rook performed one coordinated OSD deployment refresh near the end of the direct
  test. All three OSDs returned `up/in`, all 80 placement groups returned
  `active+clean`, and final health was `HEALTH_OK`. Five alternating rounds reduce the
  effect on medians, but loop-backed OSDs remain a trial mechanism, not a production
  recommendation.

Reading the arms together: the HDD arm's fsync penalty was a media/replication artifact
(three-copy loop-backed HDD), not a CephFS property. On media-controlled SSD, CephFS is
uniformly and dramatically faster than SeaweedFS on the **direct filesystem path** (4.6x
fsync, 23x tiny commits, 36x clone) — the kernel CephFS client avoids SeaweedFS's FUSE +
filer + volume-server round trips that dominate tiny-write and clone latency. But that
raw-filesystem win only partially survives the **Forgejo application path**: CephFS keeps
the read and contents-write advantages (1.1–3.4x) yet is 1.56x _slower_ for tiny Git
pushes, because receive-pack, hooks, and the per-instance database — not the filesystem —
dominate the push. So CephFS is a genuinely promising SeaweedFS replacement where read /
contents-write latency is the pain point, but it is not a blanket git-write win. Either
way this justifies a follow-up on **real raw devices** (not loop-backed) with SSD DB/WAL
before any migration; the loop-backed OSD topology here is a measurement mechanism, not a
production design, so this is a "worth building properly and re-measuring" result, not a
drop-in migration decision.

## Teardown contract

Consumers and PVCs are removed first. After CephFS is gone, the CephCluster receives
the exact cleanup confirmation `yes-really-destroy-data` and its cleanup Jobs must
finish before the operator or loop-device DaemonSet is removed. The loop devices are
detached and their backing files deleted last. Production Forgejo and SeaweedFS health
are rechecked after teardown.
