# Cluster Architecture Redesign

Status: **Draft / In Progress**

See also: <sso.md>, <storage.md>

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
  light services (PowerDNS, kube-api-proxy, gateway/ingress).
- **New VPS worker(s)**: 2 Hetzner VPS workers for workloads that need
  public IP / always-on.

### VPS Sizing

**Decision**: 2x CCX13 CP + 2x CCX13 worker (all dedicated, uniform fleet).

| Role        | Type  | Cores       | RAM   | Disk   | EUR/mo    |
| ----------- | ----- | ----------- | ----- | ------ | --------- |
| CP (x2)     | CCX13 | 2 dedicated | 8 GB  | 80 GB  | 13.59     |
| Worker (x2) | CCX13 | 2 dedicated | 8 GB  | 80 GB  | 13.59     |
| **Total**   |       | 8 dedicated | 32 GB | 320 GB | **54.36** |

vs current 2x CPX31 at EUR 33.18 = **+64% (+EUR 21.18/mo)**.

Benefits: dedicated cores protect etcd from starvation. Uniform fleet
simplifies operations. 2 workers provide Authentik HA (anti-affinity)
and graceful failover. 16 GB total worker RAM is comfortable for all
VPS workloads.

Note: existing CPX31 nodes are grandfathered — CPX31 is no longer
available at HIL for new provisioning.

#### Current VPS CP Load (cordoned, 2026-04-01)

Both nodes are cordoned (no new workloads scheduled). Actual usage:

| Node | CPU actual | MEM actual    | MEM req | MEM lim |
| ---- | ---------- | ------------- | ------- | ------- |
| vps0 | 818m (20%) | 4586 Mi (64%) | 2070 Mi | 6309 Mi |
| vps1 | 942m (23%) | 5169 Mi (72%) | 1998 Mi | 6842 Mi |

**Memory is the bottleneck**, not CPU. Nodes use 4.5-5 GB actual RAM
despite only ~2 GB in requests. The gap is etcd, kube-apiserver, and
Longhorn sidecars (many pods with no resource requests).

### Hetzner Cloud Pricing

#### DC Selection

Only CPX (shared AMD) and CCX (dedicated AMD) are available in the US.
CAX (ARM) and CX (cost-optimized x86) are EU-only. ASH (US East) has
the same types as HIL — no reason to use it from California.

| DC   | Location        | Ping from SF | Types available   | Traffic |
| ---- | --------------- | ------------ | ----------------- | ------- |
| HIL  | Hillsboro, OR   | ~10-20 ms    | CPX, CCX          | 1-5 TB  |
| ASH  | Ashburn, VA     | ~60-75 ms    | CPX, CCX          | 1-5 TB  |
| FSN1 | Falkenstein, DE | ~140-160 ms  | CPX, CCX, CX, CAX | 20 TB   |
| NBG1 | Nuremberg, DE   | ~140-160 ms  | CPX, CCX, CX, CAX | 20 TB   |
| HEL1 | Helsinki, FI    | ~170-190 ms  | CPX, CCX, CX, CAX | 20 TB   |
| SIN  | Singapore       | ~170-200 ms  | CPX, CCX, CX      | 0.5 TB  |

**Decision: Stay in HIL.**

#### HIL Availability (checked 2026-04-01)

CPX31/41/51 are **no longer available** at HIL for new provisioning.
Gen2 types (CPX32/42) exist but are EU/Singapore only.

| Server    | vCPU | RAM   | Disk   | Type            | EUR/mo | +IPv4 | Available |
| --------- | ---- | ----- | ------ | --------------- | ------ | ----- | --------- |
| CPX11     | 2    | 2 GB  | 40 GB  | Shared (AMD)    | 4.49   | 5.09  | Yes       |
| CPX21     | 3    | 4 GB  | 80 GB  | Shared (AMD)    | 8.99   | 9.59  | Yes       |
| CPX31     | 4    | 8 GB  | 160 GB | Shared (AMD)    | 15.99  | 16.59 | **No**    |
| CPX41     | 8    | 16 GB | 240 GB | Shared (AMD)    | 29.99  | 30.59 | **No**    |
| **CCX13** | 2    | 8 GB  | 80 GB  | Dedicated (AMD) | 12.99  | 13.59 | Yes       |
| CCX23     | 4    | 16 GB | 160 GB | Dedicated (AMD) | 25.99  | 26.59 | Yes       |
| CCX33     | 8    | 32 GB | 240 GB | Dedicated (AMD) | 49.99  | 50.59 | Yes       |

