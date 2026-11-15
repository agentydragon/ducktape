# Alloy log collection

`alloy-node` collects pod stdout/stderr on every node, alongside kubelet metrics.
`alloy-journal` collects all system journal records on NixOS nodes selected by NFD's
`feature.node.kubernetes.io/system-os_release.ID=nixos` label. Talos host logs still
arrive through Vector. Central Alloy handles Kubernetes events and OTLP; the syslog
release handles the home switch and gateway.

The Promtail releases and temporary collector-label gates have been retired.
New nodes need no collector opt-in. Offline nodes start Alloy when they return;
their logs can only be recovered while the source files/journal still retain them.
The first read can replay retained pod files; journal reads are bounded by `max_age`.

## State and verification

Preserve `/var/lib/alloy-node` and `/var/lib/alloy-journal`. They hold Alloy's
positions/cursors and the metrics remote-write WAL. Keep component names
`loki.source.file.pods`, `loki.source.journal.journal` and
`prometheus.remote_write.mimir` stable so restarts reuse that state. Retirement
does not delete host files, including old Promtail checkpoints.

After a rollout, check one Ready `alloy-node` per online node and one Ready
`alloy-journal` per online NixOS node. Query recent Loki logs by `node_name` for pod
stdout/stderr and by `job="systemd-journal", node` for journals. Verify static-pod
logs too. Check fresh kubelet/cAdvisor samples in Mimir, remote-write failures and
queue lag, collector memory, and etcd disk latency. Repeat delivery checks after a
collector restart or an offline node's return. Readiness alone does not prove delivery.

Keep the collectors' page-cache memory headroom: the
[etcd I/O incident](../../../docs/lessons_learned/2026_06_19_etcd_hdd_io_contention.md)
documents why low tailer limits can starve etcd.

## Rollback through Flux

Revert the retirement change to restore the Promtail HelmReleases, network rule,
and collector gates. In the restored partial Node manifests, set **both**
`observability.allegedly.works/pod-collector` and
`observability.allegedly.works/journal-collector` to `promtail` for the affected
nodes. Commit and deploy through Flux; verify fresh pod/journal delivery again.
Promtail cannot import Alloy positions, so old checkpoints may replay duplicates.
Preserve both collectors' state and the metrics WAL throughout rollback.

The removed partial Node manifests carried
`kustomize.toolkit.fluxcd.io/prune: disabled`; Flux leaves the live Nodes intact.
Their last collector labels and prune annotations can remain harmlessly on the
Nodes. Restoring the manifests on rollback gives Flux ownership of those labels
again; dropping a protected manifest does not clear its labels.
