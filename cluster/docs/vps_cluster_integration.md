# VPS Cluster Integration

**Status**: Phase 1 complete, services pending

## Current Architecture

```text
Internet → VPS (2x Hetzner CPX31, Hillsboro OR)
              ├── 2 Talos controllers (etcd majority)
              ├── Hetzner Cloud CSI for storage
              └── KubeSpan mesh (WireGuard) → Home (Proxmox, pending)
                                                   └── Workers with ZFS storage
```

**Implemented**:

- 2x Hetzner CPX31 VPS nodes (`talos-vps-0`, `talos-vps-1`)
- Both nodes are control-plane with Talos v1.12.3, Kubernetes v1.32.0
- Cilium CNI with VXLAN tunnel mode (cross-node connectivity verified)
- KubeSpan mesh working - WireGuard handshakes verified
- Hetzner Cloud CSI for block storage
- Talos machine secrets persisted in 00-persistent-auth layer

**Pending**:

- Home Proxmox node(s) not yet added
- Services not deployed (Flux, Vault, Authentik, etc.)

## Networking: KubeSpan

Talos native WireGuard mesh connecting VPS ↔ home nodes.

**Why KubeSpan**: Native to Talos, no external dependencies, automatic mesh with integrated discovery.

**Configuration** (in Talos machine config):

```yaml
machine:
  network:
    kubespan:
      enabled: true
cluster:
  discovery:
    enabled: true
```

**Requirements**: UDP 51820 open on all nodes.

**Debugging**: See `docs/troubleshooting.md` → KubeSpan section.

## Storage Strategy

| Location | Provisioner       | Services                              |
| -------- | ----------------- | ------------------------------------- |
| VPS      | Hetzner Cloud CSI | Vault, Authentik, DNS, cert-manager   |
| Home     | Proxmox CSI (ZFS) | Harbor, Gitea, Loki, media, Nix cache |

**Rationale**: VPS for always-on critical path, home for storage-heavy workloads.

## Failure Modes

| Scenario        | Cluster       | Ingress | Notes                             |
| --------------- | ------------- | ------- | --------------------------------- |
| Single VPS down | ✅ 2/3 quorum | ✅      | Pod anti-affinity recommended     |
| Both VPS down   | ❌ 1/3 only   | ❌      | Home pods continue but unmanaged  |
| Home down       | ✅ 2/3 quorum | ✅      | SSO/internal services unavailable |

## Remaining Work

### Infrastructure

- [ ] Add home Proxmox node(s) as workers
- [ ] Deploy Proxmox CSI for home storage
- [ ] Verify KubeSpan mesh VPS ↔ home

### Services

- [ ] Flux CD with Sealed Secrets
- [ ] Vault with Raft HA
- [ ] Authentik (identity provider)
- [ ] Ingress (nginx or Gateway API)
- [ ] PowerDNS, cert-manager
- [ ] Harbor, Gitea, observability stack

### Backup (not yet configured)

- [ ] rclone with Google Drive for terraform state
- [ ] Encrypted backup script
- [ ] Document restore procedure

## Decisions Made

1. **2x Hetzner CPX31** - 4 vCPU, 8GB RAM, 160GB NVMe, ~€30/month total
2. **Controller placement: 2 VPS + 1 home** - Survives home outage
3. **KubeSpan over Tailscale** - Native to Talos, simpler
4. **Cilium VXLAN** - Required for cross-VPS networking (not same L2)