### Server Type Change: In-Place Resize

The `hcloud` Terraform provider supports changing `server_type` **without
replacing the server**. Powers off, resizes, powers on. Same disk, same IPs.

- **`keep_disk = true`**: Important for downgrade flexibility.
- **Brief downtime** per node. Rolling upgrades to maintain etcd quorum.
- Existing rolling upgrade plan: <2026-02-22-vps-cpx41-upgrade.md>

## Placement Decisions

### VPS-Only Resilience (hard invariant)

If Proxmox (home lab) goes down entirely, the following **must still
work** using only VPS nodes:

- **Nebula mesh** (lighthouses + relays on VPS)
- **Website** (`allegedly.works`)
- **DNS** (PowerDNS, authoritative)
- **Gateway/Ingress** (public HTTPS)

Additionally: **Proxmox going down must not destabilize VPS.** Enforce
via hard `nodeSelector` on Proxmox workloads and PriorityClasses for
VPS-critical services.

### Hard Constraints (Decided)

| Service         | Where               | Reason                                                 |
| --------------- | ------------------- | ------------------------------------------------------ |
| Ollama          | Proxmox (wyrm2)     | GPU-bound (2x RTX 5090)                                |
| LiteLLM         | Proxmox             | Colocate with Ollama                                   |
| PowerDNS        | **Hetzner (any)**   | VPS-only resilience; CP or worker OK, DB is replicated |
| kube-api-proxy  | VPS (DaemonSet)     | Kubeconfig access on public IPs                        |
| Gateway/Ingress | **VPS only**        | VPS-only resilience + public IP                        |
| Website         | **VPS only**        | VPS-only resilience                                    |
| Nebula          | **VPS lighthouses** | VPS-only resilience                                    |

### Soft Constraints (Decided)

| Service   | Prefer  | Fallback | Reason                     |
| --------- | ------- | -------- | -------------------------- |
| Inventree | Proxmox | n/a      | Nice-to-have, not critical |
| Grocy     | Proxmox | n/a      | Nice-to-have, not critical |
| Matrix    | Proxmox | n/a      | Not critical               |
| Gitea     | Proxmox | n/a      |                            |
| nix-cache | Proxmox | n/a      |                            |
| Atuin     | Proxmox | n/a      |                            |
| Props     | Proxmox | n/a      |                            |

## Per-Service Placement

Legend: **bold** = decision made, normal = current state / TBD.

### Core Infrastructure (stateless / minimal storage)

| Service                | Placement           | Storage | Notes                                          |
| ---------------------- | ------------------- | ------- | ---------------------------------------------- |
| sealed-secrets         | TBD                 | none    |                                                |
| tofu-controller        | TBD                 | none    | Scope reduced 47→7 resources (see <sso.md>)    |
| reloader               | TBD                 | none    |                                                |
| cert-manager           | TBD                 | none    |                                                |
| external-dns           | TBD                 | none    |                                                |
| metrics-server         | TBD                 | none    |                                                |
| vpa                    | TBD                 | none    |                                                |
| goldilocks             | TBD                 | none    |                                                |
| descheduler            | TBD                 | none    |                                                |
| node-feature-discovery | TBD                 | none    |                                                |
| nvidia-device-plugin   | **wyrm2**           | none    | GPU only on wyrm2                              |
| reflector              | TBD                 | none    |                                                |
| cnpg operator          | TBD                 | none    |                                                |
| coredns-custom         | TBD                 | none    |                                                |
| hubble-ui              | TBD                 | none    |                                                |
| dns-automation         | TBD                 | none    |                                                |
| longhorn               | TBD                 | n/a     | TBD: keep longhorn at all?                     |
| Kyverno                | **Any (incl. CPs)** | none    | Admission controller, colocate with API server |

### Auth & Security

| Service          | Placement   | Storage              | CSI        | Replication    | Notes                   |
| ---------------- | ----------- | -------------------- | ---------- | -------------- | ----------------------- |
| Authentik        | VPS         | CNPG VPS-HA (2 inst) | local-path | CNPG streaming | Migrating to Authelia   |
| Vault            | VPS-capable | Raft on local-path   | local-path | Raft internal  | Dropping (see <sso.md>) |
| external-secrets | TBD         |                      |            |                | Goes away with Vault    |

### DNS & Networking

