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
- Nodes: 2x Hetzner CPX31 (VPS, public IPs) + talos-pve-cp-0 (10.2.1.1) + wyrm2 (NixOS GPU worker, 2x RTX 5090)
- Domain: `*.allegedly.works` (PowerDNS in-cluster, DNS-01 challenges, dual LE issuers)
- HTTPS: Internet → VPS:443 → Cilium Envoy (Gateway API) → backend pods
  - Exception: Headscale uses TLSRoute passthrough (Envoy routes by SNI, Headscale terminates TLS)
- KubeSpan: WireGuard mesh between VPS and Proxmox (UDP 51820)
- Cilium MTU: `MTU: 1370` (uppercase key required — VXLAN 50 + WireGuard 80 = 130 overhead)
- KubePrism: `localhost:7445` as cluster endpoint (no VIP possible across VPS+home; kubeconfig patched post-bootstrap to real VPS IP)

## Services

| Service        | URL                                          | Purpose                       |
| -------------- | -------------------------------------------- | ----------------------------- |
| Authentik      | <https://auth.allegedly.works>               | SSO provider                  |
| Gitea          | <https://git.allegedly.works>                | Git hosting                   |
| Harbor         | <https://registry.allegedly.works>           | Container registry            |
| Vault          | <https://vault.allegedly.works>              | Secrets management            |
| Matrix/Element | <https://chat.allegedly.works>               | Chat                          |
| Grafana        | <https://grafana.allegedly.works>            | Monitoring                    |
| Nix Cache      | <https://cache.allegedly.works>              | Binary cache                  |
| Headscale      | <https://headscale.allegedly.works>          | Tailscale control             |
| Gatus          | <https://status.allegedly.works>             | Health monitoring             |
| OpenClaw       | <https://openclaw.allegedly.works>           | AI coding agent               |
| ActivityWatch  | `activitywatch.tailnet.allegedly.works:5600` | Activity tracking (mesh-only) |

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

## GPU (NVIDIA)

wyrm2 is a NixOS machine (not Talos) joined as a K8s worker via `k8s-worker.nix` and
KubeSpan. It provides 2x RTX 5090 GPUs to the cluster.

**Stack**: NixOS `hardware.nvidia-container-toolkit` generates CDI specs at
`/var/run/cdi/` → containerd configured with `nvidia-container-runtime.cdi` as a named
runtime → `RuntimeClass` resource maps `nvidia` handler to that runtime → NVIDIA device
plugin (Helm chart) discovers GPUs via NVML and advertises `nvidia.com/gpu` resources.

**How it works**: The device plugin uses the default `envvar` strategy — it sets
`NVIDIA_VISIBLE_DEVICES` on workload containers. Pods requesting GPUs must specify
`runtimeClassName: nvidia` so containerd routes them through `nvidia-container-runtime.cdi`,
which reads the env var and injects GPU devices/libraries via host CDI specs.

**Key files**:

- `nix/nixos/modules/k8s-worker.nix` — containerd nvidia runtime config, CDI settings
- `cluster/k8s/nvidia-device-plugin/helmrelease.yaml` — device plugin + RuntimeClass
- `cluster/k8s/ollama/deployment.yaml` — example GPU workload (`runtimeClassName: nvidia`)

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

## SSO (Authentik)

All applications use Authentik for SSO via native blueprints — idempotent YAML in
`k8s/authentik/sso-blueprints.yaml` (ConfigMap mounted into the worker, re-applied every
60 min). No Terraform state for Authentik resources.

- **Secret flow**: `terraform/gitops/sso-secrets/` generates OAuth2 client secrets →
  Vault → ESO `authentik-sso-client-secrets` in authentik namespace → worker `envFrom` →
  blueprint `!Env` tags
- **App-side secrets**: ESO in `k8s/authentik-blueprint/{app}-secret/` reads from
  the same Vault path
- **Remaining Terraform**: `harbor-oidc-config/` (Harbor API), `vault-oidc-auth/`
  (Vault OIDC auth backend) — configure non-Authentik systems

See <AGENTS.md> for the proxy-mode NetworkPolicy template when adding new SSO apps.

## ActivityWatch

Personal activity tracking via [aw-server-rust](https://github.com/ActivityWatch/aw-server-rust).
Cluster-internal only — accessible at `activitywatch.tailnet.allegedly.works:5600` via
Headscale mesh (MagicDNS). No built-in auth; Headscale membership is the trust boundary.

- **Server**: `aw-server-rust` on Proxmox, SQLite on `proxmox-csi-retain` (1Gi PVC)
- **Sidecar**: Tailscale container joins Headscale mesh (`TS_HOSTNAME=activitywatch`)
- **Image**: `registry.allegedly.works/activitywatch/aw-server`, CI at `.github/workflows/activitywatch-image.yml`
- **Pre-auth key**: Bootstrap Job (`k8s/activitywatch-authkey-bootstrap/`), not Terraform
  (upstream provider bug — [PR #28](https://github.com/awlsring/terraform-provider-headscale/pull/28))
- **Read-only proxy**: nginx sidecar on port 5601 (Service `activitywatch-readonly`),
  allows GET + POST `/api/0/query` only. `openclaw-sandbox` and `claude-sandbox` namespaces
  have CiliumNetworkPolicy access to this port.

### Desktop Client Setup

Watchers run locally, heartbeat to cluster via Headscale mesh. Config managed by
Nix home-manager (`nix/home/services/activitywatch.nix`).

1. Enroll device: `sudo tailscale up --login-server=https://headscale.allegedly.works`
2. Apply config: `home-manager switch --flake ~/code/ducktape#<hostname>`
3. Start: `aw-qt` (runs `aw-watcher-afk`, `aw-watcher-window`)
4. Verify: `curl http://activitywatch.tailnet.allegedly.works:5600/api/0/info`

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
