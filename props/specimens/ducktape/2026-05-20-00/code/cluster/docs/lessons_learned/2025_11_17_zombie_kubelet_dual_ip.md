# Zombie Kubelet from Worker Dual-IP Assignment

**Date**: 2025-11-17 (worker0), 2025-12-28 (worker1, root cause identified)
**Status**: Resolved

## Root Cause

Worker nodes were missing explicit network interface configuration. Controllers had
`machine.network.interfaces` (required for VIP), which implicitly disabled DHCP. Workers
had `network: {}` (empty), so **DHCP remained enabled by default**.

The network DHCP server assigned a second IP to workers. Talos NodeIPController saw both
IPs and couldn't decide which was the kubelet IP. Every veth creation (container start)
triggered re-evaluation. Under sustained container churn (e.g., tofu-controller runner pods
cycling), this destabilized kubelet and crashed containerd.

When containerd crashed while kubelet was running:

1. Kubelet process and containerd-shim survived as orphaned processes
2. Containerd restarted but lost tracking of the old kubelet container
3. Talos service manager couldn't delete the orphaned container: "cannot delete running task
   kubelet: failed precondition"
4. New kubelet couldn't start — node appeared Ready (old kubelet PID) but no new pods scheduled

**Fix**: Added explicit `dhcp: false` to worker machine config. Commit 2bf6ae9.

```yaml
machine:
  network:
    interfaces:
      - interface: eth0
        dhcp: false
```

## Key Symptoms

- Node shows Ready in `kubectl get nodes` but pods stuck in Pending
- `talosctl service kubelet status`: `STATE: Failed`, `HEALTH: Fail`
- Error: "cannot delete running task kubelet: failed precondition"
- Two IPs on eth0: `talosctl get addresses | grep "eth0.*10\."`
- Constant "node IP skipped" messages in dmesg

## Diagnosis

```bash
# Check for dual IPs (the root cause)
talosctl -n <worker-ip> get addresses | grep "eth0.*10\."
# Bad: two IPs on eth0

# Check for zombie kubelet (the symptom)
talosctl -n <worker-ip> service kubelet status
# Bad: STATE: Failed

# Check NodeIPController churn
talosctl -n <worker-ip> dmesg | grep "node IP skipped"
```

## Recovery

Reboot the affected node:

```bash
talosctl -n <node-ip> reboot
```

## Key Lessons

1. **Talos enables DHCP by default** — controllers avoid this implicitly (VIP config
   requires `machine.network.interfaces`), but workers need explicit `dhcp: false`.
2. **Dual IPs cause kubelet instability** — NodeIPController re-evaluates on every veth
   creation. High container churn amplifies the problem.
3. **Containerd crash + orphaned kubelet = Talos deadlock** — the service manager can't
   delete a running container whose parent (containerd) crashed. Only a reboot recovers.
4. **Implicit vs explicit config** — when two node roles have different defaults, make
   the config explicit for both. Don't rely on side effects of other config options.
