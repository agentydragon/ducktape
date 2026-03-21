# Talos Cluster Bootstrap Playbook

Step-by-step instructions for cold-starting the hybrid Talos cluster.
See <../README.md> for architecture overview, node topology, and networking details.

## Prerequisites

### Required Credentials

1. **Hetzner Cloud API Token** (`HCLOUD_TOKEN` env var)
2. **Proxmox API Token** (managed in persistent-auth layer, user `terraform@pve`)
3. **GitHub CLI** (`gh auth login`) for Flux GitOps bootstrap

### Required Access

- SSH to `root@atlas` (Proxmox host)
- `direnv` configured in cluster directory

### Persistent Auth Layer

Run once per environment (survives cluster destroy/recreate):

```bash
cd terraform/bootstrap/persistent-auth
tofu init && tofu apply
```

Creates: Proxmox API tokens, sealed secrets keypair, Nix signing key, Flux deploy key.
Talos machine secrets are in the infrastructure layer (fresh per lifecycle).

## Cold-Start Deployment

```bash
export HCLOUD_TOKEN="your-hetzner-api-token"
bazel run //cluster:bootstrap
```

The bootstrap script executes a 3-phase layered deployment:

### Phase 0: Preflight Validation

- Git working tree clean (Flux requirement)
- Pre-commit validation (security, linting)
- OpenTofu configuration validation

### Phase 1: Infrastructure (`terraform/bootstrap/infrastructure`)

- Hetzner API → 2x VPS with Talos ISO
- Proxmox API → 1x VM with cloud-init for static IP
- Talos API → Bootstraps cluster, generates kubeconfig
- Kubernetes API → Installs Cilium CNI, deploys sealed secrets keypair

### Phase 2: Flux (`terraform/bootstrap/flux`)

- Flux Bootstrap → GitOps engine with GitHub
- Core Services → cert-manager, Cilium Gateway API
- Storage → Hetzner CSI (VPS), Proxmox CSI (home)
- Platform → Vault, ESO, Authentik

### Verification

```bash
kubectl get nodes -o wide              # All nodes Ready
flux get all                           # Flux status
kubectl get pods -A | grep -v Running  # Non-running pods
kubectl get storageclass               # hcloud-volumes + proxmox-csi
```

## Dependency Chain

```text
Talos OS → Nebula mesh → K8s API → Cilium CNI → Sealed Secrets → CSI Drivers → Apps
```

## Let's Encrypt Issuer Toggle

Two always-present ClusterIssuers (`letsencrypt-prod`, `letsencrypt-staging`).
A single ConfigMap controls which is active:

```yaml
# k8s/cert-manager-issuer-config/configmap.yaml
data:
  LETSENCRYPT_ISSUER: letsencrypt-prod # or letsencrypt-staging
```

Every Ingress has `cert-manager.io/cluster-issuer: "${LETSENCRYPT_ISSUER}"` annotation
substituted by Flux. Flipping the toggle re-issues all certificates. Trust bundle follows
via `${LETSENCRYPT_ISSUER}-root-ca` naming convention.

## External Connectivity

### DNS Delegation

1. Route 53 delegates `allegedly.works` → VPS PowerDNS
2. PowerDNS runs on VPS nodes (public IPs)
3. cert-manager uses DNS-01 challenges

### Ingress (Gateway API)

- Cilium Gateway API with Envoy DaemonSet (hostNetwork on VPS nodes)
- VPS public IPs receive HTTPS traffic directly on ports 80/443
- Gateway terminates TLS using wildcard cert (`*.allegedly.works`)
- HTTPRoutes in each application namespace route to backend services

### Cluster Endpoint

Uses `localhost:7445` (Talos KubePrism on CP nodes, haproxy on NixOS workers) during
bootstrap to avoid circular dependency. Kubeconfig is patched post-bootstrap with
real VPS IP for external access.

## Troubleshooting

See <troubleshooting.md>.
