# Cluster Roadmap

**Last Updated**: 2026-02-23

## 🔥 Immediate Next Steps

**Status**: Cluster running with 4 nodes (2 VPS + 1 Proxmox CP + 1 GPU worker).
All kustomizations Ready. Cilium Gateway API serving HTTPS traffic.
DNS automation fully working. Authentik auth verified.
PowerDNS, Authentik, and Headscale all migrated to CloudNativePG on `local-path`.
VPS-only resilience invariant fully satisfied. Gatus monitoring comprehensive.
Headscale, Tempo, Langfuse, InvenTree, Scanner all deployed.

### Recent Changes (2026-02-23)

1. **Headscale → CloudNativePG PostgreSQL** — Migrated Headscale from SQLite with
   `local-path` PVC (single-node-bound) to a 2-instance CloudNativePG cluster
   (`headscale-db`) on `local-path` with `topologyKey: kubernetes.io/hostname`. Database
   password injected via ESO ExternalSecret (`headscale-db-values`) using a new
   `kubernetes-headscale-secret-store` ClusterSecretStore reading from the headscale
   namespace. Headscale `persistence.enabled: false` — keys stored in `headscale-keys`
   Secret so the pod can reschedule freely to the surviving VPS node.
2. **Authentik bundled PostgreSQL → CloudNativePG** — Migrated Authentik database from
   the bundled Bitnami PostgreSQL (single instance) to a 2-instance CloudNativePG cluster
   (`authentik-db`) on `hcloud-volumes`. Both CNPG pods run on Hetzner VPS nodes with
   `topologyKey: kubernetes.io/hostname` for real HA and zero-downtime failover. Removed
   the Vault-stored `postgres_password` and ESO ExternalSecret; CNPG auto-generates
   credentials in secret `authentik-db-authentik`. Server and worker now load the password
   via `env.valueFrom.secretKeyRef` instead of `envFrom`. Existing data dropped (fresh
   cluster — acceptable per plan). Old bundled PVC pending deletion.
3. **PowerDNS MariaDB → CloudNativePG PostgreSQL** — Migrated PowerDNS backend from
   MariaDB (proxmox-csi-retain) to a 2-instance CloudNativePG PostgreSQL cluster
   (`powerdns-db`) on `hcloud-volumes`. Both PostgreSQL pods run on Hetzner VPS nodes
   with `topologyKey: kubernetes.io/hostname` for real HA. PowerDNS DaemonSet now uses
   `GPGSQL_PASSWORD` from the CNPG-generated secret. VPS-only resilience invariant for
   DNS is now fully satisfied. Old orphaned MariaDB PVC (`data-powerdns-mariadb-0`)
   pending deletion.
4. **Gatus comprehensive monitoring** — Added monitors for Vault, Gitea, Grafana, Matrix,
   Harbor OIDC, Ollama, LiteLLM (including live inference probe), Langfuse, Loki,
   Prometheus, Hubble UI, Headlamp, InventTree, Headscale, Nix Cache, Atuin, Grocy,
   FileBrowser, OpenClaw.
5. **Headscale deployed** — Headscale HelmRelease and kustomization up. Gatus probe
   configured. Device migration from ansible VPS still pending.
6. **Tempo deployed** — Distributed tracing via Grafana Tempo in `monitoring` namespace.
7. **Langfuse deployed** — LLM observability platform in `langfuse` namespace.
8. **Scanner deployed** — Document scanning with FileBrowser frontend in `scanner` namespace.
9. **InvenTree deployed** — Inventory management in `inventree` namespace.
10. **Headscale SSO** — Authentik outpost configured for Headscale.

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

### Next Actions

- [ ] **Clean up orphaned MariaDB PVC** — `data-powerdns-mariadb-0` in `dns-system` is
      no longer used (PowerDNS switched to CloudNativePG). Delete once confirmed:
      `kubectl delete pvc data-powerdns-mariadb-0 -n dns-system`
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
      it for authenticated `/v2/` checks (proves full auth chain, not just 401 response).
      Currently the probe only checks for 401 (unauthenticated path).
- [ ] **Nix cache: initialize Attic cache** — Attic server is running but has no caches
      created (empty `cache` table). `cache.allegedly.works/nix-cache-info` returns 404
      because Attic serves that endpoint per-cache at `/<name>/nix-cache-info`. Fix: run
      `atticadm make-token` to generate an admin JWT, then `attic cache create main` and
      `attic cache configure main --public`. Either add an init Job to the chart/kustomization
      or run interactively once. The `atticadm` binary may not be in the current image
      (`ghcr.io/zhaofengli/attic:latest`) — check and potentially use a different tag or
      generate the token from the JWT secret directly. Gatus probe should use
      `cache.allegedly.works/main/nix-cache-info` once cache exists.
