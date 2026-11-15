# Promtail → Alloy: staged node-by-node cutover

**Live continuity verification and final Promtail retirement are pending.** Do
not remove the old releases merely because Helm or render checks succeed.

The [wyrm2 canary](wyrm2-alloy-canary.md) uses Flux-managed Node labels to enable
pod and journal logs on one node. It accepts retained-file duplicates and up to
12h journal replay without importing checkpoints or resetting Alloy state. The
checkpoint-import procedure below is a separate migration option.

Pod-file logs join the existing `alloy-node` process and its persistent host
storage. A separate `alloy-journal` runs only on opted-in NixOS nodes: journal
mounts cannot be conditional within the all-node DaemonSet's Pod template. It
needs no Kubernetes API token/RBAC and has read-only journal/machine-id mounts,
root with all capabilities dropped, no host network/PID namespace, and its own
persistent cursor directory. This preserves kernel/GPU messages without granting
journal access to Talos. Talos/Vector, central OTLP/events, syslog, and Loki backend
configuration keep their existing paths.

## States and prerequisites

Each Node has independent `observability.allegedly.works/pod-collector` and
`observability.allegedly.works/journal-collector` labels:

| Value                  | Promtail                  | Alloy                                                        |
| ---------------------- | ------------------------- | ------------------------------------------------------------ |
| absent (or `promtail`) | continues                 | metrics continue; pod targets filtered out; no journal pod   |
| `drained`              | DaemonSet removes its pod | pod label stops node Alloy; no journal pod                   |
| `alloy`                | excluded                  | node-local pod targets enabled / NixOS journal pod scheduled |

For checkpoint import, use `drained` as a real checkpoint before `alloy`.
The linked wyrm2 replay canary changes directly to `alloy` with duplicates and
transient overlap accepted; it does not require a metrics restart or state reset.
Manage collector labels through partial, prune-disabled Node manifests in Flux;
keep existing label ownership in one place. Journal opt-in additionally requires
NFD's `feature.node.kubernetes.io/system-os_release.ID=nixos`. Offline roaming nodes stay on Promtail until they return and are individually migrated. Keep both
legacy releases and their Loki ingress permission until every node is verified.
The existing [roaming node controller](../../../docs/plans/roaming_nodes.md)
marks offline laptops `node.kubernetes.io/out-of-service:NoExecute`; the new
journal DS does not tolerate that taint. Verify this exclusion and ordinary
Helm/DaemonSet rollout health before cutover. An offline node
cannot be certified while it remains offline.

Before starting, record the actual deployed chart/image/config versions, labels,
file paths, resource usage, container IDs and node readiness. Confirm the Alloy
image supports `legacy_positions_file` and journal `legacy_position` (the new
configuration is validated against v1.21.1). Check the actual Promtail 6.x relabel
rules against `pod-logs.alloy`; customizations in a deployed chart require review.

**For checkpoint import, inspect positions before draining.** Both current
Promtail chart values default to host `/run/promtail/positions.yaml`, including journal Promtail. Do not assume
two independent files exist or that one snapshot contains both file offsets and
the `journal` job cursor. Record each process's configured path and inspect the
host file; retain separate immutable pod/journal snapshots and checksums. If they
share a path or either checkpoint is missing/stale, stop the migration until an
operator has isolated and verified the checkpoints, or explicitly choose the
replay fallback below. A render cannot establish which state was last written.

## Checkpoint import, one online node at a time

1. Pick one ordinary worker, then a control-plane node, then a NixOS node before
   any roaming laptop. Record Loki's latest timestamp/line and existing label sets
   for representative pod stdout/stderr, multiline/partial CRI records, static
   pods, and (on NixOS) systemd and kernel logs. Emit unique numbered canary
   records before, during and after the handoff. Keep the node online and ensure
   log rotation/retention cannot expire data during the checkpoint pause.
2. Set the relevant label to `drained` in the node's Flux-managed partial Node
   manifest and deploy that Git change. Keep the native
   `kustomize.toolkit.fluxcd.io/prune: disabled` annotation throughout migration
   and rollback. For pod migration this also stops that node's Alloy metrics
   process. Wait for both Pods to terminate gracefully and flush clients and final
   checkpoints. Verify actual Pod termination, not just Helm Ready.
   Do not force-delete a running collector or snapshot a file still being written.
   Drain both old collectors together if they share host state.
3. Back up the final legacy state outside the collector directories. Copy the
   verified pod checkpoint atomically to
   `/var/lib/alloy-node/legacy-pod-positions.yaml`, and journal checkpoint to
   `/var/lib/alloy-journal/legacy-journal-positions.yaml` on that same node.
   Preserve original files and permissions. Confirm each recorded path maps to
   the same `/var/log/pods/.../*.log` file and byte offset; confirm the journal
   cursor names the old `journal` job and is still seekable in the local journal.
