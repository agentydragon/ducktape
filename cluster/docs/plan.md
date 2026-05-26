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

- **Gitea**: `gitea-{namespace,secrets,db,admin-token,servicemonitor}` — re-suspended
  2026-05-09 for wyrm2 relocation. Resources and CNPG database deleted.
  Re-enable once hardware is back up.
- **BuildBuddy Executor**: `buildbuddy-executor` — scaled to 0. Re-enable when needed.
- **InvenTree**: `inventree-{namespace,secrets,token-provisioner}`,
  `authentik-blueprint-inventree-secret` — VPS memory pressure.
- **Firecrawl**: `firecrawl-{namespace,db}`
- **Langfuse**: `langfuse-{secrets,db}` — suspended 2026-03-31, degraded Longhorn
  volumes on wyrm2. Namespace kept active for `claude-rbac` RoleBinding dependency.
- **ActivityWatch**: `activitywatch` — suspended 2026-04-06.
- **Airlock**: `airlock` — unsuspended 2026-04-12 (FastAPI bug fixed, new image deployed). `google-workspace-mcp` depends on this.
- **ARC**: `arc-namespace` — suspended 2026-04-11, resources deleted. Secrets
  (`arc-secrets`) are deployed. GitHub runner pod/statefulset removed.
- **Props**: `props` — suspended 2026-04-06. Secrets (`props-secrets`) are deployed.
- **Scanner**: `scanner` — suspended.
- **kagent**: `kagent-{crds,db,secrets}` — suspended 2026-05-08. No tool-call output
  truncation; sessions die on large MCP outputs (z.ai error 1261). Namespace and
  resources deleted. See <../k8s/agents/kagent/TODO.md>.
- **SDR**: `sdr` — suspended 2026-05-09. Temporarily disabled until the radio
  hardware is set up again after relocation to new place.
- **Mimir + Tempo**: `mimir`, `tempo` — suspended 2026-05-13. VPS worker-0 OOM
  cascade (6072 Mi requested / 7246 Mi allocatable). Unsuspend once there's
  capacity and we figure out proper memory sizing (VPA resource policies,
  right-sized requests, or additional node capacity).
- **Google Workspace MCP**: `google-workspace-mcp` — suspended 2026-05-13.
  Resources and PVC deleted. Unsuspend once VPS memory pressure resolves.

## Next Actions

- [ ] **Replace Kimsufi worker_0** with the second new KS-5 from order #7958582
      once it's delivered. Sequence: cordon + drain `talos-kimsufi-worker-0`,
      `talosctl shutdown` the cancelled `ns103656` server, `kubectl delete node`,
      `tofu state rm` the 9 worker_0 entries, set `var.kimsufi_service_name` to
      the new server name, targeted `tofu apply`. Then update
      `nebula-mesh.json:static_host_map` with the new public IP for `10.42.0.13`.
      See <kimsufi_provisioning.md> §5 (path B already executed for worker_1 from
      the same order — `ns103711.ip-147-135-39.us` joined as
      `talos-kimsufi-worker-1` on 2026-05-15).
- [ ] **Re-key admin-only SOPS files to include user keys** — admin age key currently
      lives only on wyrm2, which is offline in a SF storage unit. `.sops.yaml` rules say
      `secrets/nebula/ca.sops.key` and `k8s/tofu-state/db/credentials.sops.yaml` (among
      others) should be encrypted to admin + 4 user keys, but the files in git still have
      only admin. Discovered 2026-05-13 trying to provision the Kimsufi node from rugged.
      Blocks: `tofu apply` of any resource needing the Nebula CA (e.g. `null_resource.nebula_node_cert`
      for the Kimsufi worker). Also blocks `tofu init` (PG backend creds in
      `tofu-state/db/credentials.sops.yaml`). - Partial workaround for PG backend: fetch creds from k8s instead of SOPS in
      `cluster/.envrc` — `kubectl -n tofu-state get secret tofu-state-db-credentials`
      works with current `claude-rbac`/user kubeconfig. - Real fix when wyrm2 is back: `sops updatekeys` on every file where `.sops.yaml`
      lists more recipients than the file actually has. Audit with
      `for f in $(git ls-files '*.sops.*'); do jq -r '.sops.age[]?.recipient' $f; done`
      and compare to expected recipients per rule.
- [ ] **Restore SDR kustomization** — unsuspend `cluster/k8s/sdr/flux-kustomization.yaml`
      (remove `suspend: true`) once the radio hardware is set up at the new place.
