# talosctl upgrade: Hostname Loss and Accidental VPS Replacement

**Date**: 2026-03-07
**Status**: Resolved

## Summary

Upgrading Talos nodes via `talosctl upgrade` caused hostname loss and random re-registration.
A subsequent `tofu apply` accidentally replaced both VPS servers, destroying the cluster.

## Root Cause 1: Hostname Loss

**VPS nodes (Hetzner)**: The Image Factory installer doesn't encode platform in the UKI
by default, so nodes reported `talos.platform=metal` instead of `hcloud`. Without the
correct platform, Talos couldn't fetch Hetzner metadata (hostname, network config).

**Proxmox nodes (nocloud)**: After kexec upgrade, the cidata disk (one-time boot medium)
is not re-attached. Platform metadata is lost. Cached config on STATE partition didn't
reliably provide hostname.

**Both cases**: Without platform metadata or explicit `machine.network.hostname`, Talos
falls through to auto-generated hostname (`auto: stable` → hash-based name like
`talos-6we-boc`).

### Fixes

1. **Hetzner**: Add `extraKernelArgs=["talos.platform=hcloud"]` to Image Factory schematic
   (commit b697e535) + `machine.install.image` for future upgrades
2. **Both platforms**: Explicit `HostnameConfig` document with `auto: off` and explicit hostname
3. **Proxmox**: Explicit `machine.network.interfaces` with `dhcp: false` and static addressing
   (cidata unavailable after kexec → node would also lose static IP and fall back to DHCP)

## Root Cause 2: Accidental VPS Replacement

`hcloud_server` had `ignore_changes = [user_data]` but not `image`. Changing the schematic
changed the Packer snapshot → new `image` ID → Terraform planned destroy+recreate. A
`tofu apply -target=...machine_configuration_apply... -auto-approve` pulled in the server
resources via dependency resolution and replaced both VPS servers (2/3 etcd members lost).

**Fix**: `lifecycle { ignore_changes = [user_data, image] }` on `hcloud_server`.

## Root Cause 3: podCIDR Reassignment (2026-03-19 follow-up)

When a node registers with a transient hostname, kube-controller-manager assigns a fresh
podCIDR. When the node re-registers with the correct hostname, it gets another podCIDR.
After Cilium restart, only the new CIDR is routed. DaemonSet pods surviving the transition
keep old-CIDR IPs and lose connectivity. Observed cascade: longhorn-manager down → Vault
stuck → ESO down → 84 kustomizations blocked.

**Fix**: Delete pods with old-CIDR IPs (`kubectl delete pod`). Clean up stale Longhorn nodes.

**Unsolved**: Full server replacement via `tofu apply` can still cause transient hostname →
CIDR gap. Consider Cilium `cluster-pool` IPAM or pre-upgrade node deletion.

## Prevention

1. Always set explicit hostnames in machine config (`HostnameConfig` with `auto: off`)
2. Always set explicit network interfaces for nocloud platforms (`dhcp: false`)
3. For Hetzner: ensure `talos.platform=hcloud` in schematic `extraKernelArgs`
4. Never `tofu apply -auto-approve` with `-target` — review full plan first
5. Add `image` to `ignore_changes` for `hcloud_server`
6. After any node hostname change: check for pods with IPs outside current `spec.podCIDR`

## Timeline

1. Added `iscsi-tools` to schematics, ran `talosctl upgrade` → all nodes got random hostnames
2. Attempted fix via `tofu apply -target=...machine_configuration_apply... -auto-approve`
3. Terraform resolved dependencies, replaced both VPS servers → etcd quorum lost
4. Full cluster teardown and rebuild required
