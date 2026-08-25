# Descheduler eviction loop on the SeaweedFS filer → cluster-wide git SIGBUS (2026-08-24)

## Symptom

All Forgejo git traffic failed. Pushes returned `remote unpack failed:
unpack-objects abnormal exit`; Flux reported `GitOperationFailed` on
`gitrepository/haku-state` with `pkt-line 3: EOF`. Forgejo logged, continuously:

```text
githttp.go:462:serviceRPC() [E] Fail to serve RPC(upload-pack) in
  /data/git/gitea-repositories/haku/haku-state.git: signal: bus error
```

Reads failed as well as writes, which is why Flux could not even list refs.

## Root cause

`sigs.k8s.io/descheduler` evicted `seaweedfs/seaweedfs-filer-0` from
`ovh-ns103711` on **every 15-minute run**, in a loop: `LowNodeUtilization`
classified the node over-utilized, evicted the filer, and the scheduler placed it
straight back on the same node.

Each filer restart desynchronizes the `weed mount` FUSE clients subscribed to it
(`meta_cache.go:331 unsynchronized dir`), leaving cached chunk locations that no
longer resolve. git reads packfiles via `mmap`, and an I/O error on a
memory-mapped page is delivered as **SIGBUS**, killing `upload-pack` and
`unpack-objects`. Same failure mechanism as
2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md, reached by a
different trigger.

The damage was confined to the one mount that had been running longest
(`ovh-ns103711`, since 2026-07-05): exactly 7 of 40 packfiles faulted, all
written 2026-07-04..13. A probe pod on another node read the same PVC — all 40
packs, 177 refs — cleanly, proving the stored data was intact and the poison was
purely client-side cache.

### Why nothing stopped the eviction

Three protection layers were added in
2026_06_19_seaweedfs_descheduler_dns_race_crashloop.md, and none of them makes a
pod ineligible for eviction:

- **Resource requests** move the pod out of BestEffort. That only changes the QoS
  tiebreak _among pods already selected_.
- **PDB** `seaweedfs-filer minAvailable: 1` over 2 replicas permits one filer to
  go at a time — which is all the descheduler ever took.
- **`stateful-infra` PriorityClass** (1*000_000) was chosen, deliberately, to sit
  \_below* the descheduler's default `priorityThreshold`, on the belief that the
  descheduler "evicts lowest-priority first". It does not: `priorityThreshold` is
  a hard binary filter, and priority order only ranks pods that are already
  eligible. A class below the threshold confers no exemption at all.

So the class whose stated purpose was to defer eviction in fact guaranteed
eligibility for it.

## Fix

Set `DefaultEvictor.priorityThreshold.value` to the `stateful-infra` value, which
makes every SeaweedFS component ineligible for descheduler eviction. This affects
only the descheduler — `kubectl drain` still evicts through the eviction API and
the PDBs, so node maintenance is unchanged.

A validation test pins the threshold to the PriorityClass value and asserts every
pod-spawning component in the Seaweed CR opts into the class.

## Recovery

1. Suspend the descheduler CronJob to stop the loop.
2. Delete the affected `seaweedfs-csi-driver-mount` pod (`OnDelete`, so this is
   also the controlled roll).
3. Restart the consumer — the FUSE mount does not propagate into a running
   container, so Forgejo's `/data` is `ENOTCONN` until its pod is replaced. This
   is the gap issue #4616 tracks.

## What to watch for

The blast radius of restarting a filer is not "a brief re-election" — it is every
FUSE client that has it subscribed, and the resulting corruption is invisible
until something `mmap`s a file. Treat the SeaweedFS metadata plane as
non-evictable rather than merely low-priority.
