# Alloy replay cutover

Flux sets the inventoried nodes' pod and journal collector labels to `alloy`.
The existing gates remove Promtail Pods, enable pod-log targets in each running
`alloy-node`, and start `alloy-journal` on NixOS. Talos journals continue through
Vector. Node Alloy Pods remain scheduled: no metrics restart or WAL reset is
required. Both old releases remain installed for per-node rollback.

Each partial Node manifest contains only the two labels and Flux's native
`kustomize.toolkit.fluxcd.io/prune: disabled` annotation. Flux reconciles those
labels without managing the Node's spec, status, or unrelated labels. The annotation protects the Node
from Flux pruning. No activation Job or manual checkpoint import/reset is
required; collectors keep their normal persistent positions and WAL writes.

This cutover accepts replay and transient overlap while the old collectors stop
and the new targets start. It does not import the shared Promtail checkpoint.
Alloy reads retained files from the beginning when no offset exists, including
when its positions file contains `positions: {}`; that file needs no removal.
Existing offsets resume normally and must be preserved. Journal Alloy uses its
existing cursor if present, otherwise its existing `max_age=12h` replay window.

Only retained files for discoverable Pods can be replayed; deleted short-lived
Pods may no longer be discoverable. Rotation, journal retention, and Loki's age,
out-of-order and rate limits can make records unrecoverable. This cutover does
not promise gap-free or exactly-once delivery. The checkpoint-import procedure
in [the cutover guide](promtail-alloy-cutover.md) is a separate option and is not
required here.

## Before deployment

Keep each node online and test Pods alive throughout its verification. Record collector
readiness, Alloy metrics/WAL health, resource pressure, retained-file/journal
bounds and Loki limits; allow headroom for replay. Compare the legacy relabel
configuration with [`pod-logs.alloy`](../pod-logs.alloy) and
[`journal.alloy`](../journal.alloy).

Use a long-lived test Pod pinned to each online node to emit numbered, uniquely tagged
stdout/stderr records before, during and after deployment, including partial
CRI records. Emit tagged journal records on NixOS, for example
`logger -t alloy-canary 'UNIQUE-ID sequence=N phase=before'`, and repeat
during and after the change. Save the test ID and pre-deployment Loki results.

## Deploy and verify

Deploy through the normal reviewed Flux workflow. Check the authoritative Node
labels and actual collectors; Flux/Helm readiness does not establish log delivery:

```sh
kubectl get nodes -L observability.allegedly.works/pod-collector,observability.allegedly.works/journal-collector
kubectl -n monitoring get pods -o wide
kubectl -n loki get pods -o wide
```

Expect both collector labels `alloy`, one Ready `alloy-node` per online node,
one Ready `alloy-journal` per online NixOS node, and neither Promtail on opted-in
nodes. Talos must have no `alloy-journal`. Confirm each existing node Alloy
Pod UID and restart count remain unchanged, metrics continue, and WAL health
remains normal.

Both journal collectors select NixOS using NFD's
`feature.node.kubernetes.io/system-os_release.ID=nixos` label. Confirm this label
is present too: the collector gate alone does not schedule the journal Pod.
The offline laptops, rugged and iguana, are opted in but remain excluded by
`node.kubernetes.io/out-of-service:NoExecute`. Verify their pod and journal
delivery when they reconnect; their labels alone do not complete verification.

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
OTLP separately. Save the evidence before retiring Promtail. Fleet retirement
waits for every node, including returning laptops.

## Rollback

Keep the affected partial Node manifests in `alloy.py` and change **both** collector label
values to `promtail`; split a node out of the loop if needed for per-node rollback.
Deploy that Git change through Flux. Preserve the native
prune-disabled annotation. The existing gates stop Alloy journal collection,
remove Alloy pod-log targets, and restore both Promtails while the node Alloy
metrics Pod continues running. Verify actual collector termination/startup and
the numbered canaries again. Transient overlap and replay are possible: Promtail
cannot import Alloy positions, and its two processes share their old checkpoint.
Preserve all Promtail/Alloy positions and the metrics WAL.

Deleting this manifest is intentionally **not** rollback: pruning protection
preserves the Node and its last-applied collector labels. Restore `promtail`
declaratively before any later ownership cleanup.