4. **Pod first activation only:** the inactive Alloy file component may already
   have created an empty new positions file. Import happens only when the new
   file does not exist. The `drained` pod-collector label also excludes this node
   from `alloy-node`; confirm its real Pod has stopped. Back up and remove only
   `/var/lib/alloy-node/loki.source.file.pods/positions.yml` if it is demonstrably
   unused (no prior Alloy log collection). Never clear `/var/lib/alloy-node`,
   other component directories, or `prometheus.remote_write.mimir` WAL. Set the
   pod-collector label to `alloy` in the same Node manifest and deploy through
   Flux; the DaemonSet restarts `alloy-node`.
   Retain its release name and storage path. The temporary metrics
   pause must also be checked after it resumes. Do not do this reset on subsequent
   restarts: Alloy's own positions take precedence over the legacy snapshot.
5. Journal activation has no pre-created state because its DS is not scheduled
   until opt-in. Check that its new cursor directory has no prior run, then set
   `observability.allegedly.works/journal-collector=alloy` in that Node manifest
   and deploy through Flux. Alloy imports the
   `journal` cursor. Keep `max_age=12h`, message format and `job=systemd-journal`,
   `unit`, `node`, `level` labels. Confirm the actual importer logs and new files;
   missing/invalid legacy files can silently cause a fresh start, not a safe handoff.
6. Query Loki over an interval spanning the handoff. Compare numbered canaries,
   timestamps and full messages, useful labels (`app`, `instance`, `component`,
   `node_name`, `namespace`, `job`, `pod`, `container`, CRI `stream`), and journal
   labels. Confirm no `filename` label and that CRI timestamps/partial records
   remain correct. Check restart and rotation continuation, failed/retried pushes,
   client drops, collector health and actual node placement. Probe Talos/Vector,
   both syslog inputs and OTLP independently to exclude collateral failures.
7. Observe memory/page cache, CPU, disk reads/pressure and etcd fsync/lease latency
   under the node's normal tail workload. The combined node process requests
   512Mi (old metrics 256Mi + old pod-tail 256Mi), with a 2Gi memory ceiling;
   journal retains its 256Mi request/512Mi ceiling. During preparation the old
   and new requests coexist: check scheduling capacity before rollout. See
   [the etcd I/O incident](../../../docs/lessons_learned/2026_06_19_etcd_hdd_io_contention.md).
   Check the existing metrics WAL still catches up after an outage. A healthy
   parser or Pod alone establishes none of these properties.

The state directory spelling must be checked against the deployed Alloy image
before touching any file. If it differs, stop and update the procedure. Archive
the checkpoint/canary evidence per node, including the image digest and cutover
times. Only then repeat for other nodes, including each laptop when it returns.

## Replay fallback, rollback and retirement

Neither collector provides an exactly-once transaction between its checkpoint and
Loki acknowledgement. A graceful checkpoint handoff minimizes overlap; retries or
an unacknowledged final batch can still duplicate records. It cannot prove exactly
once. Never tail from end merely to avoid duplicates: that can skip retained logs.

If a usable cursor/offset cannot be established, the conservative fallback is to
replay retained pod files from the beginning and the journal's existing 12h window,
accepting duplicate records across that window. Loki's existing age/rate limits
still apply; data already rotated away or outside those limits cannot be recovered
by this migration. Do not activate this fallback without documenting its exact
window and confirming every pre-cutover canary remains queryable. If that cannot
be demonstrated, leave Promtail running and report the node blocked.

For the direct Flux replay canary and its rollback, follow the
[wyrm2 guide](wyrm2-alloy-canary.md); no checkpoint import/reset is required.

For checkpoint-import rollback, first set the affected labels to `drained` in
their Flux-managed Node manifests, deploy, verify the new sources stopped, and
preserve their state. Promtail cannot import Alloy positions. Reusing an old checkpoint replays from that
checkpoint (bounded by rotation/retention); it may duplicate the whole intervening
window. Never run both against production streams to infer correctness from a
higher rate. Restore the old label only after choosing and recording that replay
window. Restore `promtail` in those same manifests and deploy through Flux;
deleting a prune-disabled Node manifest intentionally preserves its labels and
is not rollback. Do not delete any state as part of rollback.

After all nodes, including returning roaming nodes, pass the live checks in a
separate operator-reviewed retirement change: delete `_promtail_values`,
`_promtail_journal_values`, `_legacy_collector_affinity`, their two HelmRelease
calls, `journal-scrape.yaml` and its Bazel data; remove only Promtail's Loki ingress
rule and its roaming-test entries. Keep all Alloy/Vector/syslog/OTLP permissions,
node labels and both legacy snapshots until the rollback window closes. Do not
move Loki or its persistent resources between Flux Kustomizations. Confirm the
Helm uninstalls leave host checkpoint/log/WAL data intact, and check the same
canaries after retirement. LOC savings from removing Promtail are deferred until
verified retirement.