- [ ] **Resume CPAP sync CronJob** — unsuspend `cluster/k8s/cpap-sync/cronjob.yaml`
      (remove `suspend: true`) once wyrm2 is back online after relocation.
- [ ] **Evaluate lighter registry to replace Harbor** — Harbor is only used for (a) pull-through
      proxy cache (Docker Hub, GHCR, GCR, Quay, k8s.io) configured as Talos containerd mirrors,
      and (b) props agent image storage. Candidates: [Zot](https://zotregistry.dev/) (single binary,
      multi-upstream proxy + private images), or separate `registry:2` per upstream + GHCR for props.
      Would drop the Harbor Helm chart, CNPG cluster, 32Gi HDD PVC, ~700Mi RAM, and all
      Harbor-specific TF modules.

- [ ] **Apply kube-apiserver auth-config migration to VPS CPs** (unblocked once wyrm2 is
      back): `secrets/nebula/ca.sops.key` now has all agentydragon user keys as recipients
      but the file needs re-encryption before `tofu apply` works from any user machine.
      Steps when wyrm2 is back: 1. `sops updatekeys secrets/nebula/ca.sops.key` (uses admin key on wyrm2 to re-encrypt
      with new recipients from `.sops.yaml`) 2. Commit the re-encrypted `ca.sops.key` 3. `tofu apply -target='talos_machine_configuration_apply.vps'` from any agentydragon
      machine — applies the auth-config migration (removes old `--oidc-*` flags, adds
      `authentication-config` extraArg + `extraVolumes` + `/etc/kubernetes/auth/` file)
      to `talos-vps-cp-0` and `talos-vps-cp-1`. Apply to `talos-pve-cp-0` separately
      once atlas is back. 4. Verify kube-apiserver restarts cleanly on each CP before moving to the next.
      Note: etcd quorum is safe for rolling VPS CP updates (2/3 members, can lose 1).

- [ ] Verify dmeventd thin pool monitoring after wyrm2 reboot: NixOS config changed
      `pkgs.lvm2` → `pkgs.lvm2_dmeventd` so `lvchange --monitor y` actually registers
      with dmeventd. After next wyrm2 reboot, verify with
      `lvs -o lv_name,seg_monitor -S "lv_attr=~^t"` — Monitor column should show
      `monitored`. See `nix/nixos/hosts/wyrm2/default.nix` openebs setup services.
- [ ] Fast token rotation on cluster reprovision: `claude-token-rotation` CronJob runs
      biweekly (1st/15th), so after a cluster rebuild the SOPS-encrypted token in
      `secrets/claude-web-k8s-jwt.yaml` is stale until the next scheduled run. Need a
      mechanism to rotate immediately on reprovision — e.g., trigger the job from
      bootstrap, or add a post-bootstrap hook. Also consider whether the bootstrap script
      itself should decrypt the new token and inject it into the local environment so
      Claude Code sessions work immediately without waiting for the git push + re-source
      cycle.
- [ ] Auto-derive `nebula-mesh.json` from Hetzner: the static_host_map must match
      current VPS IPs, but Hetzner assigns IPs at server creation time and they can
      change across teardown/rebuild. Currently manual — tofu should write it, or
      bootstrap should update it from `hcloud server list` output.
- [ ] Require every workload to declare Stakater Reloader explicitly as enabled or
      intentionally disabled. No implicit default. Enforce via review/docs and
      add missing `reloader.stakater.com/auto: "true"` or an explicit opt-out
      comment/setting on existing Deployments, StatefulSets, and Helm releases.
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

- [ ] Authentik blueprint secret rotation: blueprints use file content hash to decide
      whether to re-apply, and `!Env` tags resolve _after_ the hash check, so rotating a
      secret consumed via `!Env` updates the K8s secret + pod env vars but Authentik's DB
      never picks it up. Mostly obsolete now that SSO client secrets live in
      `tf/gitops/sso-providers/` (TF writes the provider directly), but any remaining
      `!Env`-tagged blueprint values would hit this. Force re-application via Stakater
      Reloader + a post-start hook clearing `last_applied_hash`, or a CronJob calling the
      Authentik API.
