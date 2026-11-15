# wyrm2 Alloy replay canary

**Deploying this change runs `monitoring/wyrm2-alloy-canary` immediately.** Review
and deploy only during a supervised test window. There is no suspended-Job toggle
or automatic retry. No node has been changed by preparing this PR. Other nodes
remain on Promtail; both old releases remain installed.

This canary explicitly chooses replay: both legacy processes currently share
`/run/promtail/positions.yaml`, which cannot prove independent final checkpoints.
The Job preserves that stopped file as rollback evidence and does **not** import
it. Alloy reads retained files from the beginning for Pods it can still discover;
journal Alloy starts with its existing `max_age=12h`. Expect duplicates across
those windows. Pod retention is not a fixed time window: record the oldest
retained, discoverable files before deployment. Deleted short-lived Pods may no
longer be discoverable after the pause. Rotation, journal retention, and Loki's
age, out-of-order and rate limits can make records unrecoverable. This procedure
does not promise gap-free or exactly-once delivery; never tail from end to hide
duplicates.

## Before deployment

Keep wyrm2 online, avoid collector/config/image updates during the handoff, and
keep test Pods alive throughout it. Compare the deployed legacy scrape/relabel
configuration with [`pod-logs.alloy`](../pod-logs.alloy) and
[`journal.alloy`](../journal.alloy); the Job cannot read Secrets and does not
validate Promtail's Secret-backed runtime configuration. Record retained-file and
journal bounds, Loki limits, CPU/memory/page-cache/disk pressure and metrics/WAL
health. Allow replay headroom and verify pre-cutover records are queryable.

Use a long-lived test Pod pinned to wyrm2 to emit numbered, uniquely tagged
stdout/stderr records periodically before, during and after deployment. Include
multiline/partial CRI records. On wyrm2, emit tagged systemd journal records with
`logger -t alloy-wyrm2-canary 'UNIQUE-ID sequence=N phase=before'`; repeat during
and after the pause. Save Loki query results and the exact test ID. Also record
representative static-Pod, systemd and kernel messages, including useful labels.

The Job rejects unexpected state **before draining**: Ready/no-pressure NixOS
wyrm2 without taints, initial labels absent or `promtail`, complete collector
rollouts, current Promtail 3.5.1 and Alloy v1.22.0, expected host paths, matching
Alloy configuration fingerprints, and working `promtail → drained → alloy`
scheduling gates. Existing legacy import files, journal state, nonempty/unknown
pod positions, or a previous attempt fence block activation. Read-only host
mounts may create missing empty journal/component/evidence directories through
Kubernetes `DirectoryOrCreate`; they never create legacy import files.

After these checks it records preflight evidence, atomically labels **only
wyrm2** `drained`, and waits for both Promtails and node Alloy to actually
terminate while the node remains Ready. It snapshots/checksums the stopped
shared checkpoint and, if present, archives/removes only a strict empty
`loki.source.file.pods/positions.yml`. Both labels then become `alloy`. Metrics
pause briefly with `alloy-node`; its original host storage and WAL are preserved.
Alloy roots, Promtail state and metrics WAL are mounted read-only. Only the
unused file-source component and separate evidence directory are writable.

## Deploy and verify

Deploy this branch through the normal reviewed Flux workflow. The active Job is
owned by the existing Alloy Kustomization. That Kustomization's existing Helm
health checks do not certify the Job or log continuity; inspect both explicitly:

```sh
kubectl -n monitoring logs -f job/wyrm2-alloy-canary
kubectl -n monitoring wait --for=condition=complete job/wyrm2-alloy-canary --timeout=15m
kubectl get node wyrm2 -o jsonpath='{.metadata.labels}'
kubectl -n monitoring get pods --field-selector spec.nodeName=wyrm2 -o wide
kubectl -n loki get pods --field-selector spec.nodeName=wyrm2 -o wide
```

Expect both labels `alloy`, one Ready `alloy-node`, one Ready `alloy-journal`, and
neither old Promtail on wyrm2. Preserve Job logs and host evidence under
`/var/lib/promtail-alloy-wyrm2-canary/attempt`. Files are write-once, mode `0400`,
and `snapshots.json` records SHA-256 checksums; the live shared checkpoint remains
untouched. `activated.json` records the transition time, `ready.json` only Pod
readiness. The script has no log-content/Loki access or permissions to emit tests.

Query Loki over the full interval spanning the pause with the recorded test ID.
Check every numbered record (duplicates allowed), CRI timestamps and partial
messages, stdout/stderr `stream`, and the bounded labels `app`, `instance`,
`component`, `node_name`, `namespace`, `job`, `pod`, `container`; no `filename`.
Check journal `job=systemd-journal`, `unit`, `node`, `level` and kernel coverage.
Inspect Alloy/Loki errors, dropped/rejected samples, retry/backlog and replay
load. Record any missing records explicitly; readiness does not establish
continuity. Confirm normal metrics resume and existing WAL catches up.

With the canary still emitting, test one collector restart at a time and normal
log rotation. Verify new Alloy positions continue without a first-start replay;
**do not delete state or rerun the Job**. Observe normal-load CPU, memory/page
cache, disk reads/pressure and etcd fsync/lease latency. Probe Vector/Talos,
syslog and OTLP separately. Save the complete evidence before considering another
node; this PR contains no gate for any other node and does not retire Promtail.

## Failure, retry and rollback

No failure automatically changes a label back. The Job has `backoffLimit=0`,
`restartPolicy=Never`, a 15-minute deadline, no TTL and no Flux `force` annotation.
Its fixed name and host attempt fence prevent reimports or automatic reruns.
Future immutable-template edits require an explicit operator decision; Flux
must not delete/recreate this Job to retry it blindly.

Inspect the authoritative Node labels, live collectors, Job logs and evidence;
a crash can happen after a label change but before its evidence file is written:

| Actual gates        | Recovery                                                                                                                                                                                                                                                                       |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| absent / `promtail` | No drain occurred. Resolve the preflight failure. If an attempt fence exists, preserve it and inspect the failed operation before any retry.                                                                                                                                   |
| both `drained`      | Keep them drained while investigating. Verify all three writers really stopped. Preserve all state and snapshots. A deliberately supervised completion can set both labels `alloy` only after repeating the unused-state/import checks and confirming the replay limits above. |
| both `alloy`        | Preserve active Alloy state and diagnose startup/ingestion. A failed readiness wait must not reset positions or automatically re-enable Promtail.                                                                                                                              |
| mixed/unknown       | Stop and investigate concurrent operations; do not run another handoff.                                                                                                                                                                                                        |

If aborting while drained before Alloy ever started, the old Promtail checkpoint
is unchanged; only after confirming no Alloy writer exists may an operator set
both labels back to `promtail`. Its shared checkpoint is still not independent
process state, so replay/continuity must be checked again. Never clear directories.

After Alloy has started, rollback requires a separately supervised replay choice:
set both labels `drained`, wait for both Alloy Pods and both old Promtails to be
absent while wyrm2 stays Ready, preserve the new Alloy positions, then restore
both labels to `promtail`. Promtail cannot import Alloy positions; its old shared
checkpoint can replay the intervening retained window or be incomplete. Verify
the canaries again. Keep WAL, all legacy/new state, and evidence intact.

The Job deliberately refuses any attempt with existing evidence. To retry a
pre-drain-only failure, first prove no label changed and no Alloy log writer ever
started, archive the entire evidence directory outside the mounted path, then
explicitly delete the failed Job so Flux recreates it. After drain or activation,
do not retry this first-start Job; use the supervised recovery above. Removing
this canary's Job/RBAC later does not roll back node labels or delete host state.
