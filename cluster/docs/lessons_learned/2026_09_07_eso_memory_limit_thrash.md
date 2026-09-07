# ESO Memory Limit Thrash: A Healthy Pod That Kills Its Node's Disk

**Date**: 2026-09-07
**Status**: Resolved

## Root Cause

`external-secrets` ran with `limits.memory: 128Mi`. Its Go heap had grown to ~67 MiB
(66 `ExternalSecret`s, 8 `ClusterSecretStore`s, `concurrent: 8`), leaving too little room
for the ~60 MiB of file-backed pages the mapped binary needs.

A cgroup at its memory limit does not have to OOM-kill. The kernel can always satisfy
`memory.max` by reclaiming file-backed pages instead, so it evicted the binary's text
pages, the process immediately faulted them back in, and the loop ran forever:

```text
memory.max              134217728   (128 MiB)
memory.current          134168576   (99.96% of the limit)
anon                     70238208   (67 MiB heap)
file                     61939712   (59 MiB, being continuously evicted and re-read)
workingset_refault_file  +36,500/s
```

The pod reported `1/1 Running` with `0` restarts throughout. There is no `OOMKilled`, no
restart, and no event — the only outward sign is disk I/O.

On `ovh-ns102453` that I/O landed on `sda5`, the Talos `EPHEMERAL` partition that also
holds `/var/lib/containerd`, on a 7200 RPM HDD:

```text
sda5  r/s=542  rMB/s=155.8  busy=111%  await=69ms
```

## Key Symptoms

The interesting damage was all on the node, not on ESO:

- Talos `cri` service health check flapping: `DeadlineExceeded`, then healthy, every few
  minutes.
- `RunPodSandbox` timing out, then every retry failing permanently with
  `failed to reserve sandbox name "…" is reserved for "<id>"` — containerd does not
  release the reservation when the call is cancelled
  ([containerd#9947](https://github.com/containerd/containerd/issues/9947); it is only
  released in `RemovePodSandbox`, i.e. kubelet GC —
  [containerd#9459](https://github.com/containerd/containerd/issues/9459)).
- Containers stuck in `already in removing state`, retried every ~60 s indefinitely.
- CSI `MountVolume.MountDevice failed: timeout waiting for mount`.
- Ten pods wedged in `PodInitializing`/`ContainerCreating` on that one node, including a
  `haku-console-migration` Job that produced **zero log output** and died at
  `activeDeadlineSeconds` — the symptom that started the investigation.
- ESO's own logs full of `client-side throttling` delays of 80–140 s and
  `dial tcp 10.96.0.1:443: i/o timeout`: it was too busy page-faulting to reach the API
  server.

## What Misled Us

- **The crash-looping `cilium` pod on `rugged`.** `rugged` was down for unrelated reasons;
  its pod status was a frozen snapshot from before the kubelet stopped reporting. Node
  status on a `NotReady` node is stale, not current.
- **The one `D`-state process.** `openclaw-database-verify.worker.js` was blocked in
  `folio_wait_bit_common`, which reads like a culprit. Its own `/proc/<pid>/io` showed
  ~94 KB/s — three orders of magnitude too small. It was a victim of the saturated disk.
- **`kubectl top`.** It reports 108Mi against the 128Mi limit, which looks like healthy
  headroom. It shows only the anon working set; the thrashing is in the file pages it
  does not report.

## Diagnosis

Attribute the I/O to a cgroup rather than guessing from process state. `talosctl cgroups
--preset=io` rounds to human units, so a 20-second delta disappears — read the raw
counters:

```bash
# per-pod read rate, device 8:0 = sda
talosctl -n $NODE list /sys/fs/cgroup/kubepods/burstable | awk 'NR>1 && $NF ~ /^pod/'
talosctl -n $NODE read /sys/fs/cgroup/kubepods/burstable/pod<uid>/io.stat   # twice, diff rbytes
```

Then confirm thrash rather than legitimate reads — the pair that settles it is
`memory.current` sitting at `memory.max` while `workingset_refault_file` climbs:

```bash
talosctl -n $NODE read /sys/fs/cgroup/kubepods/burstable/pod<uid>/memory.max
talosctl -n $NODE read /sys/fs/cgroup/kubepods/burstable/pod<uid>/memory.current
talosctl -n $NODE read /sys/fs/cgroup/kubepods/burstable/pod<uid>/memory.stat  # twice
```

A high `workingset_refault_file` rate means pages are being evicted and immediately
needed again. `pgmajfault` undercounts it — most refaults are served from readahead.

## What Fixed It

`limits.memory: 512Mi`, `requests.memory: 256Mi` on the controller, sized above heap plus
mapped binary with room to grow.

Clearing the leaked sandbox reservations on the affected node needs a `cri` restart
(`talosctl -n $NODE service cri restart`) — but only after the I/O source is gone, or the
~95 restarting containers stampede onto the same saturated spindle.

## Key Lessons

1. **A memory limit sized to the heap is a disk bug waiting to happen.** Any container
   with a large mapped binary needs `limit > heap + binary`. Below that it degrades into
   unbounded I/O rather than dying, which is strictly worse: nothing restarts it and
   nothing alerts.
2. **`1/1 Running`, `0` restarts is not health.** This failure mode has no Kubernetes-level
   signal at all. Alert on `container_memory_working_set_bytes / limit` approaching 1 and
   on sustained `workingset_refault` — not just on `OOMKilled` and restarts.
3. **A saturated node disk presents as a containerd bug.** The visible errors are all
   sandbox-name collisions and CRI deadlines, several layers above the actual cause.
   Measure device utilization before reading upstream issue trackers.
4. **Co-locating pod ephemeral storage with containerd on one spindle couples every
   workload to every other.** One pod's I/O took out pod creation for the whole node.
5. **Attribute, don't correlate.** Both wrong leads here were plausible processes that
   measurement immediately excluded.