- [ ] Authentik cache/channels offload: design a Redis-based replacement for the current
      Postgres-backed cache/channels path (`django_postgres_cache`,
      `django_channels_postgres`) without regressing single-node-failure resilience on the
      Hetzner side. Decide whether this needs HA Redis/Sentinel, an acceptable degraded mode,
      or a different architecture before wiring Authentik to use it. Re-measure steady-state
      Postgres connections afterward before deciding whether any DB limit or
      worker-concurrency changes are still needed.
- [ ] Investigate why `proxmox-csi-retain` publishes zero `CSIStorageCapacity` objects —
      the scheduler sees 0 available space and refuses to schedule `WaitForFirstConsumer`
      PVCs, creating a deadlock. Likely a Proxmox API connectivity issue in
      `proxmox-proxy`. Check `csi-proxmox` controller logs and `proxmox-proxy` logs.
- [ ] Consider OpenEBS LVM on Talos nodes; Talos has `machine.disks` or `machine.volumes`.
      Would allow firecracker fast-clone on Hetzner.
- [ ] Trial SeaweedFS with both the upstream operator and CSI driver. Deploy the
      Seaweed cluster via `seaweedfs-operator`, then install the CSI driver as a
      separate Flux `HelmRelease` pointed at the operator-created filer service.
      Keep the StorageClass non-default, test with disposable PVCs first, and
      evaluate S3/object usage separately from FUSE/PVC usage. Do not use it for
      CNPG or core infra unless the trial proves recovery, upgrades, and node
      restarts are boring.
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
- [ ] Delete the generic `local-path` StorageClass (not region-pinned; superseded by
      `local-path-hetzner` and `local-path-proxmox`).
- [ ] Fix Goldilocks VPA over-requesting memory on VPS pods. VPA `updateMode: Auto`
      mutates pod requests on creation but old pods retain stale high values until
      restarted. This caused grocy-sf to fail scheduling (97% memory requested on VPS
      workers despite 65-78% actual usage). **Root cause**: Goldilocks auto-creates VPAs
      with no resource policy, and VPA recommendations accumulate historical peaks.
      **Proposed solution**: (1) Add `goldilocks.fairwinds.com/vpa-resource-policy`
      annotations to all namespaces with memory min/max bounds (enforce via Kyverno).
      (2) Switch VPA `updateMode` from `Auto` to `Initial` for non-critical workloads
      so stale requests don't block scheduling — let pod restarts pick up new values
      naturally. (3) Consider running the Kubernetes Descheduler (`k8s-sigs/descheduler`)
      with `RemoveDuplicates` and `LowNodeUtilization` strategies to spread pods across
      VPS workers and evict those with grossly inflated requests. Descheduler is the
      right tool for _rebalancing_ after VPA shrinks requests, but the root fix is
      bounding VPA recommendations via resource policies so they never balloon in the
      first place.
- Loki + SeaweedFS S3 (MinIO retired 2026-05; Loki/Tempo/Mimir all point at the
  SeaweedFS S3 gateway on OVH):
  - [ ] **Phase 3** (future): Off-site backup of log history — replicate the
        OVH SeaweedFS buckets to a second location (Hetzner Object Storage,
        Cloudflare R2, or a second SeaweedFS instance on Proxmox) for
        disaster recovery.
  - [ ] **Phase 4** (future): Split Loki write path by region — Proxmox node logs
        write to a Proxmox-local object store, Hetzner node logs write to the
        OVH SeaweedFS. Avoids cross-site traffic for log ingestion.
        Grafana queries both.
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

| Service   | Status | Storage            | Notes                                                         |
| --------- | ------ | ------------------ | ------------------------------------------------------------- |
| DNS       | OK     | `local-path`       | CNPG 2-instance on VPS                                        |
| Website   | OK     | None (stateless)   |                                                               |
| Ingress   | OK     | None (hostNetwork) | Cilium Gateway on VPS                                         |
| Authentik | OK     | `local-path`       | All components pinned                                         |
| Grafana   | OK     | CNPG OVH-HA        | grafana-operator managed, JWT auth, no admin creds dependency |

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

### Secrets: SOPS SSOT

All secrets are SOPS (age-encrypted in git, decrypted by Flux). ESO is still installed
but only with the Kubernetes provider, mirroring a small number of secrets cross-namespace
(authentik, openclaw-gateway/sandbox, openhands). Stakater Reloader restarts pods on
changes. Vault was decommissioned 2026-04-19 — see <../vault-migration/TODO.md>.

### Google OAuth Client redirect URIs (blocked upstream)

