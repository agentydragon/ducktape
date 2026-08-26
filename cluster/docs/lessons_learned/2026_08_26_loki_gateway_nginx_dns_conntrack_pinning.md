# loki-gateway nginx resolver pinned to a dead CoreDNS pod IP

**Date**: 2026-08-26. **Status**: Fixed (ducktape#4750).

## Symptom

`loki-gateway`'s nginx silently failed to resolve `loki-read`/`loki-write` for an
unknown duration. Its error log:

```text
loki-read.loki.svc.cluster.local could not be resolved (110: Operation timed out)
```

`loki-canary`'s `/tail` and `/query_range` traffic got real `502`s. Anything else
routed through `loki-gateway` (e.g. `loki-read-proxy`) failed outright — with no
signal distinguishing it from Loki actually being down, since `loki-read`,
`loki-write`, and CoreDNS were all fully healthy throughout.

## Root cause

Confirmed at the kernel level: `/proc/<pid>/net/udp` on every nginx process
(master + all workers — they share the same pre-fork sockets) showed every
resolver UDP socket `connect()`-ed to a specific dead pod IP on a node running
no CoreDNS pod. `cilium monitor` independently caught the same traffic being
dropped as `Policy denied` (the address resolved to no live identity).

Mechanism: nginx reuses one fixed UDP source port for every DNS query it sends.
Kubernetes' conntrack-based Service NAT picks a backend once for that 5-tuple
and keeps reusing the decision on every later packet, since ongoing traffic
just refreshes the conntrack entry instead of ever re-evaluating it. When the
CoreDNS pod originally selected gets rescheduled, nginx keeps sending to the
now-dead pod IP forever — invisible to CoreDNS (packets never arrive) and
invisible to Cilium's own service/endpoint tables (both stay correct). Only a
gateway restart or `SIGHUP` re-triggers resolution and lands on a live
backend — until the next CoreDNS reschedule repeats it.

This is nginx-vs-Kubernetes-DNAT behavior, not a Cilium/Talos/Terraform
misconfiguration here: `cilium-health status`, BPF conntrack fill, the
`kube-dns` EndpointSlice, and Cilium's own service/backend table were all
clean throughout the investigation. It matches
[grafana/loki#14013](https://github.com/grafana/loki/issues/14013) and
<https://gist.github.com/joemiller/68ab3f7a7a08e4a9d5ad5d023cb14fc2> exactly,
down to an identical log line — a known upstream pattern, not novel to this
cluster.

## Fix

1. **`loki-read-proxy` now points at `loki-read` directly**, bypassing
   `loki-gateway` entirely — it only ever queries, so it never needed
   gateway's read/write path routing. Matches how Grafana's own datasource
   already reached Loki.
2. **`loki-gateway` got a `dnsmasq` sidecar** (`gateway.extraContainers` +
   `gateway.nginxConfig.resolver: "127.0.0.1:8053"` in
   `cluster/k8s/monitoring/loki/helmrelease.yaml`). dnsmasq opens a fresh UDP
   flow per query instead of reusing one fixed source port, so it never falls
   into the conntrack-pinning trap. It forwards via its own
   `/etc/resolv.conf` (the kubelet-injected kube-dns ClusterIP), so no DNS
   server address is hardcoded anywhere.

## Key Lessons

1. **A component being "DNS-healthy" isn't binary.** CoreDNS, Cilium's
   service tables, and conntrack can all be completely correct while one
   specific long-lived client socket is permanently wrong — check the
   client's own actual state (`/proc/pid/net/udp`, `strace`), not just the
   server side, when a resolution failure looks intermittent or inexplicable.
2. **A resolve-once-and-hold client is exposed to backend churn forever**,
   not just at the moment of churn. The bug here was days-to-weeks old by
   the time it was noticed; nothing about it would ever self-heal short of
   a restart.
3. **Prefer bypassing a fragile hop over hardening it**, when the consumer
   never needed what that hop uniquely provides. `loki-read-proxy` didn't
   need gateway's write-path routing at all.
