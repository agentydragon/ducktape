# Cluster Roadmap

**Last Updated**: 2026-02-18

## 🔥 Immediate Next Steps

**Status**: Cluster running with 4 nodes (2 VPS + 1 Proxmox CP + 1 GPU worker).
~84/87 kustomizations Ready (3 Harbor-related still converging).
Cilium Gateway API serving HTTPS traffic. DNS automation fully working.
Authentik auth verified.

### Recent Fixes (2026-02-18)

1. **Authentik moved to VPS nodes** — All Authentik components (server, worker, PostgreSQL,
   Redis, outposts) pinned to Hetzner VPS nodes. PostgreSQL switched from `proxmox-csi-retain`
   to `hcloud-volumes`. Server and worker scaled to 2 replicas with pod anti-affinity across
   VPS nodes. Fixes: cross-site latency causing 1-1.6s outpost API calls (Django ORM
   round-trips over VXLAN+KubeSpan), liveness probe kills (33 restarts/26h from 3s timeout
   with only 2 gunicorn workers), and single-node-failure vulnerability. Liveness/readiness
   probe timeout increased from 3s to 10s. Outpost deployments pinned via
   `kubernetes_json_patches` in Terraform config.

### Recent Fixes (2026-02-16)

1. **Cilium Gateway API migration complete** — Replaced ingress-nginx with Cilium
   Gateway API. Single `cluster-gateway` Gateway with wildcard + apex HTTPS listeners,
   HTTP→HTTPS redirect. Per-service HTTPRoutes in each app namespace. ingress-nginx
   directory deleted.
2. **Vault internal HTTPS→HTTP** — TLS now terminates at Gateway. All internal Vault
   URLs switched from `https://` to `http://` (ESO config, 16 tofu-controller specs).
   Removed `VAULT_CACERT`, `SSL_CERT_FILE` env vars and CA volume mounts.
3. **Gateway API CRDs: experimental channel** — Cilium 1.16.x requires TLSRoute CRD,
   only in experimental channel. Standard channel caused `"Required GatewayAPI resources
are not found"`. Added `kubectl wait --for=condition=Established` before Cilium install.
4. **cert-manager Gateway API enablement** — `--feature-gates=ExperimentalGatewayAPISupport`
   obsolete since cert-manager v1.15. `gateway-shim` controller was silently disabled.
   Switched to `config.enableGatewayAPI: true`. Wildcard + apex certs now auto-issued.
5. **external-dns Gateway API** — Added `--source=gateway-httproute`, RBAC for
   `gateway.networking.k8s.io` resources + namespaces. Added
   `external-dns.alpha.kubernetes.io/target` annotation on Gateway via Flux postBuild
   substitution from `cluster-info` ConfigMap (new `vps_ips_csv` key).
6. **dns-records terraform idempotency** — Route 53 glue records now use
   `allow_overwrite = true` (upsert across cluster lifecycles). Domain registration
   uses declarative `import` block + `lifecycle { ignore_changes }` for non-nameserver
   attributes (transfer lock, contacts, privacy). IAM policy slimmed from
   `route53domains:*` to 4 specific actions (see `docs/iam-policy-route53.json`).

### Recent Fixes (2026-02-13)

1. **Dual ClusterIssuer with single-toggle switching** — Two always-present ClusterIssuers
   (`letsencrypt-prod`, `letsencrypt-staging`). Active issuer selected by a single ConfigMap
   (`k8s/cert-manager-issuer-config/configmap.yaml`). Every Ingress has
   `cert-manager.io/cluster-issuer: "${LETSENCRYPT_ISSUER}"` annotation substituted by Flux,
   so flipping the toggle re-issues all certificates. Trust bundle also follows the toggle
   via `${LETSENCRYPT_ISSUER}-root-ca` naming convention (staging CA only trusted in staging
   mode). `ingressShim.defaultIssuerName` kept as fallback.
2. **Authentik MFA blocking login** — Custom flow without MFA stage via Terraform
   (`sso/users/main.tf`), domain-matched `authentik_brand`.
