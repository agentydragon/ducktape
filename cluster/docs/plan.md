# Cluster Roadmap

**Status**: 5 nodes (2 VPS + 1 Proxmox CP + 1 GPU worker + 1 roaming laptop).
Cilium Gateway API, DNS automation, Authentik SSO. PowerDNS and Authentik on
CloudNativePG `local-path`.

## Dropped Services

- **Capacitor** (2026-03-22): Removed. `ghcr.io/gimlet-io/capacitor-next` requires a
  proprietary license key despite the Apache 2.0 source license — the license check is
  injected in Gimlet's private build pipeline, not in the published source. Weave GitOps
  (the main alternative with dep graph visualization) has had no stable release since
  2023-12-06. Use Headlamp + `flux` CLI instead.

## Suspended Kustomizations

- **Kagent**: `kagent`, `kagent-namespace`, `kagent-secrets`
- **Gitea**: `gitea`, `gitea-namespace`, `gitea-secrets`, `gitea-admin-token`,
  `gitea-servicemonitor`, `authentik-blueprint-gitea-secret` — VPS memory pressure
  (2026-03-17 OOM). Re-enable after rebalancing.
- **BuildBuddy Executor**: `buildbuddy-executor` — scaled to 0. Re-enable when needed.
- **InvenTree**: `inventree`, `inventree-namespace`, `inventree-secrets`,
  `inventree-token-provisioner`, `authentik-blueprint-inventree-secret` — VPS memory pressure.
- **Firecrawl**: `firecrawl`, `firecrawl-namespace`, `firecrawl-db`
- **Langfuse**: `langfuse`, `langfuse-secrets`, `langfuse-db` —
  suspended 2026-03-31, degraded Longhorn volumes on wyrm2. Namespace kept active
  for `claude-rbac` RoleBinding dependency.
- **ActivityWatch**: `activitywatch` — suspended 2026-04-06.
- **Airlock**: `airlock` — unsuspended 2026-04-12 (FastAPI bug fixed, new image deployed). `google-workspace-mcp` depends on this.
- **ARC**: `arc`, `arc-namespace` — suspended 2026-04-11, resources deleted. Secrets
  (`arc-secrets`) are deployed. GitHub runner pod/statefulset removed.
- **Props**: `props` — suspended 2026-04-06. Secrets (`props-secrets`) are deployed.
- **Scanner**: `scanner` — suspended.

## Next Actions

- [ ] Fast token rotation on cluster reprovision: `claude-token-rotation` CronJob runs
      biweekly (1st/15th), so after a cluster rebuild the SOPS-encrypted token in
      `secrets/claude-web-k8s-token.yaml` is stale until the next scheduled run. Need a
      mechanism to rotate immediately on reprovision — e.g., trigger the job from
      bootstrap, or add a post-bootstrap hook. Also consider whether the bootstrap script
      itself should decrypt the new token and inject it into the local environment so
      Claude Code sessions work immediately without waiting for the git push + re-source
      cycle.
- [ ] Auto-derive `nebula-mesh.json` from Hetzner: the static_host_map must match
      current VPS IPs, but Hetzner assigns IPs at server creation time and they can
      change across teardown/rebuild. Currently manual — tofu should write it, or
      bootstrap should update it from `hcloud server list` output.
