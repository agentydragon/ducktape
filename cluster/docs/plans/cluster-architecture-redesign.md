# Cluster Architecture Redesign

Status: **Draft / In Progress**

## Motivation

etcd on VPS control planes gets starved by co-scheduled workloads, causing
cluster instability and painful recovery. Redesign node roles and workload
placement to prevent this.

## Current Topology

| Node                  | Role        | Location                      | Specs                      |
| --------------------- | ----------- | ----------------------------- | -------------------------- |
| vps0 (talos-vps-cp-0) | CP + worker | Hetzner CPX31 (4 vCPU / 8 GB) | Hillsboro OR               |
| vps1 (talos-vps-cp-1) | CP + worker | Hetzner CPX31 (4 vCPU / 8 GB) | Hillsboro OR               |
| talos-pve-cp-0        | CP + worker | Proxmox (atlas)               | Private 10.2.1.1           |
| wyrm2                 | Worker      | Proxmox (atlas), NixOS        | 2x RTX 5090, GPU workloads |

## Proposed Node Changes

- **vps0, vps1**: Become **pure (or near-pure) control planes**. Only very
  light services (PowerDNS, kube-api-proxy, gateway/ingress). Possibly
  downsize from CPX31.
- **New VPS worker(s)**: 1-2 Hetzner VPS workers for workloads that need
  public IP / always-on.

### VPS Sizing (TBD)

- CP instance type if downsized? (CPX11 = 2 vCPU / 2 GB, CX22 = 2 vCPU / 4 GB, ...)
- Worker instance type?
- How many VPS workers? (1 vs 2 for HA)

## Placement Decisions

### Hard Constraints (Decided)

| Service         | Where                                 | Reason                                |
| --------------- | ------------------------------------- | ------------------------------------- |
| Ollama          | Proxmox (wyrm2)                       | GPU-bound (2x RTX 5090)               |
| LiteLLM         | Proxmox                               | Colocate with Ollama                  |
| PowerDNS        | VPS (TBD: workers only, or also CPs?) | Needs public IP for authoritative DNS |
| kube-api-proxy  | VPS (DaemonSet)                       | Kubeconfig access on public IPs       |
| Gateway/Ingress | VPS                                   | Public IP for inbound traffic         |

### Soft Constraints (Decided)

None yet.

## Per-Service Placement

Legend: **bold** = decision made, normal = current state / TBD.

### Core Infrastructure (stateless / minimal storage)

| Service                | Placement | Storage | Notes                      |
| ---------------------- | --------- | ------- | -------------------------- |
| sealed-secrets         | TBD       | none    |                            |
| tofu-controller        | TBD       | none    | TBD: keep?                 |
| reloader               | TBD       | none    |                            |
| cert-manager           | TBD       | none    |                            |
| external-dns           | TBD       | none    |                            |
| metrics-server         | TBD       | none    |                            |
| vpa                    | TBD       | none    |                            |
| goldilocks             | TBD       | none    |                            |
| descheduler            | TBD       | none    |                            |
| node-feature-discovery | TBD       | none    |                            |
| nvidia-device-plugin   | **wyrm2** | none    | GPU only on wyrm2          |
| reflector              | TBD       | none    |                            |
| cnpg operator          | TBD       | none    |                            |
| coredns-custom         | TBD       | none    |                            |
| hubble-ui              | TBD       | none    |                            |
| dns-automation         | TBD       | none    |                            |
| longhorn               | TBD       | n/a     | TBD: keep longhorn at all? |

### Auth & Security

| Service          | Placement   | Storage              | CSI        | Replication    | Notes                    |
| ---------------- | ----------- | -------------------- | ---------- | -------------- | ------------------------ |
| Authentik        | VPS         | CNPG VPS-HA (2 inst) | local-path | CNPG streaming |                          |
| Vault            | VPS-capable | Raft on local-path   | local-path | Raft internal  | **TBD: keep or remove?** |
| external-secrets | TBD         |                      |            |                | Depends on Vault         |
| Kyverno          | TBD         | none                 |            |                |                          |

### DNS & Networking

| Service        | Placement         | Storage              | CSI        | Notes                               |
| -------------- | ----------------- | -------------------- | ---------- | ----------------------------------- |
| **PowerDNS**   | **VPS**           | CNPG VPS-HA (2 inst) | local-path | TBD: CP only, worker only, or both? |
| **Gateway**    | **VPS**           | none                 |            | Cilium Envoy hostNetwork            |
| kube-api-proxy | **VPS DaemonSet** | none                 |            |                                     |

### Data Services

