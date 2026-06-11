# Talos Cluster Bootstrap Playbook

Step-by-step instructions for cold-starting the hybrid Talos cluster.
See <../README.md> for architecture overview, node topology, and networking details.

## Prerequisites

### Required Credentials

1. **Hetzner Cloud API Token** (`HCLOUD_TOKEN` env var)
2. **Proxmox API Token** (`PROXMOX_VE_API_TOKEN` env var, `root@pam`)
3. **GitHub CLI** (`gh auth login`) for Flux GitOps bootstrap

### Required Access

- SSH to `root@atlas` (Proxmox host)
- `direnv` configured in cluster directory

### Persistent Auth Resources

Persistent-auth resources (Proxmox API tokens, Nebula node certs, SOPS age key
deployment) live in `terraform/main/persistent-auth.tf` with `lifecycle { prevent_destroy = true }`.
Core secrets (Nebula CA, Flux deploy key, cluster age keypair) are SOPS-encrypted
in `secrets/` and read by tofu via the `sops` provider.
Talos machine secrets are ephemeral (fresh `cluster.id` per lifecycle).
See <bootstrap_dependencies.md> for the full dependency graph.

## Cold-Start Deployment

```bash
export HCLOUD_TOKEN="your-hetzner-api-token"
bazel run //cluster:bootstrap
```

The bootstrap script executes a multi-phase deployment against a single TF root
(`terraform/main/`, PG backend via CNPG `tofu-state-db`):

### Phase 0: Preflight Validation

- Git working tree clean (Flux requirement)
- Pre-commit validation (security, linting)
- OpenTofu configuration validation

### Phase 1: Persistent Auth (`tofu apply -target=<persistent-auth resources>`)

- Proxmox API tokens, Nebula CA → node certs, SOPS age key deployment
- Resources have `lifecycle { prevent_destroy = true }` — preserved across cycles

### Phase 2: Infrastructure (`tofu apply -target=<infra resources>` + health checks)

- Hetzner API → 2x VPS with Talos ISO
- Proxmox API → 1x VM with cloud-init for static IP
- Talos API → Bootstraps cluster, generates kubeconfig
- Kubernetes API → Installs Cilium CNI, deploys SOPS age key to flux-system

### Phase 3: Full Apply (`tofu apply`)

- Flux Bootstrap → GitOps engine with GitHub
- Core Services → cert-manager, Cilium Gateway API
- Storage → Hetzner CSI (VPS), Proxmox CSI (home)
- Platform → ESO, Authentik

### Verification

```bash
kubectl get nodes -o wide              # All nodes Ready
flux get all                           # Flux status
kubectl get pods -A | grep -v Running  # Non-running pods
kubectl get storageclass               # longhorn (default), proxmox-csi-retain, etc.
```

## Dependency Chain

```text
Talos OS → Nebula mesh → K8s API → Cilium CNI → Flux (SOPS) → CSI Drivers → Apps
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

1. Route 53 is the authoritative DNS for `allegedly.works` (zone + records managed by Terraform)
2. cert-manager uses Route 53 DNS-01 solver for ACME challenges

### Ingress (Gateway API)

- Cilium Gateway API with Envoy DaemonSet (hostNetwork on VPS nodes)
- VPS public IPs receive HTTPS traffic directly on ports 80/443
- Gateway terminates TLS using wildcard cert (`*.allegedly.works`)
- HTTPRoutes in each application namespace route to backend services

### Cluster Endpoint

Uses `localhost:7445` (Talos KubePrism on CP nodes, haproxy on NixOS workers) during
bootstrap to avoid circular dependency. Kubeconfig is patched post-bootstrap with
real VPS IP for external access.
