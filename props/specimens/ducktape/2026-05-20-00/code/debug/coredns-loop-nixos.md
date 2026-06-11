# CoreDNS loop detection crash on NixOS k8s workers

**Status**: Root cause found and fixed
**Date**: 2026-03-21
**Affected**: CoreDNS pods scheduled on NixOS k8s worker nodes (wyrm2, rugged)

## Symptom

CoreDNS enters CrashLoopBackOff within seconds of starting on a NixOS node:

```
[FATAL] plugin/loop: Loop (127.0.0.1:50799 -> :53) detected for zone "."
```

CoreDNS on Talos nodes is unaffected.

## Root cause

**kubelet was running with a stale config that lacked `resolvConf`.**

The `resolvConf = "/run/systemd/resolve/resolv.conf"` setting was added to
`k8s-worker.nix` in a NixOS switch, which updated
`/etc/kubernetes/kubelet-config.yaml` (a symlink to a nix store path). But
kubelet reads `--config` only once at startup. The kubelet service unit didn't
change (same `ExecStart`), so NixOS didn't restart it. kubelet kept running
with the boot-time config (profile 37) which had **no `resolvConf` field**.

Without `resolvConf`, kubelet defaults to reading `/etc/resolv.conf` — which
on NixOS is the systemd-resolved stub (`nameserver 127.0.0.53`). kubelet
passes this to containerd as `DnsConfig`. For `dnsPolicy: Default` pods (like
CoreDNS), the pod's `/etc/resolv.conf` gets `127.0.0.53`. CoreDNS forwards
upstream queries there, which loops back to itself.

### Proof chain

1. **Profile 37** (active at boot, Mar 20 22:22) kubelet config: no
   `resolvConf` field
   ```
   /nix/store/jwj2szra6w9v5z45w3j5qj712a3kd0ff-.../etc/kubernetes/kubelet-config.yaml
   ```
2. **Current profile** (46, after switches) kubelet config: has
   `resolvConf: /run/systemd/resolve/resolv.conf`
3. **kubelet**: started at boot (22:22:41 Mar 20), `NRestarts=0`, never
   restarted through 9 NixOS switches (profiles 37→46)
4. **Containerd sandbox metadata**: `dns_config.servers = ["127.0.0.53"]` —
   kubelet passed the stub, confirming it used the old config

## Fix

1. Added `restartTriggers = [ kubeletConfigYaml ]` to the kubelet systemd
   service in `k8s-worker.nix`. This makes `nixos-rebuild switch` restart
   kubelet whenever the config file content changes.

2. The existing `resolvConf` setting is correct — it just wasn't taking
   effect because kubelet wasn't restarted.

## Lessons

- NixOS only restarts services when the systemd unit itself changes
  (`ExecStart`, environment, etc.). Changes to config files read by the
  service require explicit `restartTriggers` or `restartIfChanged`.
- kubelet reads `--config` once at startup and never re-reads it. This is
  a known kubelet behavior, not a bug.
- The `resolvConf` kubelet setting DOES apply to `dnsPolicy: Default` pods
  (confirmed by reading kubelet and containerd source). Our initial hypothesis
  that it only affected `ClusterFirst` was wrong — the real issue was the
  stale config.