3. **Authentik ESO key naming** — All 4 Authentik ExternalSecrets now use direct `secretKey`
   matching the env var name (`AUTHENTIK_BOOTSTRAP_PASSWORD`, `AUTHENTIK_BOOTSTRAP_TOKEN`, etc.).
4. **Proxmox CSI `nodeSelector`** — chart uses top-level key, not `controller.nodeSelector`.
5. **ESO username keys** — Grafana/Gitea charts expect both username+password; added static
   username fields to ESO templates.

### Suspended Kustomizations

- **Kagent**: `kagent`, `kagent-namespace`, `kagent-secrets`

### Completed: Harbor CI + Flux Webhook (2026-02-18)

- [x] **GitHub secrets** `HARBOR_ROBOT_USERNAME` and `HARBOR_ROBOT_TOKEN` added to `agentydragon/ducktape`
- [x] **GitHub webhook** registered for instant Flux GitRepository reconciliation

### Next Actions

- [x] **Switch cert-manager to production Let's Encrypt** — done via dual-issuer toggle.
      Single ConfigMap in `k8s/cert-manager-issuer-config/` controls active issuer.
      Every Ingress has `cert-manager.io/cluster-issuer: "${LETSENCRYPT_ISSUER}"` annotation
      (Flux-substituted), so flipping the toggle re-issues all certs. Trust bundle follows
      via `${LETSENCRYPT_ISSUER}-root-ca` naming convention.
- [x] **Migrate from ingress-nginx to Cilium Gateway API** — completed 2026-02-16.
      See Recent Fixes above.
- [x] **Fix `dns-records` terraform** — Added `allow_overwrite` for glue records,
      `import` block for domain registration, `lifecycle { ignore_changes }` for
      non-nameserver attributes. IAM policy minimized to 4 actions. Applied successfully.
- [ ] **Grocy: provision API token for agent access** — after first login, create an API key
      in the Grocy UI (user menu → Manage API keys), store it at `kv/grocy/api-key` in Vault,
      then wire via ExternalSecret into OpenClaw (`GROCY_API_KEY`) and/or expose for Claude.
      REST API base: `https://grocy.allegedly.works/api/`.
- [ ] **Test all SSO flows** — Gitea verified working. Run `scripts/check-authentik-login.py`.
      Remaining to test: Harbor, Grafana, Matrix, Vault OIDC login via browser.
- [ ] **Re-enable MFA** (TOTP/WebAuthn) once device enrollment is set up. Current custom flow
      in `terraform/gitops/sso/users/main.tf` skips MFA. Add enrollment stage + MFA validation
      stage back when ready.
- [ ] **Wire `scripts/check-authentik-login.py` into bootstrap/CI** — currently manual.
      Consider adding to `bootstrap.py` health checks or as a flux kustomization health check.
- [ ] **Gatus: Harbor robot token for authenticated probe** — Create a Harbor robot account
      via Terraform with minimal read scope, store token in Vault, configure Gatus to use
      it for authenticated `/v2/` checks (proves full auth chain, not just 401 response)
- [ ] **Gatus: expand monitors** — Add probes for Vault, Gitea, Grafana, Matrix, etc.
- [ ] **Gatus: LiteLLM inference health probe** — Add a Gatus endpoint that sends a
      lightweight chat completion request to `ollama.allegedly.works` and verifies a
      valid response. Proves the full LiteLLM→Ollama inference pipeline is working.
- [ ] **Nix cache: initialize Attic cache** — Attic server is running but has no caches
      created (empty `cache` table). `cache.allegedly.works/nix-cache-info` returns 404
      because Attic serves that endpoint per-cache at `/<name>/nix-cache-info`. Fix: run
      `atticadm make-token` to generate an admin JWT, then `attic cache create main` and
      `attic cache configure main --public`. Either add an init Job to the chart/kustomization
      or run interactively once. The `atticadm` binary may not be in the current image
      (`ghcr.io/zhaofengli/attic:latest`) — check and potentially use a different tag or
      generate the token from the JWT secret directly. Gatus probe should use
      `cache.allegedly.works/main/nix-cache-info` once cache exists.
