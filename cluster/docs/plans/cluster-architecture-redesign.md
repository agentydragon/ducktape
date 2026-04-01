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

Notable resource consumers still on CPs: Authentik DB (2 pods), Flux
controllers (4 pods), Kyverno (3 pods), Longhorn CSI controllers +
instance-manager, PowerDNS + DB, tofu-state-db, Matrix Redis.

**Implication**: CPX11 (2 GB) is too small for a CP. CPX21 (4 GB) is
very tight — etcd + apiserver + controller-manager alone use ~2-3 GB.
CCX13 (8 GB dedicated) or CPX31 (8 GB shared, current) are the safe
options. If we drop Longhorn, we reclaim significant memory from the
CSI sidecar pods.

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

**Decision: Stay in HIL.** Lowest latency from CA, same types/prices as
ASH. EU CAX is ~50% cheaper but 140+ ms latency.

#### HIL Availability (checked 2026-04-01)

CPX31/41/51 are **no longer available** at HIL for new provisioning.
Gen2 types (CPX32/42) exist but are EU/Singapore only. Only CPX11/21
(small shared) and CCX (dedicated) can be provisioned at HIL.

| Server    | vCPU | RAM   | Disk   | Type            | EUR/mo | +IPv4 | Available |
| --------- | ---- | ----- | ------ | --------------- | ------ | ----- | --------- |
| CPX11     | 2    | 2 GB  | 40 GB  | Shared (AMD)    | 4.49   | 5.09  | Yes       |
| CPX21     | 3    | 4 GB  | 80 GB  | Shared (AMD)    | 8.99   | 9.59  | Yes       |
| CPX31     | 4    | 8 GB  | 160 GB | Shared (AMD)    | 15.99  | 16.59 | **No**    |
| CPX41     | 8    | 16 GB | 240 GB | Shared (AMD)    | 29.99  | 30.59 | **No**    |
| CPX51     | 16   | 32 GB | 360 GB | Shared (AMD)    | 59.99  | 60.59 | **No**    |
| **CCX13** | 2    | 8 GB  | 80 GB  | Dedicated (AMD) | 12.99  | 13.59 | Yes       |
| CCX23     | 4    | 16 GB | 160 GB | Dedicated (AMD) | 25.99  | 26.59 | Yes       |
| CCX33     | 8    | 32 GB | 240 GB | Dedicated (AMD) | 49.99  | 50.59 | Yes       |

Existing CPX31 nodes are grandfathered but cannot be recreated if
destroyed. US traffic allowances slashed ~90% in Dec 2024.

### Server Type Change: In-Place Resize

The `hcloud` Terraform provider supports changing `server_type` **without
replacing the server**. The provider powers off the VM, calls the Hetzner
resize API, and powers it back on. Same disk, same IPs.

Key considerations:

- **`keep_disk = true`**: Prevents disk upsizing during resize. Important for
  downgrade flexibility — Hetzner cannot shrink disks once enlarged.
- **Brief downtime** per node during resize. Do rolling upgrades (one node at
  a time) to maintain etcd quorum (2/3).
- Current TF config does NOT `ignore_changes` on `server_type`, so `tofu plan`
  will detect and apply changes.
- Existing rolling upgrade plan: <2026-02-22-vps-cpx41-upgrade.md>

## Placement Decisions

### VPS-Only Resilience (hard invariant)

If Proxmox (home lab) goes down entirely, the following **must still
work** using only VPS nodes:

- **Nebula mesh** (lighthouses + relays on VPS)
- **Website** (`allegedly.works`)
- **DNS** (PowerDNS, authoritative)
- **Gateway/Ingress** (public HTTPS)

