# Distributed storage and a tiny Rook/Ceph trial

**Status:** implementation prepared 2026-07-11; measurements pending.

This is the implementation companion to <forgejo_git_write_latency.md>. The existing
SeaweedFS Forgejo attribution bench found 31x slower tiny commits and 254x slower local
clones than local SSD. The dominant cost was the FUSE, filer, metadata database, and
volume-server round trips rather than the physical disk class.

## Trial architecture

The trial deliberately leaves production Forgejo and all existing disks untouched:

```text
3 OVH HDD nodes
  -> one preallocated 50 GiB file per node
  -> privileged DaemonSet attaches each file as /dev/rook-ceph-trial-osd
  -> Rook v1.19.6 / Ceph v20.2.2, one OSD per host
  -> CephFS size=3, min_size=2, kernel CSI client, pod networking
  -> isolated two-replica Forgejo on a 10 GiB RWX PVC
```

The OSDs are loop-backed because the HDDs are already formatted XFS and hold
SeaweedFS/local-path data. Rook explicitly enables loop devices for this test. This
layout is useful for comparing filesystem paths but is not a production Ceph disk
design.

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

Pending live reconciliation and benchmark execution.

## Teardown contract

Consumers and PVCs are removed first. After CephFS is gone, the CephCluster receives
the exact cleanup confirmation `yes-really-destroy-data` and its cleanup Jobs must
finish before the operator or loop-device DaemonSet is removed. The loop devices are
detached and their backing files deleted last. Production Forgejo and SeaweedFS health
are rechecked after teardown.