The Google Cloud OAuth client backing Authentik's "Sign in with Google"
source (client_id `230253529789-…`, referenced from
`tf/gitops/sso-providers/source_google.tf`) is hand-managed in the
GCP Console. Every new forward-auth app needs its callback URI
(`https://<app>.allegedly.works/source/oauth/callback/google/`)
appended to the client's Authorized redirect URIs by hand — a
`redirect_uri_mismatch` is the symptom on first sign-in. The
behaviour itself is intentional in Authentik
(<https://github.com/goauthentik/authentik/issues/19883> closed as
not-planned); standalone proxy outposts run flows on the proxied
domain. Domain-Level forward-auth would centralise the callback but
sacrifices per-app group restrictions
(<https://docs.goauthentik.io/add-secure-apps/providers/proxy/forward_auth/>),
which is the entire reason for using forward-auth here.

**Why this isn't behind GitOps yet:** Web Application OAuth 2.0
Client IDs in GCP have no public CRUD API
(<https://issuetracker.google.com/issues/116182848>, filed 2018),
hence no Terraform resource
(<https://github.com/hashicorp/terraform-provider-google/issues/6074>).
`google_iap_client` is IAP-only; `google_iam_oauth_client` is
Workforce Identity Federation-only — neither covers the type of
client in use. Migrating to one of those products to unblock GitOps
is a much larger lift than the manual click.

Hit list when this changes:

- [ ] Watch <https://issuetracker.google.com/issues/116182848> for a
      public OAuth-client management API. When it lands and the
      Terraform provider gains a resource, move the client and its
      redirect URIs to TF.
- [ ] Until then: record per-app callback URI additions in the app's
      commit message so future audits can rebuild the list from git.

### Kyverno GitOps Enforcement

Deployed in Audit mode (`require-gitops` ClusterPolicy, 3 replicas).

- [ ] Switch to `Enforce` after validation
- [ ] Generic operator exclusion (skip resources with `ownerReferences`)
- [ ] Image registry allowlist (`ghcr.io`, `docker.io`, `registry.allegedly.works`, `quay.io`)

### Flux HelmRelease Drift Detection

By default Flux's helm-controller only runs `helm upgrade` when an HR's spec
changes, not on the periodic interval. A hand-edit like
`kubectl scale deployment ... --replicas=0` is invisible to it — the Helm
release is still recorded as "deployed" with the right values, the deployment
object still exists, so nothing looks drifted. Hit on 2026-05-24 when
`grafana-operator` had been hand-scaled to 0 for ~10 days without Flux
restoring it; grafana-deployment then sat at 0/0 because the operator
wasn't reconciling.

Per-HR opt-in fix:

```yaml
spec:
  driftDetection:
    mode: enabled
```

- [ ] Enable on `grafana-operator` (low-risk; nothing else writes that
      Deployment)
- [ ] Audit which HRs are safe to enable wider — anything where another
      controller legitimately mutates Helm-managed resources (HPA scaling,
      sidecar injectors, security defaulters, VPA on `Auto` mode) will
      fight drift correction and should stay off.

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
(TLSRoute in `k8s/kube-api-proxy/` with TLS passthrough to the `kubernetes`
Service). This is what Claude Code web sandboxes use to reach the k8s API —
Anthropic's egress proxy only allows port 443 outbound, so a non-standard port
would not work. TLS passthrough preserves client certificates for x509 auth.

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
      TCPRoute + NodePort, or TLS passthrough (like `kube-api-proxy` TLSRoute pattern).
- [x] **Migrate tofu-controller `Terraform` CRs from `kubernetes` to `postgres` backend.**
      16 of ~19 CRs migrated (2026-05-15). PG backend uses session-based advisory locks
      that auto-release on runner pod death, eliminating the stale-Lease problem. Each CR
      gets its own schema in the existing `tofu-state-db` CNPG cluster. Reflector mirrors
      PG credentials from `tofu-state` → `flux-system` namespace.
      **Remaining**: - `github-secrets-sync` — Flux kustomization blocked by dependency chain
      (`claude-rbac` → missing `openclaw-gateway`/`docker-ci` namespaces). Will
      self-migrate once chain unblocks. CR already has PG backend in git. - `harbor-oidc-config` — already switched in git, will migrate when Harbor comes
      back up. - `dns-records` — import blocks committed, awaiting Flux reconciliation. Remove
      import blocks from `tf/gitops/dns-records/main.tf` after successful apply.
      **Cleanup** (after all CRs migrated): delete old `tfstate-default-*` and
      `tfplan-default-*` secrets in `flux-system`, remove import blocks from TF sources,
      simplify stale-lock section in `troubleshooting.md`.

### GitHub Webhook Reconciliation

Flux `Receiver` at `flux-webhook.allegedly.works`. GitHub webhook registered by
`harbor-ci` Terraform (`github_repository_webhook.flux_receiver`).

### SOPS-encrypt talosconfig and kubeconfig

- [ ] Store `terraform/main/talosconfig.yml` and `terraform/main/kubeconfig` in SOPS
      instead of plaintext local files. These contain cluster admin credentials and
      should not be unencrypted on disk.

### etcd Backup

Deploy [talos-backup](https://github.com/siderolabs/talos-backup) CronJob with age
encryption to S3.

### InvenTree API Token Provisioning

Via `inventree-token-provisioner` Job. SOPS-managed sandbox-agent password ->
Job execs into pod, creates user via Django ORM -> `inventree-api-token` Secret
in `openclaw-sandbox` and `claude-sandbox`.

### CNPG Backup Strategy

Single-instance Proxmox CNPG clusters (atuin, langfuse, inventree, harbor, props) rely
on Proxmox ZFS for local reliability (checksums, snapshots). Off-site disaster recovery
needed:

- [ ] Generalize the `tofu-state` pg_dump CronJob pattern to all Proxmox CNPG clusters
      (write dumps to VPS-hosted PVC or object storage)
- [ ] Longer term: CNPG `ScheduledBackup` + Barman to S3-compatible store (SeaweedFS
      S3 gateway, or an external cloud bucket) for continuous WAL archiving and
      point-in-time recovery
- [ ] Verify Proxmox ZFS auto-snapshot schedule covers CNPG data directories

### Velero PVC Backup

Scheduled backups of PVCs (Harbor, Gitea, Loki, Postgres). No backup strategy currently.

### CiliumNetworkPolicy Rollout

Most services lack network policies. Goal: default-deny per namespace.

**Done**: PowerDNS API, Authentik API, Prometheus, plus all proxy-backed services.

**Priority 2 -- Application services**:

- [ ] Harbor, Ollama, Grafana, Alertmanager, Gitea, Tempo, Langfuse, Headlamp

**Priority 3 -- Remaining**:

- [ ] Cert-Manager, Metrics Server, ESO webhook, OpenClaw sandbox egress, Props, Nix cache

### Scoped Historical Logs for `claude-sandbox`

Agents in `claude-sandbox` can't reach Loki — `loki-ingress` CNP only allows
promtail/grafana/alloy/gatus/authentik, and Loki runs `auth_enabled: false` so the
CNP is the entire access boundary. Without log access, post-mortems on dead pods
(e.g. the 2026-05-24 `tana-mcp` livenessProbe kill) are limited to metrics +
kubelet's short-lived previous-container buffer.

Options, from cheapest to cleanest:

- **A. Allowlist `claude-sandbox` on `loki-ingress` CNP.** ~5 lines. Grants full
  cluster Loki read; scoping relies on the agent querying only namespaces it has
  business in (same trust model as `namespace-diagnostics-reader`).
- **B. `loki-agent-proxy` deployment in `monitoring`.** nginx + njs/Lua that
  rewrites incoming LogQL `query` params to inject `{namespace=~"<allowlist>"}`
  before forwarding to `loki-read:3100`. CNP grants `claude-sandbox` → proxy only,
  not Loki directly. Real per-namespace scoping. Mirrors existing precedent
  (`tana-mcp` nginx whitelisting `/mcp` + `/health`, `activitywatch-readonly`
  whitelisting `GET/POST /api/0/query`). Natural allowlist: the
  `namespace-diagnostics-reader` and `logs-configmaps-reader` binding sets in
  <k8s/agents/claude-rbac/permissions.md>.
- **C. Loki multi-tenancy.** Set `auth_enabled: true`, route per-namespace logs to
  per-namespace tenants via Alloy/Promtail, grant tenant IDs. Touches every log
  producer; almost certainly overkill.

Grafana isn't a useful proxy here: OSS Grafana's Loki datasource has no
label-level access controls (TeamLBAC is Enterprise-only), so routing through
Grafana shifts the trust boundary without actually scoping access.

Recommendation: **B** when log access starts mattering for routine triage; **A**
as a stop-gap if the trust-the-agent model is acceptable.

### Pod Security Standards

Apply `restricted` PSS labels to app namespaces. System namespaces keep `privileged`.
Start `warn`, promote to `enforce`.

### Scheduling Priorities

Motivated by 2026-03-17 OOM cascade. Deploy PriorityClasses: `system-critical`
(DNS/ingress/Authentik), `important` (Gitea/Harbor/monitoring), `batch`
(OpenClaw/props/BuildBuddy). Plus Descheduler, PDBs, ResourceQuota + LimitRange.

### VPA + Goldilocks

VPA deployed (`k8s/vpa/`). Goldilocks auto-creates VPAs cluster-wide.
Default mode "Off" (recommendation-only). Enable per namespace.

**TODO**: Require explicit `goldilocks.fairwinds.com/vpa-resource-policy` annotations
on all namespaces/deployments that use `updateMode: auto`. Without a policy:

- VPA can recommend absurdly low CPU (e.g., 15m) that causes throttling.
  Consider a Kyverno policy to enforce this cluster-wide. See airlock namespace
  for working example (JSON format required, not YAML).

**CPU limits policy**: for workloads with expensive cold starts (Python/JVM),
use `"controlledValues":"RequestsOnly"` so VPA only sets CPU requests and never
adds a CPU limit. CFS CPU limits are a hard rate limiter enforced by cgroups
regardless of node load — a pod capped at 60m gets 60ms/s of CPU even on a
completely idle node. Removing the CPU limit lets cold-start bursts use idle
capacity; when the node is contended, CFS shares CPU proportionally to requests
(compressible resource — no pod is killed). Memory limits remain useful
(`RequestsAndLimits`) since memory is incompressible.

See `cluster/k8s/agents/tana-mcp-facade/deployment.yaml` for a working example
(fastmcp takes ~6 CPU-seconds to import; at 60m limit this costs 100s wall time).

### Alertmanager -> ntfy Bridge

Deploy [alertmanager-ntfy](https://github.com/alexbakker/alertmanager-ntfy).

### SLO Definitions

Deploy Pyrra or Sloth. Targets: ingress 99.5%, DNS 99.9%, Authentik 99.5%.

### GUI for Terraform/OpenTofu

Evaluate a web UI for managing cluster infrastructure via TF/OpenTofu — plan/apply from
a dashboard instead of CLI. Candidates: Spacelift (SaaS), env0, Atlantis (self-hosted,
PR-based), Terrateam, Scalr, or the OpenTofu-native options. Self-hosted preferred.

### Tekton / Argo Workflows for in-cluster CI

Evaluate Tekton Pipelines or Argo Workflows for in-cluster automations that need
to write back to git (e.g., JWT rotation, secret rotation, image pin updates).
Currently these are ad-hoc CronJobs with sparse clones and `git push`. A
lightweight workflow engine would give structured retries, DAG dependencies,
artifact passing, and a UI for debugging failed runs.

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
- Nix cache signing key -> GitOps Terraform writing a SOPS secret
- RWX shared storage: Longhorn NFS export (try first), NFS on Proxmox, JuiceFS, SeaweedFS

## Architecture Decisions

### CNI: Cilium with VXLAN

VXLAN tunnel mode. Hetzner VPS not on same L2; native routing fails. `MTU: 1412`
(uppercase, case-sensitive). VXLAN (50) + Nebula (38) = 88 overhead. UDP 8472 required.
See <lessons_learned/2026_02_11_cilium_mtu_cross_node_packet_loss.md>.

### Storage Strategy

Minimize Hetzner volumes; generous on Proxmox.

| Location | Services                                                      | Rationale                         |
| -------- | ------------------------------------------------------------- | --------------------------------- |
| VPS      | Authentik, Grafana, Gateway, DNS, cert-mgr                    | Always-on, critical path          |
| Home     | Harbor, Gitea, Ollama                                         | Storage-heavy, tolerates downtime |
| OVH      | SeaweedFS, attic-db, Nix cache chunks + Loki/Mimir/Tempo (S3) | Replicated across 2 kimsufi nodes |

CNPG: individual clusters per app. Two profiles: OVH-HA (2 instances, OVH
kimsufi) and Proxmox-single (1 instance). See <cnpg_conventions.md>.

## Related Documentation

- <bootstrap.md>
- <troubleshooting.md>
- <lessons_learned/2025_11_28_eso_password_generator_desync.md>

**Monthly Cost**: ~EUR64 (4x CPX31, grandfathered at HIL).
