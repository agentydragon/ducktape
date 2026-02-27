# Cluster Roadmap

## 🔥 Next Steps

**Status**: Running with 4 nodes (2 VPS + 1 Proxmox CP + 1 GPU worker).
Cilium Gateway API serving HTTPS traffic. DNS automation working. Authentik auth verified.
PowerDNS, Authentik, Headscale migrated to CloudNativePG on `local-path`.

See <changelog.md> for detailed change history.

### Suspended Kustomizations

- **Kagent**: `kagent`, `kagent-namespace`, `kagent-secrets`

### Next Actions

- [ ] **OpenClaw: obfuscation detection forces approval despite `security: full`** —
      Upstream commit `0e28e50b4` (PR #24287, issue #8592) adds unconditional obfuscation
      detection that OR-overrides `requiresExecApproval` even when `ask: off` +
      `security: full`. Triggers on `python3 -c "...exec/eval/base64..."` patterns.
      Options: patch custom image to check security level before triggering, file
      upstream issue arguing `security: full` should opt out, or refine agent prompts
      to avoid inline Python with those keywords.
- [ ] **OpenClaw: fix Ollama model discovery timeout on startup** — `TimeoutError` on
      every pod restart (KubeSpan not ready). Options: init container wait, retry with
      backoff, or `startupProbe` delay.
- [ ] **Plaid integration: fix onboarding** — `link/token/create` returns 400.
      Plaid's production onboarding process is involved (redirect URI registration,
      per-product approval, environment-specific keys). Revisit when needed.
- [ ] **Grocy: provision API token for agent access**
- [ ] **Test all SSO flows** — Remaining: Harbor SSO. Run `scripts/check-authentik-login.py`.
- [ ] **Consider moving more PVCs to `local-path`** — Proxmox CSI has 29 LUN hard limit.
      Candidates: `langfuse/langfuse-s3`, `monitoring/alertmanager-*`, `monitoring/prometheus-*`,
      `monitoring/storage-tempo-0`, `monitoring/kube-prometheus-stack-grafana`,
      `loki/storage-loki-stack-0`, `harbor/harbor-jobservice`
- [ ] **Re-enable MFA** (TOTP/WebAuthn) once device enrollment is set up
- [ ] **Wire `scripts/check-authentik-login.py` into bootstrap/CI**
- [ ] **Gatus: Harbor robot token for authenticated `/v2/` probe**
- [ ] **Nix cache: initialize Attic cache** — No caches created yet. Run `attic cache create main` + `attic cache configure main --public`. May need init Job or interactive setup.
- [ ] **Headscale: test with a real device**
- [ ] **OpenClaw: eliminate one-time token entry** — Options: operator bootstrap config,
      gateway-side injection, or upstream PR for `"trusted-proxy"` in `sharedAuthOk`.
- [ ] **Proxy outpost HA: shared session storage** — 1 replica limit (sessions in `/dev/shm`).
      Options: `CiliumEnvoyConfig` cookie hash, Gateway API `BackendLBPolicy`, upstream fix.
- [ ] **OAuth broker: upgrade Google scopes (needs approval flow)** — Add write
      scopes once agent approval mechanism is in place: `calendar` (read-write),
      `gmail.send`, `gmail.compose` (drafts + send), `drive` (read-write),
      `spreadsheets` (read-write).
- [ ] **OAuth broker: add Google Photos readonly** —
      `photoslibrary.readonly`. Enable Photos Library API in GCP project.
- [ ] **OAuth broker: add Google Tasks readonly** —
      `tasks.readonly`. Enable Tasks API in GCP project.
- [ ] **OAuth broker: add Google Slides readonly** —
      `presentations.readonly`. Enable Slides API in GCP project.
- [ ] **OAuth broker: add Google Forms readonly** —
      `forms.body.readonly` + `forms.responses.readonly`. Enable Forms API in GCP project.
- [ ] **OAuth broker: add Google Keep write scope** —
      `keep` (full read-write). Requires approval flow for write operations.
- [ ] **OAuth broker: reflect only access tokens** — Currently the Reflector-mirrored
      secret contains the long-lived refresh token. Consider reflecting only the
      short-lived access token to consumer namespaces, keeping refresh tokens in the
      `oauth-broker` namespace only. Reduces blast radius if a consumer namespace is
      compromised.
