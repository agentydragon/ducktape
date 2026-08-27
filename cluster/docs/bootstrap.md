# Talos Cluster Bootstrap Playbook

Step-by-step instructions for cold-starting the hybrid Talos cluster.
See <../README.md> for architecture overview, node topology, and networking details.

## Prerequisites

### Required Credentials

1. **Proxmox API Token** (`PROXMOX_VE_API_TOKEN` env var, `root@pam`)
2. **OVH API credentials** (`secrets/ovh-credentials.sops.yaml`)
3. **GitHub CLI** (`gh auth login`) for Flux GitOps bootstrap

### Required Access

- SSH to `root@atlas` (Proxmox host)
- `direnv` configured in cluster directory

### Persistent Auth Resources

Persistent Terraform resources (Proxmox API tokens and SOPS age key deployment)
live in `terraform/main/persistent-auth.tf` with `lifecycle { prevent_destroy = true }`.
Nebula node identities are durable inputs: public certificates and SOPS-encrypted
private keys in `secrets/nebula/`, read by tofu via the `sops` provider rather
than generated during an apply.
Talos machine secrets are ephemeral (fresh `cluster.id` per lifecycle).
See <bootstrap_dependencies.md> for the full dependency graph.

## Cold-Start Deployment

```bash
bazel run //cluster:bootstrap
```

The bootstrap script executes a multi-phase deployment against a single TF root
(`terraform/main/`, PG backend via CNPG `tofu-state-db-ovh`):

### Phase 0: Preflight Validation

- Git working tree clean (Flux requirement)
- Pre-commit validation (security, linting)
- OpenTofu configuration validation

### Phase 1: Persistent Auth (`tofu apply -target=<persistent-auth resources>`)

- Proxmox API tokens and SOPS age key deployment; reads persisted Nebula identities
- Resources have `lifecycle { prevent_destroy = true }` — preserved across cycles

### Phase 2: Infrastructure (`tofu apply -target=<infra resources>` + health checks)

- OVH API → Kimsufi bare-metal Talos nodes
- Proxmox API → NixOS worker capacity and any active Proxmox VMs
- Talos API → Bootstraps cluster, generates kubeconfig
- Kubernetes API → Installs Cilium CNI, deploys SOPS age key to flux-system

### Phase 3: Full Apply (`tofu apply`)

- Flux Bootstrap → applies committed Flux manifests; the root `ducktape` source
  reads public GitHub anonymously, then Flux decrypts GitHub App auth for
  private/write paths
- Core Services → cert-manager, Cilium Gateway API
- Storage → local-path/SeaweedFS/OpenEBS/Proxmox CSI
- Platform → ESO, Authentik

### Verification

```bash
kubectl get nodes -o wide              # All nodes Ready
flux get all                           # Flux status
kubectl get pods -A | grep -v Running  # Non-running pods
kubectl get storageclass               # should match the StorageClasses under k8s/ (no default class)
```

## Dependency Chain

See <bootstrap_dependencies.md> for the full L0–L7 bootstrap dependency graph
(external creds → SOPS → persistent auth → infrastructure → networking → Flux →
services → NixOS workers) plus per-layer recovery procedures.

## Let's Encrypt Issuer Toggle

Two always-present ClusterIssuers (`letsencrypt-prod`, `letsencrypt-staging`).
A single ConfigMap controls which is active:

```yaml
# k8s/cert-manager/issuer-config/configmap.yaml
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

- Cilium Gateway API with Envoy DaemonSet (hostNetwork on OVH nodes)
- OVH public IPs receive HTTPS traffic directly on ports 80/443
- Gateway terminates TLS using wildcard cert (`*.allegedly.works`)
- HTTPRoutes in each application namespace route to backend services

### Cluster Endpoint

Uses `localhost:7445` (Talos KubePrism on CP nodes, haproxy on NixOS workers) during
bootstrap to avoid circular dependency. Kubeconfig is patched post-bootstrap with
`api.allegedly.works` for external access.
