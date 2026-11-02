# Periodic trim for Seaweed HDD PVCs

Status: design only. Do not deploy until the job, alerting and rollout are
reviewed. This covers the three current Seaweed HDD volume-server claims, not
all Piraeus PVCs.

## Why

Deletion removes an S3 object, and Seaweed eventually compacts volume files, but
compaction only frees ext4 blocks. The ext4 mounts on all three HDD servers lack
`discard`. In a disposable test, Seaweed vacuum followed by `fstrim` returned
about 254 MiB of a 256 MiB object to its Piraeus thin LV; without trim the LV
remained allocated. See [the measured phases](https://github.com/agentydragon/ducktape/pull/13739).

SeaweedFS 4.46's leader scans for compaction every roughly 14–16 minutes after
completion of the preceding scan, with a 30% garbage threshold; production logs
show successful HDD compactions. Talos 1.14 has a trim scheduler for eligible
Talos-managed volumes, but the three nodes report no trim schedules and the
Seaweed CSI volumes do not appear in their Talos volume-status inventory. No
matching Kubernetes trim CronJob or running container command was found; opaque
scripts and external triggers were not ruled out.

## Candidate: one scheduled job per existing claim

Generate three Kubernetes CronJobs with the Seaweed cluster resources, owned by
the existing Seaweed Flux Kustomization. Each references exactly one of:

- `mount0-seaweedfs-volume-hdd-piraeus-102453-0`
- `mount0-seaweedfs-volume-hdd-piraeus-103656-0`
- `mount0-seaweedfs-volume-hdd-piraeus-103711-0`

These are `ReadWriteOnce`, strict-local claims. Their PV node affinity places a
job on the volume's node, alongside the volume server. Kubernetes permits
multiple pods on the same node to mount a ReadWriteOnce volume. Require pod
affinity to the corresponding Seaweed StatefulSet pod label as an extra guard;
this only requires the pod to exist, not to be Ready. If the pod or node is
unavailable, let the job time out and alert; do not relocate the volume or
force attachment. Confirm the affinity against generated labels in tests.

Start with a weekly UTC schedule, staggered across the three nodes, and
`concurrencyPolicy: Forbid` for each CronJob. Use bounded job execution,
`backoffLimit: 0`, and a `startingDeadlineSeconds` that skips stale schedules;
a Job `activeDeadlineSeconds` also bounds a pod already waiting for scheduling.
Forbid applies only to runs of the same CronJob, not across the three jobs or
Seaweed vacuum. Staggering reduces normal overlap but does not guarantee it;
assess load before rollout and do not claim cross-job serialization.

The job mounts only its named PVC at `/data`, verifies it is a mountpoint of
the expected filesystem type, and runs `fstrim -v /data`. Do not use hostPath,
host PID/network, a host-wide mount scan, or raw-device discard. The proposed
container has no API token, a read-only root filesystem, drops all capabilities
except `SYS_ADMIN`, disables privilege escalation, and uses RuntimeDefault
seccomp. `SYS_ADMIN` is powerful even with these constraints; the namespace's
pod-security admission and any applicable policies must explicitly permit this
specific job. Pin the tool image and expose only the one claim. Pod affinity
is not a Ready check, and a successful mount does not verify application health.

A disposable live probe used the same strict-local class and a separate job
with these security settings. While the writer pod remained running, the job
mounted the same PVC on the same node, ran `fstrim`, and reduced the test LV
from 11.05% to 4.82% of 2,151,677,952 bytes: about 128 MiB. The probe namespace,
PV and LINSTOR resource were removed. This demonstrates the shared-mount and
capability path; it does not measure production-sized trim latency or load.

## Alternatives

- **ext4 `discard` mount option:** avoids a scheduled privileged pod but issues
  discard during filesystem operations and may affect write/delete latency.
  A storage-class change affects new claims in that class, including other
  workloads; changing existing PV mount options and remounting is a separate
  rollout. Benchmark the production-like path before choosing it.
- **Talos periodic trim:** its controller schedules Talos-managed volume
  statuses, not arbitrary Kubernetes mounts observed here. Establish CSI support
  before relying on global `FilesystemTrimConfig`.
- **Host-wide privileged DaemonSet:** could cover more PVCs but must discover
  and safely constrain host mounts and avoid double-trimming. Its wider access
  and lifecycle scope are unnecessary for the three-claim pilot.

## Acceptance and rollout

1. Verify Flux ownership, the three claim/PV identities, mount options and
   available HDD headroom. Confirm admission of the exact job and image.
2. Add jobs initially suspended with metrics and alerts for failed, missing,
   overdue and stuck runs; ensure history retention does not erase the signal.
   Distinguish `fstrim`'s submitted range from actual LVM physical reclamation.
3. Enable one server first. Measure the backing LV, pool data and metadata,
   job duration, and production I/O latency during and after a run. Concurrent
   writes and snapshots can affect measured physical changes; do not assert a
   specific reclaimed amount from a live pool's delta.
4. If the first run is healthy, enable the other two separately and verify each
   node. If trim stalls, hurts I/O, fails, or picks the wrong mount, suspend the
   jobs and investigate; never try a raw-device discard as a fallback.
5. Reassess whether this should become a generic opt-in Piraeus capability only
   after validating other PVCs' lifecycle, scheduling, snapshots and security.

Sources: [Kubernetes ReadWriteOnce semantics](https://kubernetes.io/docs/tasks/administer-cluster/change-pv-access-mode-readwriteoncepod/),
[Talos trim scheduler](https://github.com/siderolabs/talos/blob/v1.14.0/internal/app/machined/pkg/controllers/block/volume_trim_schedule.go),
[Seaweed master scheduling](https://github.com/seaweedfs/seaweedfs/blob/4.46/weed/topology/topology_event_handling.go).