- [ ] **Ollama: per-user auth** — Options: Authentik JWTs, LiteLLM proxy.
- [ ] **Harbor terraform: switch to robot accounts** for least-privilege
- [ ] **Harbor CI robot: scope per-namespace pull secrets to read-only on specific projects** —
      `activitywatch`, `inventree`, `openclaw`, `props` namespaces all use the same system-level
      `ci` robot account (`kv/harbor/ci-robot`) which has push+pull on all projects. Create
      project-scoped read-only robot accounts per namespace for imagePullSecrets (pull-only,
      scoped to the relevant project). Keep the system-level `ci` robot for CI push only.
- [ ] **Consider removing `gitea-admin-token` Job** — SSO moved to blueprints, nothing
      consumes the token. May be useful for future Gitea API automation.
- [ ] **Harbor proxy cache: add GHCR credentials for private repos** — 403 on
      `openclaw/openclaw`. Needs GitHub PAT (`read:packages`) in Vault → ESO → Harbor.
- [ ] **Verify ntfy.sh notifications** — confirm Flux failure alerts arrive on phone
- [ ] **ActivityWatch: build and push initial image** — Run `activitywatch-image.yml`
      workflow manually after Harbor CI creates the project. Verify pod starts.
- [ ] **ActivityWatch: set up desktop client sync** — see "ActivityWatch Setup" below
- [ ] **ActivityWatch: Headscale direct access** — once Headscale is tested with real
      devices, switch from `kubectl port-forward` to direct mesh access
- [ ] **ActivityWatch: Android sync** — not feasible with current upstream (`aw-android`
      embeds its own server, no remote sync). Revisit when `aw-sync` ships for Android

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

## 🛡️ VPS-Only Resilience Invariants

**Rule**: The following services MUST work/recover with VPS nodes only (Proxmox completely
down). They must NOT depend on `proxmox-csi-retain` storage or Proxmox-pinned workloads.

| Service   | Requirement                      | Status | Storage            | Notes                                             |
| --------- | -------------------------------- | ------ | ------------------ | ------------------------------------------------- |
| DNS       | Must resolve `*.allegedly.works` | ✅     | `local-path`       | CNPG PostgreSQL cluster, 2 instances on VPS nodes |
| Website   | Must serve `allegedly.works`     | ✅     | None (stateless)   | No Proxmox dependencies                           |
| Ingress   | Must terminate HTTPS on VPS      | ✅     | None (hostNetwork) | Cilium Gateway on VPS nodes                       |
| Authentik | Must authenticate users          | ✅     | `local-path`       | All components pinned to VPS                      |
| Vault     | Must serve secrets               | ✅     | `local-path`       | Raft storage, schedulable on VPS                  |

### Compliance Checklist

When adding or modifying critical-path services, verify:

1. **No `proxmox-csi-retain`** PVCs in the service's dependency chain
2. **No `topology.kubernetes.io/region: proxmox`** nodeSelector/affinity
3. Service can schedule on VPS nodes (no Proxmox-only resource requirements like GPU)
4. All upstream dependencies (databases, secret stores) also pass checks 1-3

### Proxmox-Dependent Services (Acceptable)

These services tolerate Proxmox downtime by design:

| Service       | Storage              | Impact when Proxmox down           |
| ------------- | -------------------- | ---------------------------------- |
| Harbor        | `proxmox-csi-retain` | Container registry unavailable     |
| Gitea         | `proxmox-csi-retain` | Git hosting unavailable            |
| Loki          | `proxmox-csi-retain` | Log ingestion stops, no log search |
| Nix cache     | `proxmox-csi-retain` | Binary cache unavailable           |
| Grafana       | `proxmox-csi-retain` | Dashboards unavailable             |
| BuildBuddy    | Proxmox nodes        | Remote execution unavailable       |
| Ollama        | GPU worker           | LLM inference unavailable          |
| InvenTree     | `proxmox-csi-retain` | Inventory unavailable              |
| ActivityWatch | `proxmox-csi-retain` | Activity tracking unavailable      |

## Operational Hardening

### Secrets: Vault SSOT

All application secrets use Terraform → Vault → ESO pattern. Zero ESO Password generators
remain. Stakater Reloader restarts pods on secret changes. See
<lessons_learned/2025-11-28-eso-password-generator-desync.md> for historical context.

### Kyverno GitOps Enforcement

Deployed in Audit mode. `require-gitops` ClusterPolicy, HA (3 replicas).

- [ ] Switch to `Enforce` after validation.
- [ ] **Generic operator exclusion** — Replace per-operator SA whitelist with a generic
      rule skipping resources with `ownerReferences`. Requires verifying Kyverno
      `preconditions` support for `request.object.metadata.ownerReferences` length.