- [ ] **Headscale: test with a real device** — Headscale is deployed and running.
      Connect an actual device and verify connectivity end-to-end.
- [ ] **OpenClaw: eliminate one-time token entry** — currently the user must retrieve
      the auto-generated gateway token (`kubectl get secret openclaw-gateway-token ...`)
      and enter it once in the UI settings. Investigate options: operator exposing token
      in bootstrap config, gateway-side token injection into served HTML, or upstream
      PR to accept `"trusted-proxy"` in `sharedAuthOk` (message-handler.ts:385-387).
- [ ] **Headlamp: verify proxy outpost auth** — Switched to Authentik proxy outpost
      pattern (like gatus/grocy). Outpost pod is running but end-to-end flow is unverified.
      Verify: user hits `headlamp.allegedly.works` → Authentik login → proxy forwards to
      Headlamp → Headlamp uses SA for K8s API calls. If proxy outpost doesn't work for
      WebSocket-heavy apps, may need to configure Talos API server `--oidc-issuer-url`.
- [ ] **Headlamp: per-user K8s RBAC** — Currently all authenticated users share
      `cluster-admin` via the ServiceAccount. For per-user RBAC: configure Talos API
      server `--oidc-issuer-url` to trust Authentik, create `ClusterRoleBinding`s
      mapping Authentik groups to K8s roles (e.g. `authentik Admins` → `cluster-admin`).
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

| Component              | Status | Notes                                        |
| ---------------------- | ------ | -------------------------------------------- |
| Flux CD                | ✅     | GitOps                                       |
| Cilium Gateway API     | ✅     | Envoy hostNetwork on VPS nodes               |
| cert-manager           | ✅     | DNS-01 via PowerDNS, dual-issuer toggle      |
| PowerDNS               | ✅     | hostNetwork on VPS nodes, CNPG PostgreSQL DB |
| Vault                  | ✅     | With OIDC auth                               |
| Authentik              | ✅     | SSO provider, 2 replicas on VPS nodes        |
| External Secrets       | ✅     | Vault integration                            |
| Monitoring             | ✅     | Prometheus/Grafana/Loki/Tempo/Alloy          |
| Proxmox CSI            | ✅     | Storage for home nodes                       |
| local-path-provisioner | ✅     | Storage for VPS nodes                        |
| Stakater Reloader      | ✅     | Deployed, adopted (7/7 services)             |
| DNS Automation         | ✅     | tofu-controller manages Route53 + PowerDNS   |
| Node Feature Discovery | ✅     | Auto-detects GPU/hardware, provides labels   |
| NVIDIA Device Plugin   | ✅     | GPU resource registration on GPU nodes       |
| CloudNativePG          | ✅     | CNPG operator for PostgreSQL clusters        |
| Cilium Mutual Auth     | ⏸️     | SPIRE disabled (bootstrap timeout on Talos)  |

## Applications (already configured)

| App            | Purpose                | SSO | Notes                                |
| -------------- | ---------------------- | --- | ------------------------------------ |
| Harbor         | Container registry     | ✅  |                                      |
| Gitea          | Git hosting            | ✅  |                                      |
| Matrix/Element | Chat                   | ✅  |                                      |
| Nix cache      | Binary cache           | -   | Running, cache not initialized yet   |
| BuildBuddy     | Remote build exec      | -   |                                      |
| Headscale      | Tailscale control      | ✅  | Deployed, untested with real device  |
| Ollama         | LLM inference          | -   | + LiteLLM proxy                      |
| Website        | Static placeholder     | -   |                                      |
| OpenClaw       | AI coding agent        | ✅  |                                      |
| Gatus          | Health monitoring      | ✅  |                                      |
| Grocy          | Household/grocery mgmt | ✅  |                                      |
| Headlamp       | Kubernetes cluster UI  | ✅  | Proxy outpost; per-user RBAC pending |
| InvenTree      | Inventory management   | -   |                                      |
| Langfuse       | LLM observability      | -   |                                      |
| Tempo          | Distributed tracing    | -   | Part of monitoring stack             |
| Scanner        | Document scanning      | ✅  | Filebrowser frontend                 |
| Atuin          | Shell history sync     | -   |                                      |

## Applications (disabled - need flux-kustomization.yaml)

| App       | Purpose          | Status                                       |
| --------- | ---------------- | -------------------------------------------- |
| Firecrawl | Web scraping API | Helm chart + manifests exist, needs enabling |
| Devbot    | Agent workload   | Manifests exist, needs enabling              |

Re-add `flux-kustomization.yaml` files and integrate into root `kustomization.yaml`
when ready to deploy these applications.

---

## 📋 Production Cutover (`agentydragon.com`)

