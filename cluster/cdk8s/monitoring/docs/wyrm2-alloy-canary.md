# wyrm2 Alloy replay canary

Deploying this change through Flux sets wyrm2's pod and journal collector labels
to `alloy`. The existing gates remove its two Promtail Pods, enable pod-log
targets in the running `alloy-node`, and start `alloy-journal`. The node Alloy
Pod remains scheduled: no metrics restart or WAL reset is required. Other nodes
remain on Promtail, and both old releases remain installed.

The partial `Node/wyrm2` manifest contains only the two labels and Flux's native
`kustomize.toolkit.fluxcd.io/prune: disabled` annotation. Flux reconciles those
labels without managing the Node's spec, status, or unrelated labels. The annotation protects the Node
from Flux pruning. No activation Job or manual checkpoint import/reset is
required; collectors keep their normal persistent positions and WAL writes.

This canary accepts replay and transient overlap while the old collectors stop
and the new targets start. It does not import the shared Promtail checkpoint.
Alloy reads retained files from the beginning when no offset exists, including
when its positions file contains `positions: {}`; that file needs no removal.
Existing offsets resume normally and must be preserved. Journal Alloy uses its
existing cursor if present, otherwise its existing `max_age=12h` replay window.

Only retained files for discoverable Pods can be replayed; deleted short-lived
Pods may no longer be discoverable. Rotation, journal retention, and Loki's age,
out-of-order and rate limits can make records unrecoverable. This canary does
not promise gap-free or exactly-once delivery. The checkpoint-import procedure
in [the cutover guide](promtail-alloy-cutover.md) is a separate option and is not
required here.

## Before deployment

Keep wyrm2 online and test Pods alive throughout the canary. Record collector
readiness, Alloy metrics/WAL health, resource pressure, retained-file/journal
bounds and Loki limits; allow headroom for replay. Compare the legacy relabel
configuration with [`pod-logs.alloy`](../pod-logs.alloy) and
[`journal.alloy`](../journal.alloy).

Use a long-lived test Pod pinned to wyrm2 to emit numbered, uniquely tagged
stdout/stderr records before, during and after deployment, including partial
CRI records. Emit tagged journal records on wyrm2, for example
`logger -t alloy-wyrm2-canary 'UNIQUE-ID sequence=N phase=before'`, and repeat
during and after the change. Save the test ID and pre-deployment Loki results.

## Deploy and verify

Deploy through the normal reviewed Flux workflow. Check the authoritative Node
labels and actual collectors; Flux/Helm readiness does not establish log delivery:

```sh
kubectl get node wyrm2 -o jsonpath='{.metadata.labels}'
kubectl -n monitoring get pods --field-selector spec.nodeName=wyrm2 -o wide
kubectl -n loki get pods --field-selector spec.nodeName=wyrm2 -o wide
```

Expect both collector labels `alloy`, one Ready `alloy-node`, one Ready
`alloy-journal`, and neither Promtail on wyrm2. Confirm the existing node Alloy
Pod UID and restart count remain unchanged, metrics continue, and WAL health
remains normal.

Query Loki across the full change interval using the test ID. Check every
numbered record (duplicates allowed), timestamps, partial CRI messages,
stdout/stderr `stream`, and the bounded labels `app`, `instance`, `component`,
`node_name`, `namespace`, `job`, `pod`, `container`; no `filename`. Check journal
`job=systemd-journal`, `unit`, `node`, `level` and representative kernel messages.
Inspect Alloy/Loki errors, dropped/rejected records, retries, backlog and replay
load. Record missing records explicitly.

After stable ingestion, verify collector restart and normal rotation preserve
progress without resetting positions. Observe normal-load CPU, memory/page
cache, disk pressure and etcd fsync/lease latency. Probe Vector/Talos, syslog and
OTLP separately. Save the evidence before expanding to another node or retiring
Promtail.

## Rollback

Keep the partial Node manifest in `alloy.py` and change **both** collector label
values to `promtail`; deploy that Git change through Flux. Preserve the native
prune-disabled annotation. The existing gates stop Alloy journal collection,
remove Alloy pod-log targets, and restore both Promtails while the node Alloy
metrics Pod continues running. Verify actual collector termination/startup and
the numbered canaries again. Transient overlap and replay are possible: Promtail
cannot import Alloy positions, and its two processes share their old checkpoint.
Preserve all Promtail/Alloy positions and the metrics WAL.

Deleting this manifest is intentionally **not** rollback: pruning protection
preserves the Node and its last-applied collector labels. Restore `promtail`
declaratively before any later ownership cleanup.
