# talos-kimsufi-worker-1 NotReady capture

Captured on 2026-05-31 before rebooting `talos-kimsufi-worker-1`
(`147.135.39.176`, `10.42.0.14`).

> **Root cause (identified 2026-06-08):** this was the first symptom capture of a
> recurring syndrome — promtail page-cache eviction starving etcd of IO, which
> surfaced as node-lease misses and `NotReady` flaps. Full RCA + fix in
> <../2026-06-10-etcd-io-contention/promtail-page-cache-etcd-starvation.md>. The
> raw capture files (kubectl/talosctl dumps, Loki and Mimir query responses,
> node-exporter scrapes) were dropped once the RCA landed; what is kept below is
> the narrative, the visibility gap that still needs closing, and the CNPG
> `pg_create_restore_point()` recovery recipe, which is still current.

## Summary

The host was still alive on the public network, but Talos and Kubernetes control
paths on the node were wedged:

- Kubernetes node heartbeat stopped at `2026-05-31T11:39:27Z`; the node lease
  stopped at `2026-05-31T11:41:51Z`.
- `talosctl` works against the other Talos nodes but fails only for
  `147.135.39.176` with:
  `read unix @->/system/run/machined/machine.sock: use of closed network connection`.
- The public node-exporter endpoint `147.135.39.176:9100` responded during
  capture, so the machine was not powered off.
- The Nebula/internal address `10.42.0.14` timed out on `50000`, `7445`, `10250`,
  and `9100`.
- Loki shows on-node kube-scheduler timing out against KubePrism
  `https://127.0.0.1:7445` at `2026-05-31T11:40:43Z`.
- Loki shows on-node kube-apiserver handler timeouts and etcd-client context
  cancellations around `2026-05-31T11:41:41Z` to `2026-05-31T11:41:50Z`.

## Visibility gap

The missing evidence is the node-local Talos runtime state: `machined`, kubelet,
and kernel logs from the wedged node. Those could not be captured because every
Talos API command against the affected node fails before it can read service logs
or dmesg. `talosctl dmesg` is not expected to survive a reboot.

## Reboot

Talos API reboot failed with the same `machined` socket error. OVH provider-side
hard reboot was issued for `ns103711.ip-147-135-39.us` at
`2026-05-31T13:21:36Z`; OVH task `28566944` completed at
`2026-05-31T13:22:16Z`. The node returned to Kubernetes `Ready` with a fresh
kubelet heartbeat by `2026-05-31T13:27:26Z`.

The post-reboot dmesg includes XFS recovery for the Talos ephemeral/data
volumes and a kernel warning at `2026-05-31T13:25:37Z` from `cilium-agent`
loading a BPF program:
`verifier bug: REG INVARIANTS VIOLATION`. This happened after the reboot, so it
does not prove the pre-reboot outage cause, but it is relevant because Cilium on
this node also logged odd address fallback behavior before the node went
`NotReady`.

## CNPG replica recovery

After the node returned, three CNPG replicas on the rebooted node remained
`0/1` for roughly 30 minutes:

- `airlock/airlock-db-1`
- `nix-cache/attic-db-1`
- `study-casino/study-casino-db-1`

Each pod had been an old primary. CNPG ran `pg_rewind`, the instance entered
standby mode, and the new primary was healthy on `*-db-2`, but the standby was
stuck just short of its minimum recovery ending LSN. Example:
`airlock-db-1` needed `0/15000028`, while the primary had only sent and the
replica had only replayed `0/15000000`; `pg_current_wal_insert_lsn()` on the
primary was already `0/15000028`.

`pg_switch_wal()` did not help because the primaries were idle at the WAL segment
boundary. Creating one restore-point WAL record on each primary advanced WAL and
unblocked recovery:

- `select pg_create_restore_point('cnpg_recovery_nudge_20260531_airlock');`
- `select pg_create_restore_point('cnpg_recovery_nudge_20260531_attic');`
- `select pg_create_restore_point('cnpg_recovery_nudge_20260531_study_casino');`

After that, `airlock-db`, `attic-db`, and `study-casino-db` all returned to
`2/2` ready and `Cluster in healthy state`.

## Follow-up: durable host logs

This incident needs durable host-level log capture before the next similar
reboot. Promtail/Loki captured pod logs, but not enough Talos host context to
recover kernel ring buffer, `machined`, kubelet, containerd, or KubePrism state
after the Talos API path wedged.

A v0 design should run a privileged node-local collector on Talos nodes that
streams host journal/kernel logs into Loki or another durable store with labels
for `node`, `boot_id`, `talos_service`, and `source`. It should include dmesg or
equivalent kernel-ring-buffer content early after boot and should not depend on
`talosctl` being available during an incident.
