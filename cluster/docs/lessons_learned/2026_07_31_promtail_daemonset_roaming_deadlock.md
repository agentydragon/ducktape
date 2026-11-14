# DaemonSet rollouts deadlock when a roaming node is offline

**Date:** 2026-07-31
**Symptom:** A merged, reconciled promtail change was running on **zero of nine**
nodes an hour after merge, while Flux and the HelmRelease both reported success.

## What happened

```text
$ kubectl -n loki rollout status ds/promtail --timeout=10s
Waiting for daemon set "promtail" rollout to finish: 0 out of 9 new pods have been updated...

desired=9  current=9  updated=0  ready=7

promtail-9tvnr   Pending   node=rugged   deletionTimestamp=10:11:24Z
promtail-c9rrw   Running   node=iguana   deletionTimestamp=10:11:24Z

iguana    NotReady
rugged    NotReady
```

The DaemonSet used the chart default `maxUnavailable: 1`, `maxSurge: 0`. At
rollout start the controller deleted the pods on `rugged` and `iguana` — both
roaming laptops, both offline. Those pods can never terminate, because there is
no kubelet to complete deletion. They therefore hold the entire unavailable
budget permanently, and the controller never advances to the seven healthy nodes.

## Why the existing mitigation didn't cover it

Both promtail HelmReleases already set `upgrade.disableWait: true`, with a
comment explaining roaming nodes may be offline. That is a **different
mechanism**: it stops _Helm_ from waiting on the Pending/Terminating pods, so the
HelmRelease goes Ready. It does nothing about the _DaemonSet controller's_ own
rollout gating.

The combination is the trap — Helm reports success, Flux reports success, and no
pod anywhere is running the new config.

## Fix

`maxUnavailable` = _roaming node count + 1_ (then 3) on both promtail
DaemonSets, pinned to the mesh roster by a test.

## Resolution

Superseded on 2026-11-14 by the
[roaming node controller](../../roaming_node_controller/README.md): an offline laptop
is tainted `node.kubernetes.io/out-of-service`, so DaemonSets stop counting it and pod
GC force-deletes its stuck pods. The `maxUnavailable` floors, their test and Helm
`disableWait` were removed. **Still exposed:** a DaemonSet that tolerates every taint
(`- operator: Exists`) keeps counting an offline laptop and can deadlock the same way.