- [ ] **Deploy headscale**, test with a device
- [ ] **OpenClaw: eliminate one-time token entry** — currently the user must retrieve
      the auto-generated gateway token (`kubectl get secret openclaw-gateway-token ...`)
      and enter it once in the UI settings. Investigate options: operator exposing token
      in bootstrap config, gateway-side token injection into served HTML, or upstream
      PR to accept `"trusted-proxy"` in `sharedAuthOk` (message-handler.ts:385-387).
- [ ] **Headlamp: per-user OIDC auth** — Currently uses a shared `cluster-admin` ServiceAccount
      (`inCluster: true`). Switch to OIDC so Kubernetes API calls are made under the
      authenticated user's identity with per-user RBAC: 1. Configure k3s `--oidc-issuer-url` (Authentik OIDC endpoint) and `--oidc-client-id` 2. Update Headlamp HelmRelease to use OIDC flow (pointing at Authentik) 3. Create `ClusterRoleBinding`s mapping Authentik groups → Kubernetes roles
      (e.g. `authentik Admins` → `cluster-admin`)
      Benefit: audit logs show real usernames; compromised Authentik session can't
      exceed RBAC permissions; no single shared all-powerful SA token.
- [ ] **Ollama: per-user auth** — OpenClaw currently talks directly to Ollama (no auth,
      dummy `OLLAMA_API_KEY` env var). Wire up proper auth: either re-introduce LiteLLM
      proxy with API key (once OpenClaw's OpenAI streaming handler supports
      `reasoning_content`), or deploy an auth-aware reverse proxy in front of Ollama.
      The `openclaw-ollama-api-key` ESO and Vault secret (`kv/ollama/api-key`) already
      exist but are not consumed. Investigate Authentik user tokens (app passwords →
      `client_credentials` JWTs) for per-user access.
- [ ] **Harbor terraform: switch to robot accounts** — Currently uses admin database
      auth (UserID 1 always works regardless of auth mode). Switch to Harbor robot
      accounts for least-privilege. Needs: create robot account with admin scope,
      store credentials in Vault, update `harbor-proxy-cache` and `harbor-oidc-config`
      terraform modules to use robot account instead of admin password.
- [ ] **Consider removing `gitea-admin-token` Job** — originally created for Terraform to
      configure Gitea OAuth via admin API, but SSO moved to the Authentik blueprint pattern.
      Nothing currently consumes the token secret. May still be useful for future Gitea API
      automation (repo/org management).
- [ ] Rename `monitoring-stack` → `kube-prometheus` or `prometheus-grafana`
- [ ] **Verify ntfy.sh notifications** — confirm Flux reconciliation failure alerts
      actually arrive on phone via ntfy.sh push notifications.

## 🎯 Target Architecture

The cluster will run entirely on Talos:

- **2x Hetzner VPS** - Control plane nodes with public IPs
- **1+ Proxmox VMs** - Control plane + workers on home server (atlas)

No separate ansible-managed VPS. Everything currently on the VPS must move into the cluster.

**End state**: Cluster handles everything on `allegedly.works` (test) then `agentydragon.com` (production).

## Domain Strategy

| Domain             | Purpose                      | Status                  |
| ------------------ | ---------------------------- | ----------------------- |
| `allegedly.works`  | Test cluster (prod LE certs) | Active, serving traffic |
| `agentydragon.com` | Production (future cutover)  | On ansible VPS          |

## Current Nodes

| Node                   | Location | Role          | IP            |
| ---------------------- | -------- | ------------- | ------------- |
| talos-vps-cp-0         | Hetzner  | control-plane | (new on boot) |
| talos-vps-cp-1         | Hetzner  | control-plane | (new on boot) |
| talos-pve-cp-0         | Proxmox  | control-plane | 10.2.1.1      |
| talos-pve-gpu-worker-0 | Proxmox  | worker (GPU)  | 10.2.2.1      |

## Core Services (already configured)

