# Talos Cluster Operations

Operational procedures for day-to-day cluster management, scaling, maintenance, and troubleshooting.

## SOPS Secrets

```bash
# Edit an existing SOPS secret
sops cluster/k8s/<app>/secrets/my-secret.sops.yaml

# Create a new SOPS-encrypted secret (uses .sops.yaml creation rules)
sops cluster/k8s/<app>/secrets/new-secret.sops.yaml
```

## Node Operations

### Adding New Nodes

### Controller Node

```bash
cd /home/agentydragon/code/ducktape/cluster/terraform/main

# Add new node to the relevant OpenTofu topology, usually `ovh-nodes.tf`
# Apply changes
tofu apply

# New nodes will automatically join the cluster
# Verify with talosctl get members
```

### Node Maintenance

### Restart Single Node

```bash
# Gracefully restart a node via Talos (use the node's Nebula IP or hostname)
talosctl --endpoints <node> --nodes <node> reboot

# Force restart out-of-band: OVH nodes via the OVH manager/IPMI; Proxmox-hosted
# Talos VMs via `ssh root@atlas 'qm reboot <vmid>'`
```

### Remove Node

Remove the node from the relevant OpenTofu topology, then `tofu apply`.
Kubernetes node object will be cleaned up automatically.

## System Diagnostics

### VM Console Management

### Take VM Screenshots

See `~/.claude/skills/proxmox_vm/vm-screenshot.sh`

### Direct VM Console Access

```bash
# Interactive console for a Proxmox-hosted VM (from the Proxmox host)
ssh root@atlas
qm terminal <vmid>
```

## Switching Let's Encrypt Environment (Staging ↔ Production)

See <bootstrap.md> for the issuer toggle mechanism. To switch:

1. Edit `LETSENCRYPT_ISSUER` in `k8s/cert-manager/issuer-config/configmap.yaml`
2. Commit and push
3. Flux re-renders all Ingresses and cert-manager re-issues certificates automatically

**Rate limit warning:** Each switch re-issues all certificates. Avoid rapid toggling
(5 duplicate certs/domain/week on production LE).

## Troubleshooting

See <troubleshooting.md> for diagnostic commands and known issues.

## Security Configuration

### Privileged Ports (Port < 1024)

Services binding to privileged ports (e.g., port 53) as non-root need
`NET_BIND_SERVICE` capability with `drop: ["ALL"]` for PSS "restricted" compliance.
