# wyrm2 — Chrome `ERR_NETWORK_CHANGED` from Pod Flapping

## Symptom

Chrome on wyrm2 frequently shows `ERR_NETWORK_CHANGED`, disrupting browsing. Caused by
k8s pods flip-flopping on the cluster, which triggers network topology changes visible to
Chrome.

## Root Cause

Chrome's `NetworkChangeNotifier` on Linux listens to **rtnetlink** (`RTMGRP_LINK`,
`RTMGRP_IPV4_IFADDR`, `RTMGRP_IPV6_IFADDR`) on **all** interfaces in the network
namespace, with no interface filter. Any `RTM_NEWADDR`/`RTM_DELADDR` counts as an IP
address change: Chrome flushes every socket pool and errors pending requests with
`ERR_NETWORK_CHANGED`. Link add/remove alone matters only when it changes the connection
type, which pod veths do not. The trigger is the kernel `fe80::` link-local each pod's
host-side `lxc*` veth gets about 2 s after link-up, and its removal at teardown; the
veths carry no IPv4 (pause-pod test, 2026-09-25,
[#7921](https://github.com/agentydragon/ducktape/issues/7921)).

wyrm2 is a NixOS k8s worker node; Chrome runs on the same host as the kubelet. All pod
networking events in the host network namespace are visible to Chrome.

## Pod churn sources (2026-03-25 investigation)

Two issues compounded to create ~1326 rtnetlink events/hr:

1. **Operator restart cascades**: etcd instability (from pve-cp-0 kernel stalls — see
   <../../../debug/kernel_6_18_amd_kvm_stall.md>) caused API timeouts → operator leader election
   losses → restart loops across 7+ operators.

2. **Tofu-controller tf-runners**: 7 of 24 Terraform resources stuck on stale Kubernetes
   Lease locks from killed runners. Each retried every 15 seconds, spawning ~663 pods/hr.
   Fixed by deleting the 7 `lock-tfstate-default-*` Leases in `flux-system`.

After clearing locks and stabilizing etcd: pod churn dropped from ~663/hr to ~72/hr
(normal tf-runner reconciliation at `interval: 15m`).

## Mitigations

### Host-side: no kernel link-local on new interfaces

There are **no Chrome flags** to disable or tune the `NetworkChangeNotifier`. It is not
exposed via `chrome://flags` or command-line switches.

All NixOS k8s workers set `net.ipv6.conf.default.addr_gen_mode = 1` in
<../../nixos/modules/k8s-worker.nix>, so pod veths get no link-local;
NetworkManager-managed NICs set their own mode. On wyrm2 since 2026-09-25: the pause-pod
test from [#7921](https://github.com/agentydragon/ducktape/issues/7921) shows no `inet6`
events, and the Chrome symptoms have not been seen since.

### Why not isolate containerd instead?

Containerd doesn't create veths — the CNI plugin (Cilium) does, in the host namespace.
Moving containerd wouldn't help; moving Cilium would break all pod routing.

## Status: watching

Watch over time whether the churn-driven resets stay gone. If `ERR_NETWORK_CHANGED`
comes back, capture what fired it before changing anything: `ip -ts monitor address`
on the host during a burst (which interface still gains or loses an address), and a
`chrome://net-export` log around it. Delete this note if it is still quiet on 2026-10-25.

## Related

- <../../../debug/kernel_6_18_amd_kvm_stall.md> — the kernel bug causing the pod churn
- <../../../cluster/debug/pve-cp0-notready-2026-03-23/README.md> — original NMI incident investigation
- <../../../debug/atlas/ethernet_recurring/README.md> — atlas physical link flaps (different issue)
- <../wyrm2/wyrm2_freezes.md> — wyrm2 UI freezes (QXL TTM, resolved)