- [ ] **Image registry allowlist** — Kyverno policy restricting images to `ghcr.io`,
      `docker.io`, `registry.allegedly.works`, `quay.io`.

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

### TODO: Back Up persistent-auth Terraform State

`persistent-auth/terraform.tfstate` is local-only SSOT for sealed-secrets keypair, CSI
tokens, Nix signing key. Minimum: rclone to encrypted cloud storage. Better: S3 backend
with OpenTofu native state encryption + versioning.

### GitHub Webhook triggers reconciliation

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

### TODO: Deploy etcd Backup

Deploy [talos-backup](https://github.com/siderolabs/talos-backup) CronJob with age
encryption to S3. Covers cluster state loss if 2/3 control-plane nodes fail simultaneously.

### TODO: Provision InvenTree API Tokens

Provision API tokens for openclaw and Claude Code. Store in Vault at
`kv/inventree/api-tokens/{service}`.

### TODO: Deploy Velero for PVC Backup

Scheduled backups of PVCs (Harbor, Gitea, Loki, Postgres). Critical application data
currently has no backup strategy. Velero integrates with Proxmox CSI and Hetzner CSI.

### TODO: CiliumNetworkPolicy Rollout (Security Audit 2026-02-25)

Most services lack network policies. Intra-cluster APIs are unprotected — any pod
(including openclaw sandbox, props evaluators) can reach sensitive services directly.
Authentik proxy-backed services (grocy, gatus, hubble-ui, oauth-broker, scanner,
openclaw, alloy-otlp) now have policies; see the pattern in `cluster/AGENTS.md`.

**End goal**: Default-deny per namespace with explicit allow-rules. Use Hubble to
observe traffic flows first, then generate baseline allow-rules.

**Priority 1 — Secrets & DNS** (unauthenticated admin APIs, high blast radius):

- [ ] **Vault** — restrict ingress to ESO, tofu-controller runner, Vault namespace only.
      Any pod can currently reach `instance.vault:8200` and attempt auth method exploitation.
- [x] **PowerDNS API** — restrict `powerdns-api.dns-system:8081` to cert-manager webhook,
      powerdns-operator, and external-dns. Unauthenticated zone modification = DNS poisoning.
- [ ] **Authentik API** — restrict `authentik-server.authentik:80` to system namespaces
      and tofu-controller. Unauthenticated `/api/v3/` enables user enumeration, provider
      tampering, backdoor user creation.
- [ ] **Prometheus** — restrict `prometheus.monitoring:9090` to Grafana, Alertmanager.
      No auth; exposes full cluster topology, node IPs, resource usage, secret cardinality.
- [ ] **Sealed Secrets webhook** — restrict to flux-system namespace only.

**Priority 2 — Application services** (unauthenticated APIs, medium blast radius):

- [ ] **Harbor** — restrict `harbor.harbor:80` to Flux image-automation, CI pull jobs.
      Unauthenticated push/pull/API access from any pod.
- [ ] **Ollama** — restrict `ollama.ollama:11434` to OpenClaw operator, props, LiteLLM.
      Explicitly exclude openclaw-sandbox (untrusted code → GPU resource exhaustion).
- [ ] **Grafana** — restrict to Authentik proxy outpost only (intra-cluster dashboard
      queries bypass SSO).
- [ ] **Alertmanager** — restrict to Prometheus and webhook receivers. Unauthenticated
      alert silencing/creation.
- [ ] **Gitea** — restrict `gitea.gitea:3000` to webhook receivers, Flux, Authentik proxy.
- [ ] **Tempo** — restrict to Grafana, Alloy only (trace data contains request payloads).
- [ ] **Langfuse** — restrict to props, OpenClaw telemetry, Authentik proxy.
- [ ] **Headlamp** — restrict to Authentik proxy only (K8s admin UI).

**Priority 3 — Remaining services** (lower risk, still should be locked down):

- [ ] **Headscale** — restrict API to Authentik proxy, gateway.
- [ ] **Cert-Manager** — restrict metrics endpoint to monitoring namespace.
- [ ] **Metrics Server** — restrict to Prometheus, HPA controllers.
- [ ] **ESO webhook** — restrict to kube-system, flux-system.
- [ ] **OpenClaw sandbox egress** — expand existing policies to block sandbox access to
      Vault, Sealed Secrets, Gitea API, Authentik API.
- [ ] **Props** — restrict to OpenClaw, Authentik proxy.
- [ ] **Nix cache** — restrict to build clients, Authentik proxy.

### TODO: Pod Security Standards

Apply `restricted` PSS labels to application namespaces. System namespaces (`kube-system`,
`csi-proxmox`, `cilium`) keep `privileged`. Start with `warn` mode, promote to `enforce`.

### TODO: ResourceQuota + LimitRange per Namespace

Prevent resource contention. Set default CPU/memory requests+limits via LimitRange.
Set namespace-level quotas via ResourceQuota.

### Vertical Pod Autoscaler (VPA) — Deployed (Recommendation-Only)

VPA deployed in `Off` (recommendation-only) mode via Fairwinds Helm chart (`k8s/vpa/`).
Monitors actual resource usage and generates right-sized request/limit recommendations.
Replaces manual `kubectl top` analysis (see <operations/2026-02-22-memory-request-rightsizing.md>).

- [ ] **VPA Auto Mode** — Once recommendations are validated, enable `Auto` mode
      (updater + admission controller). Start with non-critical workloads.

### TODO: Alertmanager → ntfy Bridge

Deploy [alertmanager-ntfy](https://github.com/alexbakker/alertmanager-ntfy) for structured
alert routing with severity levels, action buttons (create silence, open Prometheus URL).

### TODO: SLO Definitions

Deploy Pyrra or Sloth for declarative SLO management. Start with: ingress availability
(99.5%), DNS availability (99.9%), Vault availability (99.9%). Auto-generates multi-window,
multi-burn-rate alerts (Google SRE methodology).

## 🔀 Future Directions

### GPU Worker Node ✅

`talos-pve-gpu-worker-0`: 2x RTX 5090, 8 cores, 32GB fixed RAM, Ollama at `ollama.allegedly.works`.
TODO: Revisit virtio-mem when Proxmox adds support (Bugzilla #2949).

### TODO: Dynamic Resource Allocation (DRA) for GPU

DRA is the successor to device plugins for exposing GPUs and other hardware to pods —
more flexible, standardized allocation vs. the current NVIDIA device plugin approach.
Still beta; check NVIDIA's DRA driver guidance before enabling.
See <https://docs.siderolabs.com/kubernetes-guides/advanced-guides/dynamic-resource-allocation>

### TODO: KubeRay for distributed ML

KubeRay operator deploys Ray clusters on K8s for distributed ML workloads on the GPU node.
See <https://docs.siderolabs.com/kubernetes-guides/advanced-guides/kuberay>

### TODO: Kueue for job quota management

Kueue is a K8s-native quota and job queuing system — useful for managing GPU workload scheduling.
See <https://docs.siderolabs.com/kubernetes-guides/advanced-guides/kueue>

### TODO: Talos API access from Kubernetes

`kubernetesTalosAPIAccess` feature lets pods call the Talos API directly, scoped by namespace
and Talos role. Useful for granting agentydragon's tooling read/admin access to node-level
Talos operations without leaving the cluster.
See <https://docs.siderolabs.com/kubernetes-guides/advanced-guides/talos-api-access-from-k8s>

### BuildBuddy Remote Executor

Deployed on Proxmox, currently scaled to 0 replicas. Re-enable by setting `replicaCount > 0`
in the HelmRelease when remote execution is needed.

### Service Mesh (Future)

If Cilium mutual auth proves insufficient (need L7 policies, traffic splitting, retries,
circuit breakers), consider a full service mesh. Options:

- **Cilium Service Mesh** — native integration, no sidecars (eBPF-based L7 proxy). Natural
  evolution from current Cilium setup. Still maturing.
- **Istio ambient mode** — ztunnel (per-node L4) + waypoint proxies (per-service L7).
  No sidecars. Most mature option but heavier footprint.

Not needed while Cilium mutual auth + Gateway API cover the use cases.

### TODO: Database HA (Galera / CNPG Multi-primary)

Current CloudNativePG clusters are 2-instance primary+standby (not active-active).
For stronger HA, consider: Galera for MariaDB workloads, or CNPG with regional standby
clusters. Low priority — current setup survives single-node failure via automatic failover.

## ActivityWatch Setup

Personal activity tracking via [aw-server-rust](https://github.com/ActivityWatch/aw-server-rust).
Cluster-internal only (no public exposure). Access via `kubectl port-forward` or Headscale.

### Architecture

- **Server**: `aw-server-rust` in `activitywatch` namespace on Proxmox node
- **Storage**: SQLite on `proxmox-csi-retain` (1Gi PVC)
- **Image**: Custom build at `registry.allegedly.works/activitywatch/aw-server`
  (`docker/activitywatch/Dockerfile`), CI at `.github/workflows/activitywatch-image.yml`
- **Auth**: No built-in auth — transport-level auth via kubectl/Headscale
- **Port**: 5600 (REST API + web UI)

### Desktop Client Setup

Watchers (`aw-watcher-window`, `aw-watcher-afk`) run locally and send heartbeats to the
cluster server via `kubectl port-forward`.

1. **Install ActivityWatch** on desktop (watchers + `aw-server-rust` for local fallback)

2. **Set up persistent port-forward** (systemd user service):

   ```ini
   # ~/.config/systemd/user/aw-port-forward.service
   [Unit]
   Description=ActivityWatch kubectl port-forward
   After=network-online.target

   [Service]
   ExecStart=/usr/bin/kubectl port-forward svc/activitywatch 5600:5600 -n activitywatch
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=default.target
   ```

   ```bash
   systemctl --user enable --now aw-port-forward
   ```

3. **Configure watchers** — no changes needed, default `aw-client.toml` uses
   `hostname = "127.0.0.1"`, `port = "5600"` which hits the port-forward

4. **Stop local aw-server** — watchers talk directly to the cluster server via the tunnel.
   Keep local `aw-server` installed as fallback if the tunnel is down.

5. **Verify**: Browse `http://localhost:5600` → ActivityWatch web UI with bucket data

### Headscale Access (Future)

Once Headscale is tested with real devices:

1. Expose `activitywatch.activitywatch.svc` via Headscale subnet route
2. Configure `aw-client.toml` on each machine:

   ```toml
   [server]
   hostname = "<headscale-ip-of-service>"
   port = "5600"
   ```

3. Server must bind to `0.0.0.0` (already configured) which disables the Host header
   validation check in aw-server-rust

### Android

`aw-android` embeds its own `aw-server-rust` and cannot point at a remote server.
No sync mechanism exists. Options when upstream support improves:

- **`aw-sync`**: File-based sync via Syncthing — not yet supported on Android
- **Custom exporter**: Termux script reading from `localhost:5600` on phone and POSTing
  to the cluster server's REST API
- **Accept the gap**: Desktop data (where most work happens) is centralized; phone data
  stays local

### Public Access (Future, Optional)

If browser access from anywhere is needed, add Authentik proxy outpost following the
Grocy pattern (no app changes required):

1. Add proxy provider blueprint to `k8s/authentik/blueprints/`
2. Add provider to shared proxy outpost
3. Add HTTPRoute to `k8s/authentik-proxy-routes/`
4. Add `networkpolicy.yaml` restricting backend ingress to the outpost pod
   (see `cluster/AGENTS.md` SSO Integration for the template)
5. Watchers would still need `kubectl port-forward` or Headscale (no native auth support)

## 📋 Future Services (Lower Priority)

- [ ] Jellyfin (media streaming)
- [ ] \*arr stack (media automation)
- [ ] Paperless-ngx (document management)
- [ ] Syncthing (file sync)
- [ ] Bazel Remote Cache
- [ ] Capacitor / Weave GitOps (Flux dependency DAG visualization)
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

### Storage Strategy: Consolidated VPS, Liberal Home

**Decision**: Minimize Hetzner volumes, consolidate databases; generous allocations on Proxmox

| Location | Services                                                       | Rationale                            |
| -------- | -------------------------------------------------------------- | ------------------------------------ |
| VPS      | Vault, Authentik, Gateway, DNS, cert-mgr (all on `local-path`) | Always-on, critical path (invariant) |
| Home     | Harbor, Gitea, Loki, Grafana, media, Nix cache                 | Storage-heavy, can tolerate downtime |

Authentik, PowerDNS, and Headscale each have dedicated 2-instance CloudNativePG clusters
on `local-path`, pinned to VPS nodes. Individual CNPG clusters preferred over a single
shared PostgreSQL for fault isolation.

## 🔗 Related Documentation

- **Bootstrap Procedures**: <bootstrap.md>
- **Troubleshooting**: <troubleshooting.md>
- **Changelog**: <changelog.md>
- **Secret Sync Analysis**: <lessons_learned/2025-11-28-eso-password-generator-desync.md>

## 📊 Cluster Specifications

**Monthly Cost**: ~€30 (2x CPX31 + backups) — CPX41 upgrade planned (see `docs/plans/2026-02-22-vps-cpx41-upgrade.md`)
