# Talos Kubernetes Cluster

Small Talos k8s cluster with GitOps and HTTPS.

- Deploy: `bazel run //cluster:bootstrap` (single command, automated layered deployment)
- VMs: Talos on Proxmox + Hetzner VPS, configured with OpenTofu
- Ingress: Cilium Gateway API (Envoy hostNetwork on VPS)
- CNI: Cilium VXLAN (infrastructure-managed, not GitOps)
- Secrets: SealedSecrets (stable keypair) + Vault/ESO (runtime)

## Prerequisites

- Proxmox host `atlas` with SSH access (`root@atlas`)
- Hetzner Cloud API token (`HCLOUD_TOKEN`)
- GitHub CLI (`gh auth login`) for Flux
- direnv configured in cluster directory

`.envrc` auto-exports `KUBECONFIG`/`TALOSCONFIG` and provides CLI tools (kubeseal, talosctl, etc.).

See <docs/bootstrap.md> for full setup.

## Infrastructure

- Network: 10.2.0.0/16 (VLAN 4 on Proxmox vmbr4)
  - 10.2.0.2: Atlas (Proxmox host) — **only reachable from Proxmox VLAN**
  - 10.2.1.x: Control plane (Proxmox), 10.2.2.x: Workers (Proxmox)
- Nodes: 2x Hetzner CPX31 (VPS, public IPs) + talos-pve-cp-0 (10.2.1.1) + talos-pve-gpu-worker-0 (10.2.2.1)
- Domain: `*.allegedly.works` (PowerDNS in-cluster, DNS-01 challenges, dual LE issuers)
- HTTPS: Internet → VPS:443 → Cilium Envoy (Gateway API) → backend pods
- KubeSpan: WireGuard mesh between VPS and Proxmox (UDP 51820)
- Cilium MTU: `MTU: 1370` (uppercase key required — VXLAN 50 + WireGuard 80 = 130 overhead)

## Services

| Service        | URL                                 | Purpose            |
| -------------- | ----------------------------------- | ------------------ |
| Authentik      | <https://auth.allegedly.works>      | SSO provider       |
| Gitea          | <https://git.allegedly.works>       | Git hosting        |
| Harbor         | <https://registry.allegedly.works>  | Container registry |
| Vault          | <https://vault.allegedly.works>     | Secrets management |
| Matrix/Element | <https://chat.allegedly.works>      | Chat               |
| Grafana        | <https://grafana.allegedly.works>   | Monitoring         |
| Nix Cache      | <https://cache.allegedly.works>     | Binary cache       |
| Headscale      | <https://headscale.allegedly.works> | Tailscale control  |
| Gatus          | <https://status.allegedly.works>    | Health monitoring  |
| OpenClaw       | <https://openclaw.allegedly.works>  | AI coding agent    |

Credentials: `get-passwords` (requires direnv in cluster directory).
OpenClaw requires a one-time gateway token entry in the UI — the token is included in
`get-passwords` output.

## Storage

| Provisioner          | Location | Default | Notes                                            |
| -------------------- | -------- | ------- | ------------------------------------------------ |
| `proxmox-csi-retain` | Proxmox  | Yes     | Storage-heavy: Harbor, Gitea, Loki, Nix          |
| `hcloud-volumes`     | Hetzner  | No      | (none active)                                    |
| `local-path`         | Any node | No      | CNPG: Authentik, PowerDNS; Vault Raft, Headscale |

Proxmox CSI pinned to Proxmox nodes (`topology.kubernetes.io/region: proxmox`) — needs VLAN access to API.

## Failure Modes

| Scenario        | Cluster    | Ingress | DNS   | Authentik | Notes                                         |
| --------------- | ---------- | ------- | ----- | --------- | --------------------------------------------- |
| Single VPS down | 2/3 quorum | Works   | Works | Works     | 1 server+worker replica on surviving VPS      |
| Both VPS down   | 1/3 only   | Down    | Down  | Down      | Home pods continue but cluster frozen         |
| Home down       | 2/3 quorum | Works   | Works | Works     | All VPS-critical services on `hcloud-volumes` |

### VPS-Only Resilience Invariants

The following services **MUST** work/recover with VPS only (without Proxmox):

- **DNS** (PowerDNS) — all external name resolution depends on this
- **Website** (`allegedly.works`) — public-facing

These services must not depend on `proxmox-csi-retain` storage or Proxmox-pinned nodes.
Both PowerDNS and Authentik now use CloudNativePG on `hcloud-volumes`.
See <docs/plan.md> for the full invariant definition, compliance tracking, and fix plan.

## Repository Structure

```text
cluster/
├── shell.nix, .envrc      # direnv (KUBECONFIG, TALOSCONFIG, CLI tools)
├── docs/                   # bootstrap, plan, troubleshooting, operations, secrets
├── terraform/
│   ├── bootstrap/
│   │   ├── persistent-auth/   # Keypairs, tokens (survives cluster rebuild)
│   │   ├── infrastructure/    # VMs, Talos, Cilium CNI
│   │   └── flux/              # Flux bootstrap, core services, applications
│   └── gitops/                # tofu-controller managed (DNS, SSO, secrets)
├── k8s/                       # Flux-managed manifests (apps, services, config)
└── flux-system/               # Flux controllers (auto-generated)
```

## Let's Encrypt Rate Limits

5 duplicate certs/week per domain (rolling 7-day window). Each destroy→bootstrap cycle
requests fresh certificates. Controlled by `k8s/cert-manager-issuer-config/configmap.yaml`.

**Note:** The legacy VPS at `agentydragon.com` is separate infrastructure not involved in
this cluster (see <docs/plan.md>).
