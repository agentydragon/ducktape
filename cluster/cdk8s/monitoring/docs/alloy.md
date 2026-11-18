# Alloy log collection

`alloy-node` collects pod stdout/stderr on every node, alongside kubelet metrics.
`alloy-journal` collects all system journal records on NixOS nodes selected by NFD's
`feature.node.kubernetes.io/system-os_release.ID=nixos` label. A separate Alloy DaemonSet receives Talos host logs over loopback TCP; it
keeps the former `vector-talos-logs` resource identities for an in-place rollout.
Central Alloy handles Kubernetes events and OTLP; the syslog release handles the
home switch and gateway.

The Promtail releases and temporary collector-label gates have been retired.
New nodes need no collector opt-in. Offline nodes start Alloy when they return;
their logs can only be recovered while the source files/journal still retain them.
The first read can replay retained pod files; journal reads are bounded by `max_age`.

## Talos host logs

[`talos.alloy`](../talos.alloy) uses `otelcol.receiver.tcplog` (needs `--stability.level=experimental`):
Talos sends newline-delimited JSON, not syslog, and `loki.source.syslog` closes idle connections after 120 s
(`0` selects that default instead of disabling it; Alloy 1.21.1 and 1.22.0), while Talos 1.12.3 and 1.14.0
notice a closed connection only after a failed write, so the next event is lost. Each raw line reaches Loki as
`{job="talos-node", node}` at receipt time; no JSON field becomes a label, so filter with `| json`, and Talos's
own time stays in `talos-time`. The maximum line size is the `tcplog` default of 1 MiB. Delivery is best
effort: a TCP write is not an acknowledgement, so a receiver restart can drop in-flight events, and the Loki
writer's queue is in memory (indefinite retries, including HTTP 429; no WAL or volume). Keep `IMAGE` in
`talos_logs.py` and the `alloy_config` pin in `devinfra/bazel/oci.MODULE.bazel` on one digest so
`test_talos_logs` exercises the deployed image.

## State and verification

Preserve `/var/lib/alloy-node` and `/var/lib/alloy-journal`. They hold Alloy's
positions/cursors and the metrics remote-write WAL. Keep component names
`loki.source.file.pods`, `loki.source.journal.journal` and
`prometheus.remote_write.mimir` stable so restarts reuse that state. Retirement
does not delete host files, including old Promtail checkpoints.

After a rollout, check one Ready `alloy-node` per online node, one Ready
`alloy-journal` per online NixOS node and one Ready `vector-talos-logs` per online
Talos node (no parse/send failures in its log). Query recent Loki logs by `node_name`
for pod stdout/stderr, by `job="systemd-journal", node` for journals and by
`job="talos-node", node` for Talos, where `| json` shows the service fields. Verify
static-pod logs too. Check fresh kubelet/cAdvisor samples in Mimir, remote-write
failures and queue lag, collector memory, and etcd disk latency. Repeat delivery
checks after a collector restart, an offline node's return or, for Talos, a quiet
interval. Readiness alone does not prove delivery.

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
