# Talos Kubernetes Cluster

Small Talos k8s cluster with GitOps and HTTPS.

- Deploy: `bazel run //cluster:bootstrap` (single command, automated layered deployment)
- Machines: Talos on OVH Kimsufi bare metal plus NixOS/Proxmox workers, configured with OpenTofu
- Ingress: Cilium Gateway API (Envoy hostNetwork on OVH)
- CNI: Cilium VXLAN (infrastructure-managed, not GitOps)
- Secrets: SOPS (age-encrypted in git, decrypted by Flux). ESO with the Kubernetes
  provider mirrors a few secrets cross-namespace. Vault was decommissioned 2026-04-19
  (see <vault-migration/TODO.md>).

## Prerequisites

- Proxmox host `atlas` with SSH access (`root@atlas`)
- OVH API credentials (`secrets/ovh-credentials.sops.yaml`)
- GitHub CLI (`gh auth login`) for Flux
- direnv configured in cluster directory

`.envrc` auto-exports `KUBECONFIG`/`TALOSCONFIG` and provides CLI tools (talosctl, kubectl, etc.).

See <docs/bootstrap.md> for full setup.

## Infrastructure

- Network: 10.2.0.0/16 (VLAN 4 on Proxmox vmbr4)
  - 10.2.0.2: Atlas (Proxmox host) — **only reachable from Proxmox VLAN**
  - 10.2.1.x: Control plane (Proxmox), 10.2.2.x: Workers (Proxmox)