| Service        | Placement         | Storage              | CSI        | Notes                             |
| -------------- | ----------------- | -------------------- | ---------- | --------------------------------- |
| **PowerDNS**   | **Hetzner (any)** | CNPG VPS-HA (2 inst) | local-path | CP or worker OK, DB is replicated |
| **Gateway**    | **VPS**           | none                 |            | Cilium Envoy hostNetwork          |
| kube-api-proxy | **VPS DaemonSet** | none                 |            |                                   |

### Data Services

| Service           | Current | Proposed    | Storage                          | CSI                | Replication | Notes        |
| ----------------- | ------- | ----------- | -------------------------------- | ------------------ | ----------- | ------------ |
| Gitea             | Proxmox | **Proxmox** | CNPG (1 inst) + proxmox-csi PVC  | proxmox-csi-retain | None        |              |
| Harbor            | Proxmox | TBD         | CNPG (1 inst) + proxmox-csi PVCs | proxmox-csi-retain | None        |              |
| Matrix            | Proxmox | **Proxmox** | CNPG (1 inst)                    | local-path         | None        | Not critical |
| nix-cache (Attic) | Proxmox | **Proxmox** | CNPG (1 inst) + proxmox-csi PVC  | proxmox-csi-retain | None        |              |
| Atuin             | Proxmox | **Proxmox** | CNPG (1 inst)                    | local-path         | None        |              |
| tofu-state DB     | VPS     | TBD         | CNPG VPS-HA (2 inst)             | local-path         | CNPG        |              |
| Props             | Proxmox | **Proxmox** | CNPG (1 inst)                    | local-path         | None        |              |

### Monitoring & Observability

| Service         | Current      | Proposed                    | Storage    | Replication         | Notes                             |
| --------------- | ------------ | --------------------------- | ---------- | ------------------- | --------------------------------- |
| Prometheus      | wyrm2 (6 Gi) | **Replace with VM cluster** | —          | —                   | See VictoriaMetrics section below |
| VictoriaMetrics | —            | **VPS workers**             | local-path | Built-in (factor=2) | 2x vmstorage on VPS workers       |
| Grafana         | ?            | VPS workers                 | local-path | None                | Stateless-ish, rebuildable        |
| Loki            | Proxmox      | TBD                         | MinIO?     | Via MinIO           | See <storage.md>                  |
| Alloy           | ?            | Any                         |            |                     | DaemonSet, stateless              |
| Tempo           | ?            | VPS workers                 | local-path | None                | Rebuildable                       |
| AlertManager    | ?            | VPS workers                 | local-path | None                |                                   |
| Gatus           | ?            | VPS workers                 | local-path | None                |                                   |

#### VictoriaMetrics Cluster (replaces Prometheus)

Replace Prometheus with VictoriaMetrics cluster mode. ~3-10x less RAM
for the same data. Built-in replication, PromQL-compatible, Grafana
works as-is (Prometheus datasource type).

**Components:**

- `vmstorage` (x2): One per VPS worker, local-path storage,
  `replicationFactor=2` — each metric written to both nodes
- `vminsert` (x1): Receives remote-write from scrapers, distributes
  to storage nodes. Lightweight, can run anywhere.
- `vmselect` (x1): Serves PromQL queries, merges/deduplicates from
  both storage nodes. Lightweight, can run anywhere.

**Cross-site replication problem:** `vminsert` writes to
`replicationFactor` nodes synchronously with no topology awareness.
If vmstorage nodes span VPS + Proxmox, some writes cross the 20ms
Nebula link. Plan for **vmstorage on VPS only** with 2 nodes.

**Migration path:**

1. Deploy VM cluster alongside Prometheus
2. Configure dual remote-write (Alloy writes to both)
3. Verify dashboards work against vmselect
4. Switch Grafana datasource to vmselect
5. Remove Prometheus

### Agent / AI Services

| Service              | Current           | Proposed          | Storage | Notes                         |
| -------------------- | ----------------- | ----------------- | ------- | ----------------------------- |
| **Ollama**           | Proxmox           | **Proxmox/wyrm2** |         | **Hard: GPU**                 |
| **LiteLLM**          | ?                 | **Proxmox**       |         | Colocate with Ollama          |
| OpenClaw             | Proxmox (gateway) | **Proxmox**       |         |                               |
| Airlock              | VPS               | **VPS (later)**   |         | Consider moving to VPS worker |
| google-workspace-mcp | ?                 | TBD               |         |                               |
| tana-mcp             | Proxmox           | TBD               |         |                               |
| homeassistant-proxy  | ?                 | TBD               |         |                               |

### Misc

