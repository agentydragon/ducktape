# Root cause: Kimsufi node NotReady flaps — promtail page-cache thrash starving etcd

**First captured:** 2026-05-31 in
`cluster/debug/2026-05-31-kimsufi-worker-1-notready/` (symptom capture only).
**Root-caused:** 2026-06-08, recurring on `ovh-ns103656` + `ovh-ns104963`.

## Symptom

OVH Kimsufi nodes flap `Ready -> NotReady -> Ready` in short (2–5 min) episodes,
separated by 1–6 h healthy gaps. Episodes evict pods (props backend, CNPG primary,
graders). etcd shows `apply request took too long` / `waiting for ReadIndex response
took too long`; apiserver↔etcd p99 sits at ~4.5 s baseline with 10–45 s spikes all day.
The two flapping nodes drop in lockstep (shared etcd/apiserver dependency, not per-node
network).

## Root cause

Promtail's HelmRelease had `resources.limits.memory: 128Mi` — the grafana/promtail
chart's **commented-out example value, uncommented** (chart default is `resources: {}`,
i.e. no limit).

In cgroup v2 the page cache for files a container reads is charged to that container's
`memory.max`. Promtail tails every container log on the node; on the busiest node
(`ovh-ns103656`: kube-apiserver, cilium-agent, a stack of CNPG DBs) the tail working set
exceeds the ~42 MB of cache headroom left inside the 128 MiB cap. The kernel evicts log
pages the instant they're read, and **readahead re-fetches them from disk on every
access**.

Measured on `103656` (2026-06-08):

- `/proc/1/io`: `rchar` = 66 MB (logical) vs `read_bytes` = **23.8 TB** (physical) — ~360,000:1.
- cgroup `memory.current` 127.6 MiB / `memory.max` 128 MiB (pinned at limit).
- `workingset_refault_file` = 5.8 B, `pgscan_direct` = 6.3 B (all reclaim is direct;
  `kswapd` = 0), `memory.events max` = 311 k (throttled, never OOM-killed).
- `sda`: 94% util, 60% iowait, 391 read IOPS, ~280 MB/s — **constant** (median 61% iowait
  over 12 h). Every other node ~0%.

The read storm shares the system disk with etcd's WAL, so etcd fsync/raft-apply queue
behind it → apiserver↔etcd latency in seconds → kubelet node-lease renewals occasionally
miss the deadline → NodeReady flaps. Load-dependent, hence the recurrence: it triggers
whenever a node accumulates enough chatty logging pods.

## Fix

`cluster/k8s/monitoring/loki/promtail-helmrelease.yaml`: raised memory request to 256Mi
and limit to 1Gi (was 64Mi / 128Mi) so the tail working set can stay cached. A log
shipper must not have its page cache capped near its working set.

## Secondary issues found (separate from this incident)

- **MTU mismatch (latent):** `cilium_vxlan` MTU 1412 but `nebula1` MTU 1300 (default,
  never set). VXLAN rides inside Nebula, so full-size pod packets (1412+50=1462) exceeded
  nebula1's 1300 and fragmented/PMTUD-clamped. **Root-caused + fixed 2026-06-08**: underlay
  measured to carry 1500 (incl. .254 hairpin), Nebula overhead measured at 60 bytes, so set
  `nebula1=1420` / Cilium `MTU=1420` (Cilium's MTU is the underlay/nebula1 MTU; it subtracts
  the 50-byte VXLAN overhead itself → 1370 cross-node pod MTU). See
  <../2026-06-08-nebula-vxlan-mtu/>.
- **`wyrm2` dead since 2026-05-10** (`Ready=Unknown`): every OVH node's nebula spams
  "Handshake timed out" to `10.42.0.20` every ~7 s. Remove it from the cluster + nebula
  roster.
- **Co-location:** etcd voter + CNPG **primary** (props-db-3) + loki-write all share one
  Kimsufi system disk on `103656`, which is what gives the promtail storm its blast
  radius. Consider spreading these.