- Nodes: see [Node Types](#node-types) below
- Domain: `*.allegedly.works` (AWS Route 53, DNS-01 challenges, dual LE issuers)
- HTTPS: Internet → OVH bare metal:443 → Cilium Envoy (Gateway API) → backend pods
- Nebula: encrypted mesh overlay (UDP 4242, lighthouses + relays on OVH nodes)
- MTU stack: pod 1370 → Cilium VXLAN → `nebula1` 1420 → `eno1` 1500. See
  <docs/network.md> for the layering, encapsulation, and MTU model.
- Kubeconfig patched post-bootstrap to `api.allegedly.works`

### Node Types

| Node                           | Type             | Region    | Availability     | Hardware            |
| ------------------------------ | ---------------- | --------- | ---------------- | ------------------- |
| `ovh-ns102453`                 | Talos CP         | `hil`     | Always on        | OVH Kimsufi KS-5    |
| `ovh-ns103656`, `ovh-ns103711` | Talos CP         | `hil`     | Always on        | OVH Kimsufi KS-5    |
| `ovh-ns104952`, `ovh-ns104963` | Talos worker     | `hil`     | Always on        | OVH KS-GAME         |
| `wyrm2`                        | NixOS GPU worker | `proxmox` | Always on (home) | 2x RTX 5090         |
| `iguana`                       | NixOS laptop     | `roaming` | Often offline    | ThinkPad X1 Extreme |
| `rugged`                       | NixOS laptop     | `roaming` | Often offline    | Dell Rugged 12      |

Region labels are `topology.kubernetes.io/region`. Roaming nodes are laptops that
join/leave the cluster frequently. `rugged` has taint
`node-role.kubernetes.io/roaming=true:NoSchedule`. Do not schedule workloads that
require persistent availability on roaming nodes.

Mesh roster (every Nebula peer, including non-k8s hosts like `atlas`, `pixel6`)
lives in `nebula-mesh.json` at the repo root. To add or remove a node, see
<docs/mesh_membership.md>.

## Services

Key services (curated — this table is not the SSOT; the full set is the
HTTPRoutes under `k8s/` and `k8s/authentik/proxy-routes/`):

| Service        | URL                                | Purpose                       |
| -------------- | ---------------------------------- | ----------------------------- |
| Authentik      | <https://auth.allegedly.works>     | SSO provider                  |
| Forgejo        | <https://git.allegedly.works>      | Git hosting                   |
| Harbor         | <https://registry.allegedly.works> | Container registry            |
| Matrix/Element | <https://chat.allegedly.works>     | Chat                          |
| Grafana        | <https://grafana.allegedly.works>  | Monitoring                    |
| Nix Cache      | <https://cache.allegedly.works>    | Binary cache                  |
| Gatus          | <https://status.allegedly.works>   | Health monitoring             |
| OpenClaw       | <https://openclaw.allegedly.works> | AI coding agent               |
| Ollama         | <https://ollama.allegedly.works>   | LLM inference (GPU)           |
| Airlock        | <https://airlock.allegedly.works>  | Agent infrastructure          |
| ActivityWatch  | `activitywatch:5600`               | Activity tracking (suspended) |

Credentials: `get-passwords` (requires direnv in cluster directory).
OpenClaw requires a one-time gateway token entry in the UI — the token is included in
`get-passwords` output.

## Storage

All storage is region-local — no cross-site synchronous replication. Key
classes below (curated — SSOT is the `StorageClass` manifests under `k8s/`,
e.g. `k8s/{local-path-provisioner,openebs-lvm}/`, plus CSI Helm values):

| StorageClass         | Provisioner            | Region    | Notes                                                                              |
| -------------------- | ---------------------- | --------- | ---------------------------------------------------------------------------------- |
| `local-path-proxmox` | local-path-provisioner | `proxmox` | Proxmox-single CNPG DBs; Matrix, ActivityWatch, Scanner, OpenClaw, Tana MCP        |
| `local-path-ovh`     | local-path-provisioner | `hil-ovh` | OVH-HA CNPG DBs (authentik, gatus, forgejo, langfuse, …); SeaweedFS volume servers |
| `seaweedfs-ovh`      | SeaweedFS CSI          | `hil-ovh` | POSIX/S3-backed volumes for apps needing a real filesystem (Forgejo git repos)     |
| `lvm-proxmox-ssd`    | OpenEBS LVM CSI        | `proxmox` | NVMe thin provisioning: Firecracker                                                |
| `lvm-proxmox-hdd`    | OpenEBS LVM CSI        | `proxmox` | HDD thin provisioning: Harbor, Docker CI, Grocy                                    |
| `proxmox-csi-retain` | Proxmox CSI            | `proxmox` | Block storage via Proxmox API: Ollama, Devbot (migrating off)                      |

Proxmox CSI needs VLAN access to Proxmox API. OpenEBS LVM is constrained to nodes
with the `openebs-proxmox-ssd` / `openebs-proxmox-hdd` volume groups (currently Proxmox nodes only).
CNPG database placement (OVH-HA vs Proxmox-single) follows <docs/cnpg_conventions.md>.

## GPU (NVIDIA)

wyrm2 (NixOS, 2x RTX 5090) provides `nvidia.com/gpu` resources; GPU pods need
`runtimeClassName: nvidia`. Runtime stack and key files: <docs/gpu.md>.

## Failure Modes

| Scenario              | Cluster    | Ingress | DNS   | Authentik | Notes                                             |
| --------------------- | ---------- | ------- | ----- | --------- | ------------------------------------------------- |
| Single OVH CP down    | 2/3 quorum | Works   | Works | Works     | Surviving OVH CP carries ingress + lighthouse     |
| Multiple OVH CPs down | 1/3 only   | Down    | Down  | Down      | Home pods continue but cluster frozen             |
| Home down             | 3/3 quorum | Works   | Works | Works     | Public-critical services run on OVH-local storage |

### OVH-Only Resilience Invariants

DNS (AWS Route 53) and the public website must keep working/recovering with OVH
only (no Proxmox) — so they must not depend on `proxmox-csi-retain` storage or
Proxmox-pinned nodes. Full invariant set, compliance tracking, and fix plan:
<docs/plan.md> § "OVH-Only Resilience Invariants".

## SSO (Authentik)

All applications use Authentik SSO via native blueprints. See <docs/sso.md> for
secret flow, NetworkPolicy template, and blueprint tombstone rules.

## ActivityWatch

Personal activity tracking at `activitywatch:5600` over Nebula mesh (no built-in auth;
mesh membership is the trust boundary). Architecture and desktop client setup:
<docs/activitywatch.md>.

## Repository Structure

```text
cluster/
├── .envrc                  # direnv (KUBECONFIG, TALOSCONFIG; CLI tools from root devShell)
├── docs/                   # bootstrap, plan, troubleshooting, operations, secrets
├── terraform/
│   └── main/               # Single TF root (PG backend, all resources)
├── k8s/                    # Flux-managed manifests
│   ├── agents/             # Agent infra (openclaw, airlock, claude-rbac, tana-mcp, ...)
│   ├── authentik/          # SSO (app, blueprints, db, secrets, proxy-routes, ...)
│   ├── monitoring/         # Observability (stack, loki, alloy, tempo, ...)
│   ├── harbor/             # Registry (app, secrets, ci, webhook, ...)
│   ├── <service>/          # Grouped: subdirs per flux-kustomization (namespace, secrets, app, db)
│   ├── <service>/          # Flat: single flux-kustomization, all manifests at root
│   └── flux-system/        # Flux controllers (auto-generated)
└── validation/             # Structural validation tests
```

## Let's Encrypt Rate Limits

5 duplicate certs/week per domain (rolling 7-day window). Each destroy→bootstrap cycle
requests fresh certificates. Controlled by `k8s/cert-manager/issuer-config/configmap.yaml`.

**Note:** The legacy VPS at `agentydragon.com` is separate infrastructure not involved in
this cluster (see <docs/plan.md>).