These services must not depend on `proxmox-csi-retain` storage,
Proxmox-pinned nodes, or any Proxmox-only resource. This constraint
already existed (see `cluster/docs/plan.md` "VPS-Only Resilience
Invariants") and carries forward to the new architecture.

Additionally: **Proxmox going down must not destabilize VPS.** Workloads
that were running on Proxmox must not flood onto VPS nodes and starve
etcd/DNS/ingress. Enforce via:

- Proxmox-only workloads pinned with `nodeSelector` (not just
  preferred) so they stay Pending rather than migrating to VPS
- PriorityClasses: VPS-critical services (DNS, ingress, Authentik)
  at `system-critical` priority; non-critical workloads at lower
  priority so they get evicted first under pressure
- ResourceQuotas / LimitRanges on VPS nodes if needed

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

| Service          | Placement           | Storage              | CSI        | Replication    | Notes                                          |
| ---------------- | ------------------- | -------------------- | ---------- | -------------- | ---------------------------------------------- |
| Authentik        | VPS                 | CNPG VPS-HA (2 inst) | local-path | CNPG streaming |                                                |
| Vault            | VPS-capable         | Raft on local-path   | local-path | Raft internal  | **TBD: keep or remove?**                       |
| external-secrets | TBD                 |                      |            |                | Depends on Vault                               |
| Kyverno          | **Any (incl. CPs)** | none                 |            |                | Admission controller, colocate with API server |

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
| Loki            | Proxmox      | TBD                         | JuiceFS?   | Via MinIO           | Needs re-evaluation               |
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

**Why this works:**

- Each vmstorage on local-path — no distributed storage needed
- replicationFactor=2 means both storage nodes have all data
- If one VPS worker dies, vmselect reads from the survivor
- RAM: ~1-2 Gi total vs Prometheus 6 Gi
- MetricsQL is a PromQL superset — existing Grafana dashboards and
  alert rules work without modification
- Helm chart available (`victoria-metrics-k8s-stack` or individual
  component charts)

**Cross-site replication problem:** `vminsert` writes to
`replicationFactor` nodes synchronously, picking targets by consistent
hashing with no topology awareness. If vmstorage nodes span VPS +
Proxmox, some writes will cross the 20ms Nebula link synchronously.
You can't control which metrics go where. Options:

- Keep all vmstorage on VPS only (no cross-site penalty, but no
  Proxmox copy — metrics lost if both VPS workers die)
- Accept mixed latency (some writes 20ms, tolerable for metrics
  ingestion but annoying)
- Investigate whether vminsert supports topology-aware placement
  or zone-local write preference (unclear from docs)

For now, plan for **vmstorage on VPS only** with 2 nodes. Proxmox
copy is a nice-to-have, not critical — metrics are rebuildable.

**Migration path:**

1. Deploy VM cluster alongside Prometheus
2. Configure dual remote-write (Alloy/Prometheus writes to both)
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

### Suspended Services (decide: revive, drop, or keep suspended?)

| Service   | Reason                     | Decision                                            |
| --------- | -------------------------- | --------------------------------------------------- |
| Inventree | VPS OOM (2026-03-17)       | **Proxmox (wyrm2)** when unsuspended. Nice-to-have. |
| Kagent    | ?                          | Keep suspended                                      |
| Langfuse  | Degraded Longhorn on wyrm2 | Keep suspended                                      |
| Firecrawl | ?                          | Keep suspended                                      |

## Secrets & SSO Architecture Options

### Current Setup (Vault + tofu-controller + ESO)

TF generates random secrets -> Vault KV -> ESO creates K8s Secrets ->
Authentik reads via `!Env` blueprint tags. Apps read from K8s Secrets.

3 operators (Vault, ESO, tofu-controller). Good rotation story, but
complex for ~10 static OAuth2 secrets.

### Option A: SOPS + age (recommended simplest)

Encrypt secrets in git with age/SOPS. Flux decrypts natively — zero
additional operators.

- Recovery: age private key (one string, fits in password manager) + git
- No cluster-bound state (unlike SealedSecrets whose key is cluster-specific)
- Same Authentik `!Env` integration, no changes needed
- Rotation: manual re-encrypt and commit
- Already on the cluster TODO list (see `plan.md`)

### Option B: SealedSecrets only (current bootstrap path)

Already have SealedSecrets for bootstrap. Could use it for everything.

- Need Reflector to mirror secrets across namespaces
- 2 operators (SealedSecrets, Reflector)
- Signing key is cluster-bound — must back it up (currently in TF state)
- Recovery: need signing key + git

### Option C: Authentik as SSOT

Let Authentik generate client secrets natively (omit `client_secret` in
blueprints). Extract via API into K8s Secrets with a Job.

- 0 extra operators, but needs custom extraction Job (~30 lines)
- Bootstrap ordering problem: apps need secrets before Authentik is ready
- Secrets tied to Authentik DB — DB loss = full SSO rebuild anyway
- Not portable to a different SSO provider

### Option D: Keep Vault, drop tofu-controller

Vault as pure KV store. Populate with a simple script/Job instead of TF.
ESO reads from Vault unchanged.

- 2 operators (Vault, ESO) — still running Vault for ~10 static strings
- Best automatic rotation story (write to Vault, ESO picks up)
- Vault needs unsealing, storage, HA — heavyweight for the use case

### Option E: K8s Secrets as SSOT (generate in cluster)

Job generates secrets, creates K8s Secrets. Reflector mirrors.

- 1 operator (Reflector)
- Secrets only in etcd — cluster wipe = new secrets, full SSO rebuild
- Worst durability

### Comparison

| Option                 | Operators         | Wipe Recovery           | Rotation     | Complexity  |
| ---------------------- | ----------------- | ----------------------- | ------------ | ----------- |
| Current (Vault+TF+ESO) | 3                 | Vault backup + TF state | Auto (TF)    | High        |
| A. SOPS+age            | 0 (Flux built-in) | age key + git           | Manual       | Lowest      |
| B. SealedSecrets       | 2                 | Signing key + git       | Manual       | Low         |
| C. Authentik SSOT      | 0 + Job           | Authentik DB            | Manual (API) | Medium      |
| D. Vault sans TF       | 2                 | Vault backup            | Auto (ESO)   | Medium-high |
| E. K8s Secrets         | 1                 | New secrets             | Manual       | Low         |

Decision: **SOPS + age** for secrets. See tofu-controller audit below.

### Tofu-Controller Audit

47 Terraform resources managed by tofu-controller. Breakdown:

**Replaceable by SOPS (29 resources)** — pure secret generation
(`random_password` → Vault KV). No external API calls:

- SSO client secrets: Gitea, Grafana, Harbor, Matrix, Vault, Inventree,
  Headlamp, Gatus, OpenClaw (9 resources in `sso-secrets` module)
- App admin passwords: Harbor, Gitea, Grafana, Inventree, Authentik,
  Props, Atuin, Matrix, Langfuse (9 resources)
- API keys/tokens: PowerDNS API key, Ollama API key, Ollama direct
  token, Alloy OTLP bearer token, Authentik API token, Authentik
  secret_key, Grafana Flux token, user passwords (11 resources)

**Must remain Terraform (7 resources)** — call external APIs:

| Resource             | API Called              | Purpose                                      |
| -------------------- | ----------------------- | -------------------------------------------- |
| `dns-records`        | AWS Route 53 + PowerDNS | NS delegation + lighthouse records           |
| `harbor-proxy-cache` | Harbor API              | Proxy cache projects (Docker Hub, GHCR, etc) |
| `harbor-props`       | Harbor API              | Props project + robot account                |
| `harbor-ci`          | Harbor API              | CI project + robots + webhook tokens         |
| `harbor-webhook`     | Harbor API              | Flux receiver webhook                        |
| `harbor-oidc-config` | Harbor API              | OIDC auth configuration                      |
| `vault-oidc-auth`    | Vault API               | OIDC auth backend (goes away if Vault drops) |

**Implication**: Can't fully drop tofu-controller — still need it for
DNS and Harbor API management. But scope drops from 47 → 7 resources
(85% reduction). If we also drop Vault (Authelia path), `vault-oidc-auth`
goes away → 6 resources. The 5 Harbor resources go away only if Harbor
is replaced.

`dns-records` is the hardest to replace — it manages AWS Route 53
nameserver delegation. Could theoretically use ExternalDNS but that
doesn't handle registrar-level NS records.

### SSO Provider Options

#### Current Authentik Usage (audit)

- 12 native OIDC apps (Grafana, Gitea, Harbor, Matrix, Vault, Headlamp,
  Gatus, Inventree, Airlock x2, OpenClaw Agent, Google Workspace MCP)
- 11 proxy-mode forward-auth apps (OpenClaw, Proxmox, Longhorn, Hubble,
  Loki, Grocy, Scanner, Goldilocks, Alloy OTLP, Google Workspace MCP,
  OpenClaw Mitmproxy)
- 2 public PKCE clients (Airlock SPA, Claude Code Airlock)
- 2 service accounts with bearer tokens / client_credentials
- 3 custom OAuth scopes (`airlock:propose`, `airlock:decide`,
  `airlock:read`)
- No LDAP/SCIM/SAML in use. MFA disabled (planned).
- 1 user (`agentydragon`), 2 groups

#### Project Health

| Project   | Stars  | Latest Release | Date       | Active | Lang   |
| --------- | ------ | -------------- | ---------- | ------ | ------ |
| Keycloak  | 33,658 | 26.5.6         | 2026-03-19 | Yes    | Java   |
| Authelia  | 27,360 | v4.39.16       | 2026-03-14 | Yes    | Go     |
| Authentik | 20,749 | 2026.2.1       | 2026-03-03 | Yes    | Python |
| Zitadel   | 13,388 | v4.13.0        | 2026-03-23 | Yes    | Go     |
| Dex       | 10,690 | v2.45.1        | 2026-03-03 | Yes    | Go     |
| Kanidm    | 4,741  | v1.9.2         | 2026-03-13 | Yes    | Rust   |

All actively maintained with recent releases.

#### Authelia Deep Dive (strongest lightweight candidate)

| Capability                    | Authelia           | Authentik (current)       |
| ----------------------------- | ------------------ | ------------------------- |
| OIDC Provider                 | Yes (certified)    | Yes                       |
| OIDC Consumer (upstream IdP)  | **No**             | Yes (Google, GitHub, etc) |
| Forward-auth / proxy SSO      | Yes (native)       | Yes (proxy outpost)       |
| Envoy ExtAuthz                | Yes (documented)   | Via outpost               |
| Cilium Gateway API            | Likely (via Envoy) | Not documented            |
| Client secrets in YAML        | Yes (hashed)       | Via `!Env` blueprints     |
| Custom OAuth scopes           | Yes                | Yes                       |
| Client credentials grant      | Yes                | Yes                       |
| PKCE public clients           | Yes                | Yes                       |
| TOTP / WebAuthn               | Yes                | Yes                       |
| Local user file (no LDAP)     | Yes (YAML file)    | Yes (DB)                  |
| Web UI for user mgmt          | **No**             | Yes                       |
| Single binary, no DB required | Yes (Go + SQLite)  | No (Django+PG+Redis)      |
| RAM footprint                 | ~20-50 MB          | ~500 MB - 1 GB+           |
| RAC (remote desktop/SSH/VNC)  | **No**             | Yes (Guacamole-based)     |
| SAML IdP                      | **No**             | Yes                       |
| SCIM provisioning             | **No**             | Yes                       |
| Application dashboard / UI    | **No**             | Yes                       |

**Key blocker**: Authelia **cannot federate upstream to Google/GitHub**.
It is provider-only — users must authenticate against Authelia's local
user file or LDAP. No "log in with Google". For a single-user personal
cluster this may be fine (just set a password in the YAML user file),
but it means no Google account integration.

**RAC**: Authentik feature for browser-based RDP/SSH/VNC to remote
machines (built on Guacamole). Not available in Authelia or any other
candidate. Would need Apache Guacamole separately if wanted.

#### How Each Provider Handles Shared Secrets

The core problem: SSO provider and app must share a client secret. Who
generates it, and how does the other side get it?

**Config-file providers (Authelia, Dex)** largely eliminate the problem:

- Client secrets live in the provider's YAML config file
- Generate secret once, put it in SSO config + app secret YAML
- SOPS-encrypt both, commit to git, Flux decrypts — done
- No API extraction, no ESO, no Vault, no operators
- Secret rotation = edit two SOPS files, commit, push

**DB-backed providers (Authentik, Keycloak, Zitadel, Kanidm)**:

- Config lives in a database, not files
- Either (a) inject pre-set secrets into the DB via env/API/import, or
  (b) let the provider generate secrets and extract via API
- Both paths need a side-channel (env vars, API Jobs, Terraform)
- This is what makes the current Vault+TF+ESO chain necessary

**Implication**: If we switch to Authelia or Dex + SOPS, we can drop
Vault, ESO, tofu-controller, and Reflector entirely. The secret
management story becomes: SOPS-encrypted YAML in git, Flux decrypts.
No moving parts beyond Flux itself.

**Trade-off**: Config-file providers have no UI for managing clients.
Adding a new SSO app = edit YAML, commit. For a personal cluster with
~10 apps that rarely change, this is fine.

#### What We'd Lose Switching to Authelia

1. **Google/GitHub upstream federation** — must use local password
2. **RAC** (remote desktop via browser) — need Guacamole separately.
   Guacamole works independently of Authentik: has native OIDC
   extension (works with Authelia), header-based auth (works behind
   forward-auth proxy), and its own local user DB. Only loss vs
   Authentik RAC is a separate web UI instead of embedded in the SSO
   dashboard.
3. **Web admin UI** — all config via YAML files
4. **SAML, SCIM** — not in use currently, but unavailable if needed
5. **Application dashboard** — no user-facing app launcher page

#### What We'd Gain

1. **~10-20x less RAM** (~50 MB vs ~1 GB for Authentik stack)
2. **Drop PostgreSQL** for SSO (Authelia uses SQLite or no DB)
3. **Drop Vault, ESO, tofu-controller** — SOPS replaces all of them
4. **Config-as-code native** — no blueprints, no `!Env` hacks
5. **Simpler bootstrap** — no DB migrations, no worker pods
6. **Fewer failure modes** — no blueprint hash desync, no ESO
   password generator issues, no Vault token rotation bugs

#### Dex as Alternative

Dex **can** federate to Google/GitHub (it's a connector/federator),
but it **cannot** do forward-auth/proxy-mode. So Dex alone won't
replace Authentik's proxy outpost for the 11 proxy-mode apps.

**Dex + Authelia combo**: Dex for upstream Google federation, Authelia
for forward-auth. But this adds complexity — two SSO components instead
of one.

#### Decision Framework

Key question: **Is "log in with Google" a hard requirement?**

- If no: **Authelia + SOPS** is the clear winner. Drops 4 operators,
  saves ~1 GB RAM, config-as-code, simplest bootstrap.
- If yes: Stay with **Authentik** (simplest path) or evaluate
  **Zitadel** (Go, lighter than Authentik, has upstream federation
  - Terraform provider). Dex+Authelia combo is possible but complex.

Decision: **Try Authelia.** Run in parallel with Authentik, migrate
app-by-app, turn off Authentik + Vault once fully migrated.

#### Authelia Service Account / M2M Capabilities

| Capability                    | Authentik                           | Authelia                                    |
| ----------------------------- | ----------------------------------- | ------------------------------------------- |
| Client credentials grant      | Full, UI-managed                    | Supported in YAML config                    |
| JWT access tokens             | Default                             | Opaque by default; opt-in per client        |
| Service account tokens        | Native SA objects, API-managed      | Not supported; use client_credentials       |
| Token rotation                | Blueprints (delete+recreate)        | Standard OAuth expiry (cleaner)             |
| Per-client group restrictions | Policy bindings (expression engine) | `authorization_policies` (group/user match) |
| Custom token claims           | Property mappings (flexible)        | Limited `claims_policies`                   |

**Alloy OTLP** (currently: long-lived bearer token, rotated every 60 min
via Authentik blueprint): Switch to `client_credentials` grant. Alloy
calls Authelia's token endpoint, gets a JWT, uses it as Bearer. Standard
OAuth token expiry replaces the manual delete+recreate pattern. Cleaner.

**OpenClaw Agent** (currently: `client_credentials` grant): Works
directly — same grant type, same flow, just different issuer URL.

**Per-client access control**: Authelia `authorization_policies` support
`group:admins` / `user:agentydragon` matching. Less expressive than
Authentik's policy engine but covers our use case (admin-only access).

#### Migration Strategy: Authelia + Authentik in Parallel

Deploy Authelia alongside Authentik. Migrate apps one at a time. Each
app gets a new client secret in Authelia's YAML config (SOPS-encrypted)
and a corresponding SOPS-encrypted K8s Secret in its namespace. The old
Authentik secret stays in Vault/ESO until Authentik is fully turned off.

**Dual-provider support per app:**

- Grafana, Matrix, Vault: support multiple OIDC providers simultaneously
  (safe rollback during migration)
- Harbor, Headlamp, Gatus, Inventree: single OIDC config (one provider
  at a time, must flip)
- Proxy-mode apps (11): the HTTPRoute decides which provider does
  forward-auth — flip the route, not the app

**Migration order:**

1. Deploy Authelia (tiny: single pod ~50 MB, optional Redis)
2. Native OIDC apps first (change issuer URL + client config):
   start with low-risk (Headlamp, Goldilocks, Gatus)
3. Proxy-mode apps second (rewire HTTPRoutes to Authelia ExtAuthz):
   Longhorn, Hubble, Scanner, etc.
4. Service accounts last (OpenClaw Agent, Alloy OTLP): verify
   client_credentials grant + JWT validation
5. Once all apps migrated: suspend Authentik, then Vault + ESO
6. After validation period: delete Authentik, Vault, ESO, and
   tofu-controller secret resources. Keep TF resources that call
   external APIs (Harbor, DNS).

## Remaining Decisions

### Tier 1: Blocking (decide before executing the plan)

1. **Longhorn: keep or drop?** Affects Prometheus storage, VPS CP memory
   (Longhorn sidecars are heavy), and whether we need hcloud-csi for VPS
   workers. Strong arguments for dropping (memory hog, problems on wyrm2).
   See "Storage Strategy" section below.
2. **Vault: keep or drop?** SOPS+age handles secrets. If we also go
   Authelia, Vault's only remaining use is `vault-oidc-auth` TF resource
   (which goes away with Vault). Harbor TF resources don't need Vault.
3. **SSO provider: Authentik or Authelia?** Affects worker RAM needs
   (~1 GB vs ~50 MB), operator count, entire secrets flow.
4. **Prometheus + Loki long-term placement.** Currently emergency-pinned
   to wyrm2 / proxmox-csi. Where do they live after the restructure?
   Depends on Longhorn decision and worker sizing.

### Tier 2: Should decide (but won't break if deferred)

5. **Monitoring stack placement** (Grafana, Alloy, Tempo, AlertManager,
   Gatus). Currently unassigned. Most are lightweight, can go anywhere.
6. **Harbor placement.** Uses proxmox-csi, effectively Proxmox-pinned.
   Just needs the explicit decision.
7. **tofu-state DB.** Currently VPS-HA CNPG. Stays on VPS workers?
8. **Light services on CPs.** Which services are acceptable on the
   "near-pure" CPs? Current candidates: PowerDNS, kube-api-proxy,
   Gateway/Ingress, Kyverno, CoreDNS, system DaemonSets.
9. **Hcloud CSI.** Currently deployed but unused. Remove or use for
   VPS worker storage?

### Tier 3: Low risk (can default or defer)

10. **Stateless core infra** (sealed-secrets, reloader, cert-manager,
    external-dns, metrics-server, vpa, goldilocks, descheduler,
    node-feature-discovery, reflector, cnpg operator, coredns-custom,
    hubble-ui, dns-automation). All stateless, small. Default: run
    anywhere, prefer workers.
11. **Agent services** (google-workspace-mcp, tana-mcp,
    homeassistant-proxy). Lightweight, Proxmox-preferred.
12. **Misc** (Headlamp, Scanner, ActivityWatch, BuildBuddy executor).
13. **Suspended services** (Kagent, Langfuse, Firecrawl). Keep
    suspended, revive, or drop entirely?
14. **CNPG backup strategy.** Standardize pg_dump CronJobs across all
    Proxmox CNPG clusters.

## Storage Strategy

### Current Longhorn Usage

All Longhorn PVCs (checked 2026-04-01):

| Namespace  | PVC                             | Size | Notes                      |
| ---------- | ------------------------------- | ---- | -------------------------- |
| gitea      | data-gitea-postgresql-0         | 10G  | Legacy, suspended          |
| harbor     | data-harbor-redis-0             | 1G   |                            |
| harbor     | data-harbor-trivy-0             | 5G   |                            |
| harbor     | database-data-harbor-database-0 | 1G   | Legacy (migrated to CNPG?) |
| harbor     | harbor-jobservice               | 1G   |                            |
| harbor     | harbor-registry                 | 30G  |                            |
| loki       | storage-loki-stack-0            | 20G  |                            |
| monitoring | db-alertmanager-monitoring-0    | 1G   |                            |
| monitoring | db-prometheus-monitoring-0      | 20G  |                            |
| monitoring | grafana                         | 2G   |                            |
| monitoring | storage-tempo-0                 | 10G  |                            |
| vault      | vault-raft-instance-{0,1,2}     | 2G   | Goes away if Vault drops   |

None of these benefit from Longhorn's cross-node replication:

- Harbor, Loki: Proxmox-pinned (proxmox-csi would work)
- Vault: Has its own Raft replication. Goes away if dropped.
- Monitoring (Prometheus, Grafana, Tempo, AlertManager): Ephemeral /
  rebuildable. local-path is fine.
- Gitea: Suspended.

Cross-site sync replication (Proxmox ↔ VPS over Nebula) would add
~20ms write latency per operation — too slow for databases.

### Distributed Storage Options Considered

Goal: replicated storage so workloads aren't pinned to specific nodes,
POSIX multi-writer (RWX) mounting, optionally async replication across
sites for DR.

| Solution           | RAM/node    | RWX / POSIX        | Async WAN  | Fit                                              |
| ------------------ | ----------- | ------------------ | ---------- | ------------------------------------------------ |
| Longhorn (current) | 500-700 MB  | NFS (fragile)      | S3 backup  | Bad: memory hog, issues on wyrm2                 |
| Rook/Ceph          | 1.5-3 GB    | CephFS (native)    | rbd-mirror | Bad: too heavy for 8 GB nodes                    |
| Piraeus/LINSTOR    | 150-300 MB  | RWO only           | DRBD-A     | OK but no RWX; needs DRBD kernel module on Talos |
| OpenEBS Mayastor   | 500 MB-1 GB | RWO only           | No         | Bad: hugepages, no async                         |
| Kadalu (GlusterFS) | 300-500 MB  | Native POSIX       | Geo-rep    | Bad: GlusterFS abandoned by Red Hat              |
| **JuiceFS**        | 200-300 MB  | **Full POSIX RWX** | **Yes**    | **Best fit — investigate**                       |
| SeaweedFS          | 300-500 MB  | FUSE (POSIX-ish)   | Yes        | More object store than FS                        |
| MinIO              | 300-500 MB  | S3 only            | Yes        | Not a filesystem                                 |

### JuiceFS (investigate)

JuiceFS is a FUSE-based distributed filesystem that separates metadata
and data storage:

- **Metadata**: Redis, PostgreSQL, TiKV, or SQLite. We already have
  CNPG — could use a dedicated CNPG cluster for JuiceFS metadata.
- **Data**: Any S3-compatible backend, or local disk. Could use MinIO
  on Proxmox, or a cloud S3 bucket for off-site DR.
- **Full POSIX RWX**: Multiple pods can mount the same volume
  read-write simultaneously. Real POSIX semantics (locking, etc).
- **CSI driver**: Exists and is reasonably mature.
- **Light client**: ~200-300 MB overhead (FUSE client per node).
- **Async replication**: Data backend handles replication (S3
  cross-region, MinIO site replication, etc).
- **Caching**: Built-in client-side caching for read-heavy workloads.

**Architecture for our cluster:**

```text
Pods (any node) → JuiceFS FUSE client → metadata (CNPG PostgreSQL)
                                       → data (MinIO on Proxmox or S3)
```

**What this enables:**

- PVCs that can follow pods to any node (not pinned to storage location)
- Shared filesystems for multi-pod workloads
- Proxmox storage with VPS-accessible data (via metadata + S3)
- Backup story: S3 backend handles durability

**Open questions:**

- FUSE performance for database workloads? (Probably still use
  local-path for CNPG, JuiceFS for everything else)
- Talos FUSE support? (Talos supports FUSE via system extensions)
- JuiceFS CSI has a known open issue with Talos (#1083, auth-file
  metadata connections). Verify before committing.

### Option: JuiceFS + MinIO Site Replication

Full architecture for site-independent, unpinned RWX storage.

**Components:**

- JuiceFS CSI driver (Helm chart: controller StatefulSet + DaemonSet)
- JuiceFS metadata: dedicated database in CNPG (VPS-HA, 2 instances).
  PostgreSQL is a supported metadata engine. Just `CREATE DATABASE
juicefs` in an existing CNPG cluster.
- MinIO at Proxmox: data backend, storage on proxmox-csi-retain
- MinIO at VPS: data backend, storage on hcloud volume
- MinIO site replication: active-active async between both instances
- HAProxy or DNS-based failover: each site prefers local MinIO

**MinIO site replication details:**

- Truly active-active: both sites accept writes simultaneously
- Async by default (configurable to sync, but async recommended at
  20ms RTT). Writes complete locally, replicate in background.
- Conflict resolution: last-write-wins (fine for JuiceFS — objects are
  content-addressed chunks, not user-editable files)
- Recovery after outage: background scanner detects missing objects and
  re-queues when the site comes back. For extended outages run
  `mc admin replicate resync`. No data loss for writes to the
  surviving site.
- MinIO docs say site replication requires "distributed deployments"
  (single-node-multi-drive minimum, not single-drive). Each site needs
  multiple drives/partitions for erasure coding.

**JuiceFS failover caveat:** JuiceFS connects to a single S3 endpoint.
It does not natively failover between MinIO instances. Need an external
mechanism (HAProxy per site preferring local MinIO, or DNS-based
failover).

**Data flow:**

```text
Pod on VPS → JuiceFS FUSE → metadata: CNPG (VPS-HA)
                           → data: HAProxy → MinIO-VPS (local)
                                    ↕ async site replication ↕
Pod on Proxmox → JuiceFS FUSE → metadata: CNPG (over Nebula)
                               → data: HAProxy → MinIO-Proxmox (local)
```

**Cost:** MinIO itself is lightweight (~100 MB RAM per instance). VPS
storage via hcloud volumes at EUR 0.044/GB/mo. For ~100 GB = ~EUR
4.40/mo.

**JuiceFS CSI overhead:** Per-node DaemonSet (lightweight). Per-PVC
mount pod (default 100m CPU / 512 Mi). Mount pods scale with PVC
count, not node count.

**JuiceFS project health:** 13.4k GitHub stars, Apache 2.0, actively
maintained (releases every 2-4 weeks). CSI driver at 289 stars but
frequent releases. Not CNCF, backed by Juicedata Inc. Community
edition is fully sufficient (enterprise adds managed metadata engine
and web console).

**Good workloads for JuiceFS:**

- Harbor registry storage (30G, bulk read-heavy)
- Loki log storage (20G, append-heavy)
- Tempo trace storage (10G)
- Grafana dashboards/config (2G)
- Shared data across pods (CI caches, media)
- Anything that should move VPS↔Proxmox without data migration

**Bad workloads (keep on local-path):**

- CNPG databases (high IOPS, use app-level replication)
- Latency-sensitive transactional workloads

**Comparison to Longhorn:**

| Aspect       | Longhorn                        | JuiceFS + MinIO                  |
| ------------ | ------------------------------- | -------------------------------- |
| Replication  | Sync only (all replicas ack)    | Async site replication via MinIO |
| Cross-site   | 20ms write penalty per op       | Local writes, async background   |
| RWX          | NFS hack (fragile)              | Native POSIX                     |
| Overhead     | 500-700 MB global + hidden pods | ~100 MB MinIO + 512 Mi per PVC   |
| PVC mobility | Within replica set              | Any node with FUSE client        |
| Complexity   | One operator                    | JuiceFS CSI + MinIO + HAProxy    |

### Alternative: Rook/Ceph

Leaning toward Ceph despite the resource overhead. It is the standard
answer for "nodes should be cattle, not pets" — the same property
that makes cloud K8s work (EBS, Persistent Disk). Without distributed
storage, PVCs pin pods to nodes and every node becomes a pet.

**What Ceph gives us that nothing else does well:**

- PVCs that follow pods to any node, no babysitting
- CephFS for full POSIX RWX (native, not FUSE)
- RBD for block storage with sync replication within a site
- rbd-mirror for async cross-site DR
- Battle-tested (CNCF graduated, 20+ years of Ceph, massive community)
- One system for everything (block, filesystem, object via RGW)

**Cross-site write latency problem (same as Longhorn):**

One of the main reasons for switching off Longhorn is its cross-site
write latency (every write waits for all replicas across Nebula).
Ceph with cross-site replicas has the **same fundamental problem.**

With a pool spanning VPS + Proxmox (size=3: 2 VPS + 1 Proxmox,
min_size=2): every write must wait for at least 2 replica acks.
Regardless of which site the pod is on, at least one ack crosses
the 20ms Nebula link. A pod on Proxmox writing to a VPS primary
pays ~40ms (round-trip to primary + back). A pod on VPS writing
to a local primary pays ~20ms (waiting for one ack, which may
come from the VPS replica in <1ms if lucky, but CRUSH can't
guarantee which replica acks first).

Setting `min_size: 1` removes the cross-site penalty (local ack
only) but weakens durability — data could be lost if the primary
dies before replicating.

**This means**: A single "works everywhere" pool with cross-site
replicas does NOT solve the Longhorn latency problem. To get fast
writes, you still need site-local pools (replicas only within one
site), which reintroduces the per-site storage class babysitting.

**Options:**

1. Site-local pools only (fast writes, no cross-site copy) — nodes
   are cattle within each site but not across sites. Cross-site DR
   via rbd-mirror (async, separate operation). Still need separate
   StorageClasses per site.
2. Single cross-site pool, accept 20ms writes — simpler ops, but
   same latency as Longhorn for cross-site replicas.
3. `min_size: 1` cross-site pool — fast local ack, weaker durability.
   Ceph still replicates, just doesn't block the write on it.

None of these give "one storage class, fast writes everywhere, cross-
site durability." That combination doesn't exist in any synchronous
replication system. Only JuiceFS+MinIO (async site replication with
local writes to object store) or similar async-first architectures
avoid this tradeoff.

**The resource concern:**

Ceph MON+OSD+MGR+MDS needs 1.5-3 GB/node minimum. On 8 GB CCX13
nodes that's 20-40% of RAM. But: if we're dropping Longhorn (500-700
MB + hidden overhead), Vault (~500 MB), and switching to Authelia
(~50 MB vs Authentik ~1 GB), we reclaim ~2 GB — enough to absorb
Ceph's overhead.

**Needs validation before committing.** Resource overhead numbers
are estimates from docs. Actual usage on a small 4-node cluster may
differ. Test before adopting in production.

#### Ceph Validation Plan

Test in a disposable cluster before adopting in production. Each
phase gates the next.

Phase 0 — Disposable test cluster:

1. Provision 3-4 small Talos VMs (Proxmox or Hetzner, cheap/temporary)
2. Install Rook operator + Ceph cluster with minimal config
3. Measure actual resource usage (MON, OSD, MGR, MDS) on small nodes
4. Document: how much RAM does Ceph actually use on 8 GB nodes with
   ~100 GB total storage? Is there enough headroom for workloads?

Phase 1 — Basic block storage (RBD):

5. Create a CephBlockPool with `size: 2` (2 replicas)
6. Create StorageClass, provision PVCs, run test pods
7. Kill a node, verify pod reschedules and PVC reattaches on another
8. Measure write latency and IOPS vs local-path baseline

Phase 2 — CephFS (POSIX RWX):

9. Create a CephFilesystem, deploy MDS
10. Mount from multiple pods simultaneously, verify RWX semantics
11. Measure MDS resource overhead

Phase 3 — Cross-site topology (the hard part):

12. Add nodes from a second site (VPS or separate Proxmox host)
13. Configure CRUSH rules to keep replicas within a site
14. Verify writes don't incur cross-site latency
15. Set up rbd-mirror for async cross-site replication
16. Test site failover: kill all nodes at one site, verify data
    accessible from the other after rbd-mirror promotion

Phase 4 — Observability stack on Ceph:

17. Deploy Loki + Tempo with Ceph RGW (S3-compatible) as backend
18. Deploy VictoriaMetrics with CephFS or RBD for TSDB storage
19. Verify monitoring stack survives node replacement

Phase 5 — Production migration plan:

20. Document node provisioning with OSD auto-setup
21. Plan Longhorn → Ceph PVC migration (data copy strategy)
22. Size production Ceph for the actual workload
23. Determine if CCX13 (8 GB) is sufficient or need CCX23 (16 GB)

### Proposed Storage Architecture

**Provisional decision**: Evaluate Rook/Ceph first (validation plan
above). Fall back to JuiceFS + MinIO if Ceph resource overhead is too
high for 8 GB nodes.

**MinIO topology**: Two separate MinIO instances (one per site),
synchronized via MinIO site replication (async, bidirectional). NOT
one distributed cluster across sites. Each is an independent server
with its own storage.

```text
VPS:     MinIO-VPS (hcloud volume storage)
           ↕ async site replication (bidirectional)
Proxmox: MinIO-Proxmox (proxmox-csi storage)
```

**MinIO access pattern**: HAProxy DaemonSet on each node prefers
local MinIO, falls back to remote. All consumers (JuiceFS, Loki,
Tempo) connect to `localhost:9000`.

| Use Case                    | Storage            | Notes                                       |
| --------------------------- | ------------------ | ------------------------------------------- |
| Databases (CNPG)            | local-path         | App-level replication, per CNPG conventions |
| Metrics (VictoriaMetrics)   | local-path         | Built-in replication (factor=2)             |
| Logs (Loki Simple Scalable) | MinIO (direct)     | Native S3 backend, replication via MinIO    |
| Traces (Tempo)              | MinIO (direct)     | Native S3 backend, replication via MinIO    |
| Replicated / multi-writer   | JuiceFS (on MinIO) | POSIX RWX, metadata in CNPG                 |
| Bulk data (Harbor registry) | JuiceFS (on MinIO) | Unpinned, durable                           |
| Ephemeral (Grafana)         | local-path         | Rebuildable, not precious                   |
| Vault                       | Goes away          | Dropping Vault                              |

Loki and Tempo support S3/MinIO natively — they talk to MinIO
directly via the HAProxy, no JuiceFS layer needed. JuiceFS is for
workloads that need POSIX filesystem semantics (Harbor registry,
shared data, etc).

#### Validation Plan

Validate the MinIO + HAProxy foundation before building JuiceFS and
migrating workloads on top. Each phase gates the next.

Phase 1 — MinIO site replication:

1. Deploy MinIO on Proxmox (proxmox-csi storage)
2. Deploy MinIO on VPS worker (hcloud volume)
3. Configure site replication (`mc admin replicate add`)
4. Write objects from both sites, verify they appear on both
5. Verify MinIO multi-drive requirement — test with SNMD (single-node
   multi-drive) at each site

Phase 2 — HAProxy failover:

6. Deploy HAProxy DaemonSet with health checks and local-preference
7. Write via HAProxy from both sites, verify routing to local MinIO
8. Kill MinIO-VPS, verify HAProxy falls back to MinIO-Proxmox
9. Bring MinIO-VPS back, verify resync completes
10. Kill MinIO-Proxmox, verify HAProxy falls back to MinIO-VPS

Phase 3 — Loki + Tempo on MinIO (low-risk, native S3 support):

11. Reconfigure Loki to use MinIO backend (Simple Scalable mode)
12. Reconfigure Tempo to use MinIO backend
13. Verify log/trace ingestion and querying works
14. Verify data survives a MinIO-site failover

Phase 4 — JuiceFS on MinIO:

15. Deploy JuiceFS CSI driver, create CNPG metadata database
16. Verify Talos FUSE support (known issue #1083)
17. Create test PVC, mount from pods on both sites
18. Write from both sites simultaneously (RWX validation)
19. Kill one MinIO, verify JuiceFS reads still work via HAProxy
20. Performance test: sequential/random I/O, metadata operations

Phase 5 — Migrate workloads:

21. Migrate Harbor registry storage to JuiceFS
22. Migrate remaining Longhorn PVCs
23. Decommission Longhorn

**Open items to figure out:**

- JuiceFS metadata reads: route RO PostgreSQL queries to CNPG read
  replicas (`-ro` service) to reduce load on primary. JuiceFS may not
  support separate RO/RW connection strings natively — may need a
  PgBouncer or HAProxy layer that splits read/write traffic.
- MinIO multi-drive requirement: site replication needs "distributed
  deployment" (single-node-multi-drive minimum). VPS side needs
  multiple hcloud volumes; Proxmox side is easy (ZFS).
- JuiceFS mount pod resource tuning: default 512 Mi per PVC may be
  excessive for small volumes.
- Loki replication_factor in Simple Scalable mode: do we need ingester
  replication if MinIO already handles data durability?