| Component              | Status | Notes                                       |
| ---------------------- | ------ | ------------------------------------------- |
| Flux CD                | ✅     | GitOps                                      |
| Cilium Gateway API     | ✅     | Envoy hostNetwork on VPS nodes              |
| cert-manager           | ✅     | DNS-01 via PowerDNS, dual-issuer toggle     |
| PowerDNS               | ✅     | hostNetwork on VPS nodes                    |
| Vault                  | ✅     | With OIDC auth                              |
| Authentik              | ✅     | SSO provider                                |
| External Secrets       | ✅     | Vault integration                           |
| Monitoring             | ✅     | Prometheus/Grafana/Loki                     |
| Proxmox CSI            | ✅     | Storage for home nodes                      |
| local-path-provisioner | ✅     | Storage for VPS nodes                       |
| Stakater Reloader      | ✅     | Deployed, adopted (7/7 services)            |
| DNS Automation         | ✅     | tofu-controller manages Route53 + PowerDNS  |
| Node Feature Discovery | ✅     | Auto-detects GPU/hardware, provides labels  |
| NVIDIA Device Plugin   | ✅     | GPU resource registration on GPU nodes      |
| Cilium Mutual Auth     | ⏸️     | SPIRE disabled (bootstrap timeout on Talos) |

## Applications (already configured)

| App            | Purpose                | SSO |
| -------------- | ---------------------- | --- |
| Harbor         | Container registry     | ✅  |
| Gitea          | Git hosting            | ✅  |
| Matrix/Element | Chat                   | ✅  |
| Nix cache      | Binary cache           | -   |
| BuildBuddy     | Remote build exec      | -   |
| Headscale      | Tailscale control      | -   |
| Ollama         | LLM inference          | -   |
| Website        | Static placeholder     | -   |
| OpenClaw       | AI coding agent        | ✅  |
| Gatus          | Health monitoring      | ✅  |
| Grocy          | Household/grocery mgmt | ✅  |
| Headlamp       | Kubernetes cluster UI  | ✅  |

## Applications (disabled - need flux-kustomization.yaml)

| App       | Purpose          | Status                                       |
| --------- | ---------------- | -------------------------------------------- |
| Firecrawl | Web scraping API | Helm chart + manifests exist, needs enabling |
| Devbot    | Agent workload   | Manifests exist, needs enabling              |

TODO: Re-add flux-kustomization.yaml files and integrate into root kustomization.yaml
when ready to deploy these applications.

---

## 📋 Production Cutover (`agentydragon.com`)

- [ ] Deploy headscale, migrate all devices from ansible VPS headscale
- [ ] Atlas Proxmox accessible via headscale mesh (or `atlas.allegedly.works` proxy)
- [ ] Website hosted in cluster, verify accessible
- [ ] Update `agentydragon.com` DNS to point to cluster
- [ ] Decommission ansible-managed VPS

### Headscale Bootstrap DNS Workaround

**Context**: Tailscale's bootstrap DNS (via DERP servers) only resolves `tailscale.com`
domains. With headscale using `agentydragon.com`, clients can't resolve the control
server on boot (chicken-and-egg: DNS is set to 100.100.100.100 which requires the
tunnel to be up first).

**Workaround**: Static `/etc/hosts` entry on atlas pointing `agentydragon.com`
to the VPS IP. Managed in `ansible/atlas.yaml` (atlas-specific, not in the role).

**When VPS changes**: Must re-run ansible on all tailscale clients to update the IP.
Eventually `agentydragon.com` will have multiple IPs (cluster VPS nodes) — the hosts
entry will need to list all of them or use a single stable entry point.

---

## 🔧 Operational Hardening

### Secrets: Vault SSOT ✅ Complete

All application secrets use Terraform → Vault → ESO pattern. Zero ESO Password generators
remain. Stakater Reloader restarts pods on secret changes. See
<lessons_learned/2025-11-28-eso-password-generator-desync.md> for historical context.

### Kyverno GitOps Enforcement ✅

Deployed in Audit mode. `require-gitops` ClusterPolicy, HA (3 replicas).
TODO: Switch to `Enforce` after validation.

### Cilium Mutual Authentication (SPIRE) — Paused

SPIRE is disabled in `cilium-values.yaml` — install times out during bootstrap on Talos
(SPIRE pods never become ready). KubeSpan provides inter-node encryption. Revisit when
SPIRE/Talos compatibility improves.

