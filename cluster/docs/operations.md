# Talos Cluster Operations

Operational procedures for day-to-day cluster management, scaling, maintenance, and troubleshooting.

## SOPS Secrets

See [Secrets Strategy](secrets.md) for editing and creation, local age-key decryption scope, and Flux's SOPS decryption requirements and failure modes.

## Node Operations

### Add or remove a host

Follow the topology-aware [host onboarding](mesh_membership.md#add-a-host) or
[host removal](mesh_membership.md#remove-a-host) procedure. These runbooks cover
provisioning, cluster membership, generated manifests, and removal checks.

### Node Maintenance

### Restart Single Node

```bash
# Gracefully restart a node via Talos (use the node's Nebula IP or hostname)
talosctl --endpoints <node> --nodes <node> reboot

# Force restart out-of-band: OVH nodes via the OVH manager/IPMI; Proxmox-hosted
# Talos VMs via `ssh root@atlas 'qm reboot <vmid>'`
```

## System Diagnostics

### VM Console Management

### Take VM Screenshots

Use `vm-interact <vmid> --screenshot`; see <../skills/proxmox_vm/SKILL.md>.

### Direct VM Console Access

```bash
# Interactive console for a Proxmox-hosted VM (from the Proxmox host)
ssh root@atlas
qm terminal <vmid>
```

## Switching Let's Encrypt Environment (Staging ↔ Production)

Use the [issuer toggle procedure](bootstrap.md#lets-encrypt-issuer-toggle), then
commit and push the regenerated manifests for Flux to apply. Each switch
re-issues all certificates, so avoid rapid toggling; see the
[Let's Encrypt rate limits](../README.md#lets-encrypt-rate-limits).

## Troubleshooting

See <troubleshooting.md> for diagnostic commands and known issues.

## Security Configuration

### Privileged Ports (Port < 1024)

Services binding to privileged ports (e.g., port 53) as non-root need
`NET_BIND_SERVICE` capability with `drop: ["ALL"]` for PSS "restricted" compliance.