| Service           | Current | Proposed | Storage                          | CSI                | Replication | Notes |
| ----------------- | ------- | -------- | -------------------------------- | ------------------ | ----------- | ----- |
| Gitea             | Proxmox | TBD      | CNPG (1 inst) + proxmox-csi PVC  | proxmox-csi-retain | None        |       |
| Harbor            | Proxmox | TBD      | CNPG (1 inst) + proxmox-csi PVCs | proxmox-csi-retain | None        |       |
| Matrix            | Proxmox | TBD      | CNPG (1 inst)                    | local-path         | None        |       |
| nix-cache (Attic) | Proxmox | TBD      | CNPG (1 inst) + proxmox-csi PVC  | proxmox-csi-retain | None        |       |
| Atuin             | Proxmox | TBD      | CNPG (1 inst)                    | local-path         | None        |       |
| tofu-state DB     | VPS     | TBD      | CNPG VPS-HA (2 inst)             | local-path         | CNPG        |       |
| Props             | Proxmox | TBD      | CNPG (1 inst)                    | local-path         | None        |       |

### Monitoring & Observability

| Service      | Current      | Proposed | Storage         | CSI                | Replication | Notes        |
| ------------ | ------------ | -------- | --------------- | ------------------ | ----------- | ------------ |
| Prometheus   | wyrm2 (6 Gi) | TBD      | PVC             | longhorn?          | None        | Memory-heavy |
| Grafana      | ?            | TBD      |                 |                    |             |              |
| Loki         | Proxmox      | TBD      | proxmox-csi PVC | proxmox-csi-retain | None        |              |
| Alloy        | ?            | TBD      |                 |                    |             |              |
| Tempo        | ?            | TBD      |                 |                    |             |              |
| AlertManager | ?            | TBD      |                 |                    |             |              |
| Gatus        | ?            | TBD      |                 |                    |             |              |

### Agent / AI Services

| Service              | Current           | Proposed          | Storage | Notes                |
| -------------------- | ----------------- | ----------------- | ------- | -------------------- |
| **Ollama**           | Proxmox           | **Proxmox/wyrm2** |         | **Hard: GPU**        |
| **LiteLLM**          | ?                 | **Proxmox**       |         | Colocate with Ollama |
| OpenClaw             | Proxmox (gateway) | TBD               |         |                      |
| Airlock              | ?                 | TBD               |         |                      |
| google-workspace-mcp | ?                 | TBD               |         |                      |
| tana-mcp             | Proxmox           | TBD               |         |                      |
| homeassistant-proxy  | ?                 | TBD               |         |                      |

### Misc

| Service             | Current  | Proposed | Storage          | Notes |
| ------------------- | -------- | -------- | ---------------- | ----- |
| Headlamp            | ?        | TBD      | none             |       |
| Scanner             | ?        | TBD      |                  |       |
| ActivityWatch       | Proxmox  | TBD      | proxmox-csi (1G) |       |
| Grocy               | ?        | TBD      |                  |       |
| Website             | VPS      | TBD      | none (stateless) |       |
| Proxmox-proxy       | ?        | TBD      |                  |       |
| BuildBuddy executor | scaled 0 | TBD      |                  |       |

### Suspended Services (decide: revive, drop, or keep suspended?)

| Service   | Reason                     | Decision |
| --------- | -------------------------- | -------- |
| Inventree | VPS OOM (2026-03-17)       | TBD      |
| Kagent    | ?                          | TBD      |
| Langfuse  | Degraded Longhorn on wyrm2 | TBD      |
| Firecrawl | ?                          | TBD      |

## Open Architecture Questions

### Secrets Architecture

1. **Keep Vault or not?** What replaces it if dropped?
2. **Source of truth for cross-service secrets**: For each secret/token, where
   does it originate and who holds the canonical copy?
3. **Storage wipe propagation**: If service X wipes storage, which secrets
   need re-provisioning? Document the dependency graph per secret. E.g., if
   service Y also keeps the token in its storage, both need a coordinated wipe.
4. **Tofu controller**: Keep using it? It currently manages some Vault/Authentik
   config via GitOps.

### Storage Strategy

1. **Longhorn**: Keep as default replicated storage? It had issues on wyrm2.
   Alternative: drop Longhorn, use proxmox-csi + local-path only?
2. **Hcloud CSI**: Currently deployed but unused. Remove or use for VPS
   workers?
3. **CNPG backup strategy**: pg_dump CronJobs exist for some DBs. Standardize?

### Light Services on Control Planes

Which services are acceptable to keep on VPS CPs despite the "near-pure CP"
goal? Current candidates:

- PowerDNS (very light, critical for DNS)
- kube-api-proxy (DaemonSet, minimal)
- Gateway/Ingress (Cilium, already on CPs)
- CoreDNS customization
