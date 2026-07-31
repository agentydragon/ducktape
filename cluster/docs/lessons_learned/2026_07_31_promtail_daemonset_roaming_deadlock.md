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

`maxUnavailable` = _roaming node count + 1_ (currently 3) on both promtail
DaemonSets. The stuck pods still consume budget, but the ceiling is high enough
that the controller can proceed on the healthy nodes.

Cost: up to 3 nodes briefly without log shipping during a rollout. Promtail
resumes from its positions file, so that is a freshness gap, not lost logs.

`//cluster/validation:test_roaming_daemonset_capacity` enforces the relation,
deriving the roaming count from `nebula-mesh.json` (`role: "laptop"`) rather than
trusting a comment. Its one remaining gap is coverage: a new roaming DaemonSet
must be added to that test's list, or it is unprotected.

## Generalization

Any DaemonSet scheduled onto roaming nodes has this exposure, not just promtail.
When adding one, either set `maxUnavailable` above the roaming count or exclude
roaming nodes via `nodeAffinity`. `k8s/monitoring/stack/helmrelease.yaml` carries
a standing `TODO(roaming-nodes)` proposing exactly that choice cluster-wide;
this incident is the concrete argument for resolving it.

`promtail-journal` was fixed at the same time despite not being stuck — its
nodeSelector is NixOS-only and two of its three candidate nodes are the roaming
laptops, so it is _more_ exposed. It had simply not rolled while one was down.

## Detection

The failure is silent. What surfaced it was checking `rollout status` before
trusting a change, rather than inferring rollout from "the PR merged".

```bash
kubectl -n loki rollout status ds/promtail --timeout=10s
kubectl -n loki get ds     # UP-TO-DATE well below DESIRED is the signal
```

Worth generalizing: after any DaemonSet change, verify `UP-TO-DATE == DESIRED`
before concluding the change is live — and before testing anything downstream of
it. A downstream test run against a non-rolled DaemonSet reports the _old_
behavior, which reads as "the fix didn't work" and sends you debugging correct
config.