- [ ] **Investigate SPIRE timeout** — determine root cause of SPIRE pod startup failure
      on Talos. May require Talos-specific securityContext or init container changes.
- [ ] Once SPIRE works: create test-mode CiliumNetworkPolicies, then promote to required.

### TODO: Firewall Hardening

All Hetzner firewall rules currently allow `0.0.0.0/0`. Keep 80/443/53 public; restrict
K8s API (6443), Talos API (50000-50001), etcd (2379-2380), kubelet (10250), KubeSpan (51820),
VXLAN (8472) to admin IPs and inter-node CIDRs.

### TODO: Remote Proxmox API Access

Proxmox API only reachable from home network (10.2.0.2:8006). CSI works (pods on Proxmox),
but `tofu apply` requires home network. Options: split CSI/provisioning hosts, add
Tailscale route, or accept limitation.

### TODO: Multi-Endpoint Kubeconfig via DNS

Kubeconfig points to single VPS IP. Use `api.allegedly.works` resolving to all CP nodes
for failover. Chicken-and-egg: bootstrap needs direct IP, post-bootstrap rewrites to DNS name.

### TODO: Terraform State Backup

`persistent-auth/terraform.tfstate` is the SSOT for sealed-secrets keypair — local file only,
no backup. Options: rclone+Google Drive, encrypted S3, git-crypt, or manual backup script.

### GitHub Webhook for Instant Reconciliation ✅ (pending manual registration)

Flux `Receiver` resources and HTTPRoute deployed at `flux-webhook.allegedly.works`.
Harbor webhook auto-configured by `harbor-webhook` Terraform. GitHub webhook requires
one manual `gh api` call — see "Next Actions (Harbor CI + Flux Webhook)" above.

### Authentik Blueprint Migration (Reduce TF State Coupling) — DONE

Migrated all 10 Authentik-targeting Terraform modules to native blueprints. TF state
secrets reduced from 11 to 1 (`tfstate-default-sso-secrets`). Blueprints are idempotent
YAML in `k8s/authentik/sso-blueprints.yaml` (ConfigMap mounted into worker). Client
secrets generated by consolidated `terraform/gitops/sso-secrets/` module → Vault → ESO →
worker env vars → blueprint `!Env` tags.

See <lessons_learned/2026-02-18-authentik-tf-state-lifecycle-coupling.md> for the original
analysis.

