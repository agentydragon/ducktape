# wyrm2 — Chrome `ERR_NETWORK_CHANGED` from Pod Flapping

## Symptom

Chrome on wyrm2 frequently shows `ERR_NETWORK_CHANGED`, disrupting browsing. Caused by
k8s pods flip-flopping on the cluster, which triggers network topology changes visible to
Chrome.

## Root Cause

Chrome's `NetworkChangeNotifier` on Linux listens to **rtnetlink** (`RTMGRP_LINK`,
`RTMGRP_IPV4_IFADDR`, `RTMGRP_IPV6_IFADDR`) for `RTM_NEWLINK`, `RTM_DELLINK`,
`RTM_NEWADDR`, `RTM_DELADDR` on **all** interfaces in the network namespace. Every pod
veth creation/deletion fires these events. Chrome does not filter by interface name — it
treats every event as a potential network change, drains the connection pool, and errors
pending requests with `ERR_NETWORK_CHANGED`.

wyrm2 is a NixOS k8s worker node; Chrome runs on the same host as the kubelet. All pod
networking events in the host network namespace are visible to Chrome.

## Pod churn sources (2026-03-25 investigation)

Two issues compounded to create ~1326 rtnetlink events/hr:

1. **Operator restart cascades**: etcd instability (from pve-cp-0 kernel stalls — see
   <debug/kernel-6.18-amd-kvm-stall.md>) caused API timeouts → operator leader election
   losses → restart loops across 7+ operators.

2. **Tofu-controller tf-runners**: 7 of 24 Terraform resources stuck on stale Kubernetes
   Lease locks from killed runners. Each retried every 15 seconds, spawning ~663 pods/hr.
   Fixed by deleting the 7 `lock-tfstate-default-*` Leases in `flux-system`.

After clearing locks and stabilizing etcd: pod churn dropped from ~663/hr to ~72/hr
(normal tf-runner reconciliation at `interval: 15m`).

## Mitigations

### Chrome-side: Network namespace isolation (recommended)

There are **no Chrome flags** to disable or tune the `NetworkChangeNotifier`. It is not
exposed via `chrome://flags` or command-line switches.

Run Chrome in a separate network namespace that only sees host interfaces, not pod veths:

```bash
firejail --net=eth0 google-chrome
```

### Why not isolate containerd instead?

Containerd doesn't create veths — the CNI plugin (Cilium) does, in the host namespace.
Moving containerd wouldn't help; moving Cilium would break all pod routing.

### Cluster-level

1. Fix pve-cp-0 stalls — see <debug/kernel-6.18-amd-kvm-stall.md>
2. Add `NoSchedule` taints to VPS control plane nodes (prevent OOM cascade)
3. Clean up stale VolumeAttachments for `talos-pve-gpu-worker-0`

## Cluster Outage — 2026-03-30

While debugging the kernel 6.18 stalls, removing pve-cp-0 left 2-member etcd. VPS nodes
(no `NoSchedule` taint) absorbed workload pods → OOM → nebula tunnel broke → etcd no
leader → full cluster outage. Recovered by rebooting + cordoning VPS nodes.

**Prevention**: VPS control plane nodes need `NoSchedule` taints.

## TODOs

- [ ] Consider `firejail --net=<iface>` for Chrome permanently
- [ ] Add `NoSchedule` taints to VPS nodes
- [ ] Clean up stale VolumeAttachments for `talos-pve-gpu-worker-0`
- [ ] Bring `rugged` back online

## Related

- <debug/kernel-6.18-amd-kvm-stall.md> — the kernel bug causing the pod churn
- <debug/pve-cp0-notready-2026-03-23/README.md> — original NMI incident investigation
- <debug/atlas/ethernet_recurring/README.md> — atlas physical link flaps (different issue)
- <debug/atlas/wyrm2-freezes.md> — wyrm2 UI freezes (QXL TTM, resolved)
