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

| Service      | Current      | Proposed | Storage         | CSI                | Replication | Notes                              |
| ------------ | ------------ | -------- | --------------- | ------------------ | ----------- | ---------------------------------- |
| Prometheus   | wyrm2 (6 Gi) | TBD      | PVC             | longhorn?          | None        | Emergency pin, needs re-evaluation |
| Grafana      | ?            | TBD      |                 |                    |             |                                    |
| Loki         | Proxmox      | TBD      | proxmox-csi PVC | proxmox-csi-retain | None        | Needs re-evaluation                |
| Alloy        | ?            | TBD      |                 |                    |             |                                    |
| Tempo        | ?            | TBD      |                 |                    |             |                                    |
| AlertManager | ?            | TBD      |                 |                    |             |                                    |
| Gatus        | ?            | TBD      |                 |                    |             |                                    |

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

| Service             | Current  | Proposed            | Storage          | Notes                        |
| ------------------- | -------- | ------------------- | ---------------- | ---------------------------- |
| Headlamp            | ?        | TBD                 | none             |                              |
| Scanner             | ?        | TBD                 |                  |                              |
| ActivityWatch       | Proxmox  | TBD                 | proxmox-csi (1G) |                              |
| Grocy               | ?        | **Proxmox (wyrm2)** | local-path       | Nice-to-have, not critical   |
| Website             | VPS      | **VPS only**        | none (stateless) | VPS-only resilience          |
| Proxmox-proxy       | Proxmox  | **Proxmox**         | none             | Hard: needs VLAN to 10.2.0.2 |
| BuildBuddy executor | scaled 0 | TBD                 |                  |                              |

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
- MinIO on Proxmox vs cloud S3 bucket for data backend?
- Talos FUSE support? (Talos supports FUSE via system extensions)

### Proposed Storage Architecture (TBD)

| Use Case                                 | Storage                       | Notes                                       |
| ---------------------------------------- | ----------------------------- | ------------------------------------------- |
| Databases (CNPG)                         | local-path                    | App-level replication, per CNPG conventions |
| Bulk data (Harbor registry, Loki, media) | proxmox-csi-retain or JuiceFS | Durable, large                              |
| Ephemeral (Prometheus, Grafana, Tempo)   | local-path                    | Rebuildable, not precious                   |
| Shared multi-writer                      | JuiceFS (if adopted)          | RWX POSIX                                   |
| Vault                                    | Goes away                     | Dropping Vault                              |