- [ ] Restore docker-ci Gateway routing: docker-ci needs TLS passthrough on port 2376.
      Previously used a dedicated `docker-ci-tls` Gateway listener + TLSRoute, but Cilium
      bug [#42159](https://github.com/cilium/cilium/issues/42159) caused that listener's
      `allowedRoutes.kinds: [TLSRoute]` to bleed into all listeners, blocking all HTTPRoutes.
      Options: (a) move docker-ci to HTTPS on port 443 with a subdomain, (b) separate Gateway
      resource for docker-ci only, (c) wait for Cilium fix and re-add the listener with
      `sectionName` on all HTTPRoute parentRefs. Also consider auto-discovering VPS IPs for
      `CiliumLoadBalancerIPPool` from Hetzner nodes / nodes with external IPs.
- [ ] Cilium Gateway API `Programmed=False` (#42786): hostNetwork mode leaves the Gateway
      status as `Programmed=False` / `AddressNotAssigned`. Verify whether this is cosmetic
      (routes still work) or blocks CEC programming. If blocking, try `spec.addresses` with
      static VPS IPs, or track the upstream fix.
- [ ] Decouple wyrm2 from tofu: `module.wyrm2` in the same TF root as the cluster means
      any `tofu apply` risks rebooting wyrm2 (the machine running tofu). The `--exclude`
      flag is a workaround but error-prone. Options: separate TF root for wyrm2, or manage
      wyrm2 VM config purely via NixOS/Proxmox API (no terraform). See postmortem
      `cluster/docs/lessons_learned/2026_04_01_cluster_nuke_postmortem.md`.
- [ ] Move Flux to Talos inline/extra manifest (CCM already done via `talos-ccm.tf`).
      Cilium can't be inlined — its rendered manifest (~82KB) exceeds Hetzner's 32KB
      `user_data` limit. Cilium stays as `null_resource.cilium_bootstrap` (helm CLI).
      Gateway API CRDs could move to `extraManifests` (URL fetch) but currently also
      use `null_resource` for consistency with Cilium.
- [ ] Consolidate tofu plan prerequisites — credentials are now SOPS-managed
      (`shared/cluster-tokens.yaml`, `credentials.sops.yaml`) and auto-decrypted by
      `cluster/.envrc`. Remaining gaps: - `kubeconfig`: written to `terraform/main/kubeconfig` only after
      `tofu apply`; must be manually copied before the kubernetes provider
      can plan - `talosconfig.yml`: similarly written by `tofu apply`; empty stub in
      checkout, real copy lives in `terraform/main/` on wyrm2 after bootstrap - `PG_CONN_STR`: password from SOPS, but ClusterIP lookup needs `kubectl`
      (falls back to `localhost:15432` port-forward)

- [ ] Authentik blueprint secret rotation: Authentik blueprints use file content hash to
      decide whether to re-apply. `!Env` tags are resolved _after_ the hash check, so rotating
      secrets in Vault (via Terraform `sso-secrets/`) updates the K8s secret and pod env vars,
      but Authentik's DB is never updated because the blueprint YAML didn't change. Need a
      mechanism to force blueprint re-application when `authentik-sso-client-secrets` changes
      (e.g., Stakater Reloader on Authentik worker to restart pods + a post-start hook that
      clears `last_applied_hash`, or a sidecar/CronJob that calls the Authentik API to force
      re-apply). Until fixed, any secret rotation breaks all SSO logins cluster-wide.
- [ ] Resync SSO client secrets: Authentik DB has stale secrets for all OAuth2 providers
      (Gitea, Grafana, Gatus, Harbor, Headlamp, InvenTree, Matrix, etc.) after TF state
      loss caused `sso-secrets` to regenerate all `random_password` resources. Fix by
      force re-applying all SSO blueprints (clear `last_applied_hash` via `ak shell`) so
      the DB picks up the current env var values.
- [ ] Consider OpenEBS LVM on Talos nodes; Talos has `machine.disks` or `machine.volumes`.
      Would allow firecracker fast-clone on Hetzner.
- [ ] Longhorn node tags: Kyverno mutate policy sets `node.longhorn.io/default-node-tags`
      annotation, but only fires at Node admission time — nodes created before Kyverno is
      deployed (i.e., bootstrap) never get tagged. Currently patched manually. Options:
      patch in `bootstrap.py` after Longhorn is up, or use a Longhorn `NodeLabel` feature
      if one is added upstream. Affects `hetzner-longhorn` and `proxmox-longhorn` SCs.
- [ ] Enable systemd watchdog for kubelet on NixOS workers (`WatchdogSec=` in kubelet
      service unit) — restarts kubelet if it deadlocks
- [ ] NVIDIA GPU monitoring: add DCGM exporter ServiceMonitor + Grafana dashboard (gnetId 12239)
- [ ] etcd: add dedicated ServiceMonitor for full etcd metrics (current scrape is partial via apiserver, now via Alloy)
- [ ] **Roaming node DaemonSet problem** (high priority): Offline roaming nodes
      (iguana/rugged) cause DaemonSet pods to stay Pending, which makes Helm
      install/upgrade timeout. Current workaround is `disableWait: true` in
      monitoring-stack — this is unsatisfying because it suppresses all readiness
      checking, not just for roaming nodes. Affects every HelmRelease with a
      DaemonSet. Need a proper solution:
  - Option A: Taint roaming nodes + add tolerations to DaemonSets that should
    run there (node-exporter, Cilium). DaemonSets without toleration skip roaming.
  - Option B: `nodeAffinity` on DaemonSets to exclude `roaming` region entirely.
  - Option C: Custom Flux health check that ignores Pending pods on NotReady nodes.
  - `rugged` already has `NoSchedule` taint; `iguana` does not — add it.
- [ ] Enable roaming-tolerant workloads on rugged (`grocy`, `scanner`, `activitywatch`,
      `proxmox-proxy`, `props`/`props-registry`)
- [ ] OpenClaw: obfuscation detection forces approval despite `security: full`
      (upstream `0e28e50b4`, PR #24287)
- [ ] OpenClaw: fix Ollama model discovery timeout on startup (Nebula not ready)
- [ ] OpenClaw: eliminate custom image (`ghcr.io/agentydragon/openclaw-matrix`).
      Three steps: (1) publish the airlock plugin as an npm package and install via
      `spec.plugins`, (2) move the `airlock-auth-proxy` sidecar to a standalone
      Deployment+Service in `openclaw-gateway` namespace (CNP-gated to openclaw pod
      only — preserves OAuth2 identity, no security loss), (3) point the plugin config
      at the new service URL instead of `127.0.0.1:8767`. This decouples the proxy
      lifecycle from the StatefulSet (no OpenClaw restart for proxy image updates) and
      lets the instance use the stock upstream image.
- [ ] OpenClaw: StatefulSet RollingUpdate won't replace crash-looping pods. The K8s
      StatefulSet controller counts a crash-looping pod as unavailable, and with default
      `maxUnavailable: 1` the budget is already spent, so it refuses to delete the pod
      for update. This means image updates (e.g. from Flux image automation) are never
      rolled out while the pod is unhealthy. The openclaw-operator (v0.11.1, checked
      through v0.26.2) doesn't set `MaxUnavailable` and exposes no CRD knob for it.
      Workaround: `kubectl delete pod openclaw-0 -n openclaw-gateway`.
      Fix: upstream the operator to set `maxUnavailable: "100%"` for single-replica
      StatefulSets, or add a `spec.updateStrategy` passthrough field.
- [ ] OpenClaw: eliminate one-time token entry
- [ ] Cilium Gateway `Programmed: False` (upstream bug `cilium/cilium#42786`):
      hostNetwork gateways lost address assignment in v1.18.3 refactor.
      Workaround: wildcard/apex ClusterRRsets in `k8s/powerdns/zones/`.
      Remove workaround when Cilium ships a fix and we upgrade past it.
- [ ] File upstream: powerdns-operator "stuck Failed" bug — once a ClusterRRset
      reaches Failed, it never retries unless spec changes. Should retry with backoff.
      See <lessons_learned/2026_04_07_powerdns_operator_stuck_failed_rrsets.md>.
- [ ] Plaid integration: fix onboarding (`link/token/create` returns 400)
- [ ] Tandoor: verify deployment works end-to-end (DB migration, Authentik
      proxy auth, recipe import)
- [ ] Move more PVCs to `local-path` (Proxmox CSI 29 LUN limit). Candidates:
      `langfuse/langfuse-s3`.
- Loki + MinIO:
  - [ ] Reconsider MinIO on control-plane nodes: currently 4 replicas across all
        VPS nodes (2 CP + 2 worker) with CP tolerations. Alternative: 2 replicas on
        workers only with `drivesPerNode: 2` (4 drives total, still erasure coded).
        Keeps CP nodes lighter but halves node-loss tolerance (1 node = all data).
  - [ ] **Phase 3** (future): Deploy a second MinIO instance on Proxmox (`local-path`,
        single-node). Set up async replication or scheduled `mc mirror` from the
        Hetzner instance for off-site backup of log history.
  - [ ] **Phase 4** (future): Split Loki write path by region — Proxmox node logs
        write to Proxmox MinIO, Hetzner node logs write to Hetzner MinIO. Avoids
        cross-site traffic for log ingestion. Grafana queries both.
- [ ] Re-enable MFA (TOTP/WebAuthn) once device enrollment is set up
- [ ] Wire `scripts/check-authentik-login.py` into bootstrap/CI
- [ ] Gatus: Harbor robot token for authenticated `/v2/` probe
- [ ] Proxy outpost HA: shared session storage (1 replica limit, sessions in `/dev/shm`)
- [ ] Airlock OAuth: upgrade Google scopes (needs approval flow) — `calendar`, `gmail.send`,
      `gmail.compose`, `drive`, `spreadsheets`
- [ ] Airlock OAuth: add readonly scopes — Photos, Tasks, Slides, Forms
- [ ] Airlock OAuth: reflect only access tokens (not refresh tokens) to consumer namespaces
- [ ] Proxmox OIDC auth via Authentik (native OIDC realm in PVE)
- [ ] Proxmox SPICE proxy routing via cluster ingress
- [ ] Proxmox: switch to direct HTTPRoute after OIDC auth (remove proxy outpost wrapper)
- [ ] File Browser (scanner): switch to native OAuth2/OIDC
- [ ] Ollama: per-user auth (Authentik JWTs or LiteLLM proxy)
- [ ] LiteLLM: `ollama/` provider drops `tool_calls` — use `openai-chat` variants for now
- [ ] Harbor terraform: switch to robot accounts
- [ ] Harbor CI robot: scope per-namespace pull secrets to read-only per project
- [ ] Consider removing `gitea-admin-token` Job (SSO moved to blueprints)
- [ ] Harbor proxy cache: add GHCR credentials for private repos (403 on `openclaw/openclaw`)
- [ ] Verify ntfy.sh notifications
- [ ] ActivityWatch: Gatus health check (`activitywatch-readonly:5600/api/0/info`)
- [ ] ActivityWatch: public access via Authentik proxy outpost

## Production Cutover (`agentydragon.com`)

- [ ] Website hosted in cluster, verify accessible
- [ ] Update `agentydragon.com` DNS to point to cluster
- [ ] Decommission ansible-managed VPS:
  - [ ] Clean up remaining: stale nginx sites, stale home dirs, `/tmp` tarball
  - [ ] Verify no remaining services depend on old VPS
  - [ ] Update DNS records, tear down VPS

### File Sync (Syncthing Replacement)

See <plans/file_sync_evaluation.md>.

## VPS-Only Resilience Invariants

**Rule**: These services MUST work with VPS only (Proxmox completely down). No
`proxmox-csi-retain` storage or Proxmox-pinned workloads.

| Service   | Status | Storage              | Notes                                                         |
| --------- | ------ | -------------------- | ------------------------------------------------------------- |
| DNS       | OK     | `local-path`         | CNPG 2-instance on VPS                                        |
| Website   | OK     | None (stateless)     |                                                               |
| Ingress   | OK     | None (hostNetwork)   | Cilium Gateway on VPS                                         |
| Authentik | OK     | `local-path`         | All components pinned                                         |
| Vault     | OK     | `local-path-hetzner` | Raft storage, VPS-pinned                                      |
| Grafana   | OK     | CNPG VPS-HA          | grafana-operator managed, JWT auth, no admin creds dependency |

**Compliance checklist** for critical-path changes:

1. No `proxmox-csi-retain` PVCs in dependency chain
2. No `topology.kubernetes.io/region: proxmox` affinity
3. Can schedule on VPS nodes
4. All upstream dependencies also pass 1-3

**Proxmox-dependent services** (tolerate downtime by design): Harbor, Gitea,
Nix cache, BuildBuddy, Ollama, InvenTree, ActivityWatch.

### Migrating off `proxmox-csi-retain` on wyrm2

**Rationale**: Proxmox CSI hotplugs SCSI disks onto the VM. The bpg/proxmox Terraform
provider treats all disks as a single TypeSet with no stable keys — it can't distinguish
Terraform-managed disks from CSI-managed ones. This forces `lifecycle { ignore_changes = [disk] }`
on the entire VM, which means Terraform can't manage _any_ wyrm2 disks (including intentional
ones like cache disks). Eliminating proxmox-csi usage on wyrm2 lets us eventually remove the
ignore rule and manage all disks declaratively.

**Migration**: Replace `proxmox-csi-retain` PVCs with `local-path-proxmox` (same failure
domain, no CSI disk hotplug). Remaining `proxmox-csi-retain` consumers on wyrm2:
`ollama/llm-models` (200Gi), `devbot-workspace` (20Gi), `devbot-config` (5Gi).

## Operational Hardening

### Secrets: Vault SSOT

Runtime secrets use Terraform → Vault → ESO. Bootstrap secrets use SOPS (age-encrypted in git, decrypted by Flux). Zero ESO Password generators remain.
Stakater Reloader restarts pods on changes. See
<lessons_learned/2025_11_28_eso_password_generator_desync.md>.

### Kyverno GitOps Enforcement

Deployed in Audit mode (`require-gitops` ClusterPolicy, 3 replicas).

- [ ] Switch to `Enforce` after validation
- [ ] Generic operator exclusion (skip resources with `ownerReferences`)
- [ ] Image registry allowlist (`ghcr.io`, `docker.io`, `registry.allegedly.works`, `quay.io`)

### Cilium Mutual Auth (SPIRE) -- Paused

SPIRE disabled — times out on Talos bootstrap. Nebula provides inter-node encryption.

- [ ] Investigate SPIRE timeout on Talos
- [ ] Once working: test-mode CiliumNetworkPolicies, then promote

### Firewall Hardening

All Hetzner rules allow `0.0.0.0/0`. Restrict K8s API (6443), Talos API (50000-50001),
etcd (2379-2380), kubelet (10250), Nebula (4242), VXLAN (8472) to admin IPs and
inter-node CIDRs. Keep 80/443/53 public.

### Kubeconfig Endpoints (Current State)

| Consumer                      | Endpoint                 | Mechanism                                             |
| ----------------------------- | ------------------------ | ----------------------------------------------------- |
| Talos nodes (kubelet)         | `localhost:7445`         | KubePrism (built-in Talos API proxy)                  |
| NixOS workers (wyrm2, rugged) | `localhost:7445`         | haproxy → all CP Nebula IPs (`10.42.0.{1,2,10}:6443`) |
| TF state / `cluster/.envrc`   | `https://<vps0-ip>:6443` | Direct VPS IP (bootstrap node)                        |
| `~/.kube/config` (wyrm2)      | `localhost:7445`         | Via local haproxy                                     |

`api.allegedly.works` exists on port 443 behind the cluster Cilium Gateway
(HTTPRoute in `k8s/kube-api-proxy/` fronts an nginx Deployment that re-encrypts
to the in-cluster `kubernetes` Service). This is what Claude Code web sandboxes
use to reach the k8s API — Anthropic's egress proxy only allows port 443
outbound, so a non-standard port would not work.

### OpenTofu State Backend

All 6 former TF roots consolidated into a single root at `cluster/terraform/main/` with
PG backend (CNPG `tofu-state-db`, schema `main`, 2 replicas on VPS `local-path`). Backup
CronJob writes `pg_dump` to `local-path-proxmox` PVC every 6 hours.

Zero `terraform_remote_state` dependencies — everything is in the same root. Persistent-auth
resources have `lifecycle { prevent_destroy = true }`. Bootstrap uses targeted applies
(`-target`) instead of separate directories. Single `proxmox` provider using
`PROXMOX_VE_API_TOKEN` env var (`root@pam`).

**Access**: From k8s workers (wyrm2, rugged), `.envrc` auto-detects ClusterIP and connects
directly — no port-forward. From non-workers, fall back to `kubectl port-forward`.

**Future**:

- [ ] Eliminate port-forward for non-workers. Cilium Gateway API does not support TCPRoute
      ([cilium#21929](https://github.com/cilium/cilium/issues/21929)). Options when available:
      TCPRoute + NodePort, or dedicated nginx TCP proxy (like `kube-api-proxy` pattern).

### GitHub Webhook Reconciliation

Flux `Receiver` at `flux-webhook.allegedly.works`. GitHub webhook registered by
`harbor-ci` Terraform (`github_repository_webhook.flux_receiver`).

### etcd Backup

Deploy [talos-backup](https://github.com/siderolabs/talos-backup) CronJob with age
encryption to S3.

### InvenTree API Token Provisioning

Via `inventree-token-provisioner` Job. TF module -> Vault -> Job execs into pod,
creates user via Django ORM -> `inventree-api-token` Secret in `openclaw-sandbox`
and `claude-sandbox`.

### CNPG Backup Strategy

Single-instance Proxmox CNPG clusters (atuin, langfuse, inventree, harbor, props) rely
on Proxmox ZFS for local reliability (checksums, snapshots). Off-site disaster recovery
needed:

- [ ] Generalize the `tofu-state` pg_dump CronJob pattern to all Proxmox CNPG clusters
      (write dumps to VPS-hosted PVC or object storage)
- [ ] Longer term: CNPG `ScheduledBackup` + Barman to S3-compatible store (MinIO on VPS
      or cloud bucket) for continuous WAL archiving and point-in-time recovery
- [ ] Verify Proxmox ZFS auto-snapshot schedule covers CNPG data directories

### Velero PVC Backup

Scheduled backups of PVCs (Harbor, Gitea, Loki, Postgres). No backup strategy currently.

### CiliumNetworkPolicy Rollout

Most services lack network policies. Goal: default-deny per namespace.

**Done**: PowerDNS API, Authentik API, Prometheus, plus all proxy-backed services.

**Priority 1 -- Secrets & DNS**:

- [ ] Vault -- restrict to ESO, tofu-controller, Vault namespace

**Priority 2 -- Application services**:

- [ ] Harbor, Ollama, Grafana, Alertmanager, Gitea, Tempo, Langfuse, Headlamp

**Priority 3 -- Remaining**:

- [ ] Cert-Manager, Metrics Server, ESO webhook, OpenClaw sandbox egress, Props, Nix cache

### Pod Security Standards

Apply `restricted` PSS labels to app namespaces. System namespaces keep `privileged`.
Start `warn`, promote to `enforce`.

### Scheduling Priorities

Motivated by 2026-03-17 OOM cascade. Deploy PriorityClasses: `system-critical`
(DNS/ingress/Authentik/Vault), `important` (Gitea/Harbor/monitoring), `batch`
(OpenClaw/props/BuildBuddy). Plus Descheduler, PDBs, ResourceQuota + LimitRange.

### VPA + Goldilocks

VPA deployed (`k8s/vpa/`). Goldilocks auto-creates VPAs cluster-wide.
Default mode "Off" (recommendation-only). Enable per namespace.

**TODO**: Require explicit `goldilocks.fairwinds.com/vpa-resource-policy` annotations
with `minAllowed` on all namespaces that use `updateMode: auto`. Without a floor,
VPA can recommend absurdly low CPU (e.g., 15m) that prevents containers from starting.
Consider a Kyverno policy to enforce this cluster-wide. See airlock namespace for
working example (JSON format required, not YAML).

### Alertmanager -> ntfy Bridge

Deploy [alertmanager-ntfy](https://github.com/alexbakker/alertmanager-ntfy).

### SLO Definitions

Deploy Pyrra or Sloth. Targets: ingress 99.5%, DNS 99.9%, Vault 99.9%.

## Future Directions

### GPU: DRA

DRA replaces device plugins for GPU exposure. Still beta.
See <https://docs.siderolabs.com/kubernetes-guides/advanced-guides/dynamic-resource-allocation>

### GPU: KubeRay, Kueue

KubeRay for distributed ML, Kueue for job quota management on GPU node.

### BuildBuddy Remote Executor

On Proxmox, scaled to 0. Set `replicaCount > 0` to re-enable.

### Shared Docker Daemon for CI Container Tests

Container integration tests spend ~46s per run loading OCI images (376MB total:
mitmproxy 254MB, two custom ~118MB images sharing a 113MB Python interpreter layer)
into Docker on disposable RBE Firecracker VMs. A persistent Docker daemon with cached
layers would make subsequent loads near-instant.

Done. `buildbuddy.yaml` deleted — all CI via GHA → `bb-remote`. Per-artifact
`github_release` macro, SOPS age key, `ci_env.sh`, RBE worker image as default
runner. See <../k8s/docker-ci/README.md>.

- [ ] Drop bazelisk wrapper in favor of `bb` CLI (embeds bazelisk + reads `BUILDBUDDY_API_KEY`)

### Self-Hosted Bazel Remote Cache

Legacy VPS cache removed. If needed again, deploy in-cluster on Proxmox storage.

### Service Mesh

Not needed while Cilium mutual auth + Gateway API suffice. Options if needed:
Cilium Service Mesh (eBPF L7, no sidecars) or Istio ambient mode.

### Database HA

CNPG is 2-instance primary+standby. Low priority -- survives single-node failure.

### Future Services

- [ ] Jellyfin, \*arr stack, Paperless-ngx, File sync, Tetragon

### Low Priority

- `BackendTLSPolicy` for internal HTTPS (blocked on [cilium#31352](https://github.com/cilium/cilium/issues/31352))
- Nix cache signing key -> GitOps Terraform (Vault SSOT)
- RWX shared storage: Longhorn NFS export (try first), NFS on Proxmox, JuiceFS, SeaweedFS

## Architecture Decisions

### CNI: Cilium with VXLAN

VXLAN tunnel mode. Hetzner VPS not on same L2; native routing fails. `MTU: 1412`
(uppercase, case-sensitive). VXLAN (50) + Nebula (38) = 88 overhead. UDP 8472 required.
See <lessons_learned/2026_02_11_cilium_mtu_cross_node_packet_loss.md>.

### Storage Strategy

Minimize Hetzner volumes; generous on Proxmox.

| Location | Services                                          | Rationale                         |
| -------- | ------------------------------------------------- | --------------------------------- |
| VPS      | Vault, Authentik, Grafana, Gateway, DNS, cert-mgr | Always-on, critical path          |
| Home     | Harbor, Gitea, Ollama, Nix cache                  | Storage-heavy, tolerates downtime |
| MinIO    | Loki, Mimir, Tempo                                | Erasure-coded object storage      |

CNPG: individual clusters per app, all on `local-path`. Two profiles: VPS-HA
(2 instances, Hetzner) and Proxmox-single (1 instance). See <cnpg_conventions.md>.

## Related Documentation

- <bootstrap.md>
- <troubleshooting.md>
- <lessons_learned/2025_11_28_eso_password_generator_desync.md>

**Monthly Cost**: ~EUR64 (4x CPX31, grandfathered at HIL).