Alternatively: add K8s OwnerReferences from TF state secrets to the Authentik HelmRelease
(tofu-controller doesn't do this — upstream issue #937, open since 2024). This would make
K8s GC delete state secrets when the HelmRelease is deleted.

### TODO: Flux Kustomization Dependency Graph UI

Low priority. Weave GitOps or Capacitor for visualizing kustomization DAG.

### TODO: Back Up persistent-auth Terraform State

`persistent-auth/terraform.tfstate` is local-only SSOT for sealed-secrets keypair, CSI
tokens, Nix signing key. Minimum: rclone to encrypted cloud storage. Better: S3 backend
with OpenTofu native state encryption + versioning.

### TODO: Authentik HA PostgreSQL

Single PostgreSQL instance on one VPS node. Losing that VPS requires Hetzner volume
reattach (fast but manual). Consider CloudNativePG with streaming replication across
both VPS nodes for zero-downtime failover. Stretch goal: replicate to Proxmox so
Authentik can survive total Hetzner loss (fall back to home-only operation).

### TODO: Deploy etcd Backup

Deploy [talos-backup](https://github.com/siderolabs/talos-backup) CronJob with age
encryption to S3. Covers cluster state loss if 2/3 control-plane nodes fail simultaneously.

### TODO: Provision InvenTree API Tokens

After InvenTree is deployed and operational, provision API tokens for service integrations:

- **openclaw**: Token for the openclaw operator to query/update inventory
- **claude**: Token for Claude Code sessions to interact with InvenTree API

Steps: Log in as admin → User Management → API Tokens → Create token per service account.
Store tokens in Vault at `kv/inventree/api-tokens/{service}`.

### TODO: Deploy Velero for PVC Backup

Scheduled backups of PVCs (Harbor, Gitea, Loki, Postgres). Critical application data
currently has no backup strategy. Velero integrates with Proxmox CSI and Hetzner CSI.

### TODO: Default-Deny Cilium Network Policies

All pods can currently communicate freely. Deploy default-deny `CiliumNetworkPolicy` per
namespace. Use Hubble to observe traffic flows first, then generate baseline allow-rules.

### TODO: Pod Security Standards

Apply `restricted` PSS labels to application namespaces. System namespaces (`kube-system`,
`csi-proxmox`, `cilium`) keep `privileged`. Start with `warn` mode, promote to `enforce`.

### TODO: Kyverno Audit → Enforce

Switch `require-gitops` ClusterPolicy from Audit to Enforce mode after validation.

### TODO: ResourceQuota + LimitRange per Namespace

Prevent resource contention. Set default CPU/memory requests+limits via LimitRange.
Set namespace-level quotas via ResourceQuota.

### TODO: Alertmanager → ntfy Bridge

Deploy [alertmanager-ntfy](https://github.com/alexbakker/alertmanager-ntfy) for structured
alert routing with severity levels, action buttons (create silence, open Prometheus URL).

### TODO: SLO Definitions

Deploy Pyrra or Sloth for declarative SLO management. Start with: ingress availability
(99.5%), DNS availability (99.9%), Vault availability (99.9%). Auto-generates multi-window,
multi-burn-rate alerts (Google SRE methodology).

### TODO: Image Registry Allowlist

Kyverno policy restricting container images to known registries: `ghcr.io`, `docker.io`,
`registry.allegedly.works`, `quay.io`. Blocks unknown/untrusted registries.

## 🔀 Future Directions

### GPU Worker Node ✅

`talos-pve-gpu-worker-0`: 2x RTX 5090, 8 cores, 32GB fixed RAM, Ollama at `ollama.allegedly.works`.
TODO: Revisit virtio-mem when Proxmox adds support (Bugzilla #2949).

### BuildBuddy Remote Executor ✅

3 replicas on Proxmox, connected to `remote.buildbuddy.io`.

### Service Mesh (Future)

If Cilium mutual auth proves insufficient (need L7 policies, traffic splitting, retries,
circuit breakers), consider a full service mesh. Options:

- **Cilium Service Mesh** — native integration, no sidecars (eBPF-based L7 proxy). Natural
  evolution from current Cilium setup. Still maturing.
- **Istio ambient mode** — ztunnel (per-node L4) + waypoint proxies (per-service L7).
  No sidecars. Most mature option but heavier footprint.

Not needed while Cilium mutual auth + Gateway API cover the use cases.

### Shared PostgreSQL / MariaDB Galera

Replace single-instance MariaDB (PowerDNS) with 3-node Galera cluster (VPS-0, VPS-1, Proxmox)
on `local-path` storage. 2/3 quorum survives single node failure. Could also serve as shared
PostgreSQL for multiple services.

## 📋 Future Services (Lower Priority)

- [ ] Jellyfin (media streaming)
- [ ] \*arr stack (media automation)
- [ ] Paperless-ngx (document management)
- [ ] Syncthing (file sync)
- [ ] Bazel Remote Cache
- [ ] Grafana Tempo (distributed tracing, natural fit with Grafana + Loki)
- [ ] Capacitor (Flux dependency DAG visualization, lightweight single pod)
- [ ] Tetragon (eBPF runtime security enforcement, complements Cilium)
- [ ] Flagger (progressive delivery / canary analysis for deployments)

## 🔧 Low Priority Improvements

### cosign Image Signing

Sign container images in CI with cosign (keyless via GitHub Actions OIDC). Verify via
Kyverno `verifyImages` policies. Blocks unsigned images from deploying.

### BackendTLSPolicy for Internal HTTPS

Cilium doesn't yet support `BackendTLSPolicy` (Gateway API GA since v1.4.0). Track
upstream [cilium#31352](https://github.com/cilium/cilium/issues/31352). When supported,
re-enable HTTPS between gateway and backends (Vault, etc.) instead of HTTP for internal
traffic.

### Nix Cache Signing Key → GitOps Terraform

Consider moving signing key from persistent-auth to Vault SSOT (gitops terraform module).
Trade-off: invalidates existing cached store paths (acceptable if cache is ephemeral).

### ReadWriteMany (RWX) Shared Storage

Shared storage mountable from both cluster pods and non-cluster VMs (e.g., wyrm).
Use cases: LLM model snapshots, media libraries, shared caches. Cross-site access
via KubeSpan adds latency — large data should stay home-only.

## 📐 Architecture Decisions

### Hybrid VPS + Proxmox

**Rationale**:

- VPS for public ingress, DNS, always-on services
- Home for storage-heavy workloads, media, compute
- KubeSpan mesh provides encrypted connectivity
- Reduces single point of failure

**Network Design**:

- VPS nodes: Public IPs, control-plane role
- Home nodes: Private IPs (via KubeSpan), worker role
- Cilium VXLAN for pod overlay (tunnel mode required for VPS)

### CNI: Cilium with VXLAN

**Decision**: VXLAN tunnel mode (not native routing), Talos-recommended defaults only

**Rationale**:

- Hetzner VPS nodes are not on same L2 network
- Native routing fails: "gateway must be directly reachable"
- VXLAN encapsulates pod traffic between nodes
- KubeSpan docs warn non-default Cilium options cause "asymmetric routing"
- `MTU: 1370` in Helm values (uppercase key — case-sensitive, lowercase is silently ignored)
- Avoids fragmentation: VXLAN overhead (50) + WireGuard overhead (80) = 130, so 1500 - 130 = 1370

**Firewall**: UDP 8472 required for VXLAN overlay

See <lessons_learned/2026-02-11-cilium-mtu-cross-node-packet-loss.md>
for network stack diagrams and diagnostic checklist.

### KubePrism for Cluster Endpoint

**Decision**: Use `localhost:7445` as cluster_endpoint

**Rationale**:

- No VIP possible across VPS and home networks
- KubePrism runs on every node, proxies to available API servers
- Kubeconfig patched post-bootstrap to use real VPS IP

### DNS Architecture

PowerDNS runs in-cluster on VPS nodes with `hostNetwork: true` (no AXFR, single source of
truth). Future: MariaDB Galera (3-node) for DB redundancy. ExternalDNS + powerdns-operator
for declarative zone/record management.

### Storage Strategy: Consolidated VPS, Liberal Home

**Decision**: Minimize Hetzner volumes, consolidate databases; generous allocations on Proxmox

#### VPS Storage (small, fast-access)

- **Vault Raft** - If not using shared PG (small, 10GB)
- Target: 2-3 volumes max on VPS (~$1.60/month)

#### Home Storage (large, tolerates downtime)

- Gitea + PostgreSQL (50GB+)
- Loki log storage (100GB+)
- Media services (Jellyfin, \*arr stack)
- Nix cache (100GB+)

| Location | Services                                       | Rationale                            |
| -------- | ---------------------------------------------- | ------------------------------------ |
| VPS      | Vault, Authentik, Gateway, DNS, cert-manager   | Always-on, critical path             |
| Home     | Harbor, Gitea, Loki, Grafana, media, Nix cache | Storage-heavy, can tolerate downtime |

#### Shared PostgreSQL Option

- Single PostgreSQL pod on VPS with Hetzner volume
- Multiple databases: `vault`, `authentik`, etc.
- Secrets persist across cluster destroy/recreate

## 🔗 Related Documentation

- **Bootstrap Procedures**: <bootstrap.md>
- **Troubleshooting**: <troubleshooting.md>
- **Secret Sync Analysis**: <lessons_learned/2025-11-28-eso-password-generator-desync.md>

## 📊 Cluster Specifications

- **Nodes**: 4 (2 VPS control-plane, 1 Proxmox control-plane, 1 Proxmox GPU worker)
- **Talos**: v1.12.3
- **Kubernetes**: v1.35.1
- **CNI**: Cilium (VXLAN tunnel mode)
- **Monthly Cost**: ~€30 (2x CPX31 + backups)