| Service             | Current  | Proposed            | Storage          | Notes                             |
| ------------------- | -------- | ------------------- | ---------------- | --------------------------------- |
| Headlamp            | ?        | TBD                 | none             |                                   |
| Scanner             | Proxmox  | TBD                 |                  | NFS for printer scans, behind SSO |
| ActivityWatch       | Proxmox  | TBD                 | proxmox-csi (1G) |                                   |
| Grocy               | ?        | **Proxmox (wyrm2)** | local-path       | Nice-to-have, not critical        |
| Website             | VPS      | **VPS only**        | none (stateless) | VPS-only resilience               |
| Proxmox-proxy       | Proxmox  | **Proxmox**         | none             | Hard: needs VLAN to 10.2.0.2      |
| BuildBuddy executor | scaled 0 | TBD                 |                  |                                   |

### Suspended Services

| Service   | Reason                     | Decision                                            |
| --------- | -------------------------- | --------------------------------------------------- |
| Inventree | VPS OOM (2026-03-17)       | **Proxmox (wyrm2)** when unsuspended. Nice-to-have. |
| Kagent    | ?                          | Keep suspended                                      |
| Langfuse  | Degraded Longhorn on wyrm2 | Keep suspended                                      |
| Firecrawl | ?                          | Keep suspended                                      |

## Remaining Decisions

### Tier 1: Blocking

1. **Longhorn: keep or drop?** See <storage.md>.
2. **Vault: keep or drop?** See <sso.md>.
3. **SSO provider: Authentik or Authelia?** See <sso.md>.
4. **Prometheus + Loki long-term placement.** See <storage.md>.

### Tier 2: Should decide

5. **Monitoring stack placement** (Grafana, Alloy, Tempo, AlertManager,
   Gatus).
6. **Harbor placement.** Effectively Proxmox-pinned via proxmox-csi.
7. **tofu-state DB.** Currently VPS-HA CNPG. Stays on VPS workers?
8. **Light services on CPs.** Candidates: PowerDNS, kube-api-proxy,
   Gateway/Ingress, Kyverno, CoreDNS, system DaemonSets.
9. **Hcloud CSI.** Remove or use for VPS worker storage?

### Tier 3: Low risk

10. **Stateless core infra** — run anywhere, prefer workers.
11. **Agent services** — Proxmox-preferred.
12. **Misc** (Headlamp, Scanner, ActivityWatch, BuildBuddy executor).
13. **Suspended services** — keep suspended for now.
14. **CNPG backup strategy.** Standardize pg_dump CronJobs.

## Migration Checklist

### Phase 1: Foundation (no workload disruption)

1. Remove default StorageClass annotation from `longhorn`
2. Create `local-path-hetzner` and `local-path-proxmox` StorageClasses
3. Update `cnpg-conventions.md` for region-explicit storage classes
4. Update existing CNPG clusters to use `local-path-{region}`

### Phase 2: VPS Node Restructure

5. Provision 2x CCX13 VPS workers in Hetzner (Terraform)
6. Provision 2x CCX13 VPS CPs in Hetzner (Terraform)
7. Join new workers + CPs, rolling etcd membership
8. Migrate workloads off old CPX31 CPs (drain, cordon)
9. Remove old CPX31 CPs from etcd, tear down

### Phase 3: Workload Placement

10. Pin Proxmox workloads with hard `nodeSelector`
11. Pin VPS-critical workloads
12. Move Authentik + Flux controllers to VPS workers
13. Deploy PriorityClasses
14. Verify VPS CPs are near-pure

### Phase 4: SSO Migration (Authentik → Authelia)

See <sso.md> for detailed migration strategy.

15. Deploy Authelia alongside Authentik
16. Set up SOPS + age for secrets
17. Migrate apps one by one (OIDC → proxy-mode → service accounts)
18. Suspend Authentik, Vault, ESO
19. After validation: delete Authentik, Vault, ESO code

### Phase 5: Monitoring Migration (Prometheus → VictoriaMetrics)

20. Deploy VictoriaMetrics cluster
21. Configure dual remote-write
22. Verify + switch Grafana datasource
23. Remove Prometheus

### Phase 6: Storage (evaluate and migrate)

See <storage.md> for validation plans.

24. Evaluate MinIO site replication + HAProxy
25. Evaluate Loki/Tempo on MinIO
26. Evaluate JuiceFS OR Rook/Ceph
27. Migrate remaining Longhorn PVCs
28. Decommission Longhorn

### Phase 7: Cleanup

29. Remove Longhorn operator and CRDs
30. Remove Vault, ESO, tofu-controller secret resources
31. Remove Authentik namespace
32. Update cluster docs