- [ ] Migrate all devices from ansible VPS headscale to cluster headscale (cluster headscale deployed at `headscale.allegedly.works`)
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

## 🛡️ VPS-Only Resilience Invariants

**Rule**: The following services MUST work/recover with VPS nodes only (Proxmox completely
down). They must NOT depend on `proxmox-csi-retain` storage or Proxmox-pinned workloads.

| Service   | Requirement                      | Status | Storage            | Notes                                             |
| --------- | -------------------------------- | ------ | ------------------ | ------------------------------------------------- |
| DNS       | Must resolve `*.allegedly.works` | ✅     | `hcloud-volumes`   | CNPG PostgreSQL cluster, 2 instances on VPS nodes |
| Website   | Must serve `allegedly.works`     | ✅     | None (stateless)   | No Proxmox dependencies                           |
| Ingress   | Must terminate HTTPS on VPS      | ✅     | None (hostNetwork) | Cilium Gateway on VPS nodes                       |
| Authentik | Must authenticate users          | ✅     | `hcloud-volumes`   | All components pinned to VPS                      |
| Vault     | Must serve secrets               | ✅     | `local-path`       | Raft storage, schedulable on VPS                  |

### Compliance Checklist

When adding or modifying critical-path services, verify:

1. **No `proxmox-csi-retain`** PVCs in the service's dependency chain
2. **No `topology.kubernetes.io/region: proxmox`** nodeSelector/affinity
3. Service can schedule on VPS nodes (no Proxmox-only resource requirements like GPU)
4. All upstream dependencies (databases, secret stores) also pass checks 1-3

### Proxmox-Dependent Services (Acceptable)

These services tolerate Proxmox downtime by design:

| Service    | Storage              | Impact when Proxmox down           |
| ---------- | -------------------- | ---------------------------------- |
| Harbor     | `proxmox-csi-retain` | Container registry unavailable     |
| Gitea      | `proxmox-csi-retain` | Git hosting unavailable            |
| Loki       | `proxmox-csi-retain` | Log ingestion stops, no log search |
| Nix cache  | `proxmox-csi-retain` | Binary cache unavailable           |
| Grafana    | `proxmox-csi-retain` | Dashboards unavailable             |
| BuildBuddy | Proxmox nodes        | Remote execution unavailable       |
| Ollama     | GPU worker           | LLM inference unavailable          |
| InvenTree  | `proxmox-csi-retain` | Inventory unavailable              |

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

### GitHub Webhook for Instant Reconciliation ✅

Flux `Receiver` resources and HTTPRoute deployed at `flux-webhook.allegedly.works`.
Harbor webhook auto-configured by `harbor-webhook` Terraform. GitHub secrets
(`HARBOR_ROBOT_USERNAME`, `HARBOR_ROBOT_TOKEN`) and GitHub webhook registered for
instant Flux GitRepository reconciliation on push.

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

### Vertical Pod Autoscaler (VPA) — Deployed (Recommendation-Only)

VPA deployed in `Off` (recommendation-only) mode via Fairwinds Helm chart (`k8s/vpa/`).
Monitors actual resource usage and generates right-sized request/limit recommendations.
Replaces manual `kubectl top` analysis (see <operations/2026-02-22-memory-request-rightsizing.md>).

### TODO: VPA Auto Mode

Once VPA recommendations have been validated over a few weeks, consider enabling `Auto`
mode (enable updater + admission controller). This automatically evicts pods and recreates
them with right-sized requests. Start with non-critical workloads first.

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
PostgreSQL for multiple services. Note: even without Galera, the immediate fix is migrating
MariaDB from `proxmox-csi-retain` to `hcloud-volumes` (VPS-only resilience invariant).

## 📋 Future Services (Lower Priority)

- [ ] Jellyfin (media streaming)
- [ ] \*arr stack (media automation)
- [ ] Paperless-ngx (document management)
- [ ] Syncthing (file sync)
- [ ] Bazel Remote Cache
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
truth). MariaDB backend must use `hcloud-volumes` storage (VPS-only resilience invariant).
Future: MariaDB Galera (3-node) for DB redundancy. ExternalDNS + powerdns-operator
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

| Location | Services                                             | Rationale                            |
| -------- | ---------------------------------------------------- | ------------------------------------ |
| VPS      | Vault, Authentik, Gateway, DNS (+ MariaDB), cert-mgr | Always-on, critical path (invariant) |
| Home     | Harbor, Gitea, Loki, Grafana, media, Nix cache       | Storage-heavy, can tolerate downtime |

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
- **Monthly Cost**: ~€30 (2x CPX31 + backups) — CPX41 upgrade planned (see `docs/plans/2026-02-22-vps-cpx41-upgrade.md`)
