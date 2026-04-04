# Cluster Roadmap

**Status**: 5 nodes (2 VPS + 1 Proxmox CP + 1 GPU worker + 1 roaming laptop).
Cilium Gateway API, DNS automation, Authentik SSO. PowerDNS and Authentik on
CloudNativePG `local-path`. See <changelog.md> for history.

## Dropped Services

- **Capacitor** (2026-03-22): Removed. `ghcr.io/gimlet-io/capacitor-next` requires a
  proprietary license key despite the Apache 2.0 source license — the license check is
  injected in Gimlet's private build pipeline, not in the published source. Weave GitOps
  (the main alternative with dep graph visualization) has had no stable release since
  2023-12-06. Use Headlamp + `flux` CLI instead.

## Suspended Kustomizations

- **Kagent**: `kagent`, `kagent-namespace`, `kagent-secrets`
- **Grocy**: `grocy`, `grocy-namespace`
- **Gitea**: `gitea`, `gitea-namespace`, `gitea-secrets`, `gitea-admin-token`,
  `gitea-servicemonitor`, `authentik-blueprint-gitea-secret` — VPS memory pressure
  (2026-03-17 OOM). Re-enable after rebalancing or CPX41 upgrade.
- **BuildBuddy Executor**: `buildbuddy-executor` — scaled to 0. Re-enable when needed.
- **InvenTree**: `inventree`, `inventree-namespace`, `inventree-secrets`,
  `inventree-token-provisioner`, `authentik-blueprint-inventree-secret` — VPS memory pressure.
- **Firecrawl**: `firecrawl`, `firecrawl-namespace`, `firecrawl-db`
- **Langfuse**: `langfuse`, `langfuse-namespace`, `langfuse-secrets`, `langfuse-db` —
  suspended 2026-03-31, degraded Longhorn volumes on wyrm2.

## Next Actions

- [x] ~~Migrate cluster secrets from SealedSecrets to SOPS~~ — done (2026-04-02).
      All 26 SealedSecret files converted to SOPS. Sealed-secrets controller removed.
      Nebula CA, Flux deploy key, and cluster age keypair moved to SOPS in `secrets/`.

- [ ] Nebula cert deployment gap: tofu generates certs to disk, but NixOS workers
      (wyrm2, rugged) read certs from sops-nix secrets. No automation connects them —
      after cert rotation, someone must manually copy the new cert content into the sops
      file and nixos-rebuild. Options: script that reads tofu output and writes sops, or
      move cert generation entirely to sops-nix (generate on the host, not in tofu).
- [ ] Decouple wyrm2 from tofu: `module.wyrm2` in the same TF root as the cluster means
      any `tofu apply` risks rebooting wyrm2 (the machine running tofu). The `--exclude`
      flag is a workaround but error-prone. Options: separate TF root for wyrm2, or manage
      wyrm2 VM config purely via NixOS/Proxmox API (no terraform). See postmortem
      `cluster/docs/lessons_learned/2026-04-01-cluster-nuke-postmortem.md`.
- [ ] Move Flux to Talos inline/extra manifest (CCM already done via `talos-ccm.tf`).
      Cilium can't be inlined — its rendered manifest (~82KB) exceeds Hetzner's 32KB
      `user_data` limit. Cilium stays as `null_resource.cilium_bootstrap` (helm CLI).
      Gateway API CRDs could move to `extraManifests` (URL fetch) but currently also
      use `null_resource` for consistency with Cilium.
- [ ] Consolidate tofu plan prerequisites — currently requires assembling credentials from
      multiple scattered sources before `tofu plan/apply` works: - `PG_CONN_STR`: read from k8s secret (`tofu-state-db-app`) via kubectl, not auto-set
      outside of cluster-networked machines - `TF_VAR_hcloud_token`: stored in system keyring on wyrm2 (`secret-tool lookup service hcloud account default`) - `PROXMOX_VE_API_TOKEN`: stored in system keyring on wyrm2 (`secret-tool lookup service proxmox ...`) - `kubeconfig`: written to `terraform/main/kubeconfig` only after `tofu apply`; must be
      manually copied or regenerated before the kubernetes provider can plan - `talosconfig.yml`: similarly written by `tofu apply`; empty stub in checkout, real copy
      lives in `terraform/main/` on wyrm2 after bootstrap
      Goal: make `direnv` in `cluster/` or a helper script reliably assemble all of these so
      `tofu plan` works from any machine with kubectl + SSH access to wyrm2.
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
- [ ] OpenEBS LVM on Talos nodes: Currently deployed on wyrm2 (NixOS) for
      Firecracker VM storage (thin provisioning, `volumeMode: Block` cloning).
      If it works well on wyrm2, expand to Talos nodes: - **VPS nodes**: Partition each 160GB NVMe to carve out ~60GB for a VG
      (`openebs-vps-vg`). Use Talos `machine.disks` or `machine.volumes`
      (1.9+). Add `lvm-vps` StorageClass. Rolling update: drain + destroy +
      recreate each VPS node. Gives VPS a local non-replicated option
      (faster than Longhorn for CNPG `local-path` replacements). - **Proxmox CP**: Similar approach if needed for local storage. - Expand OpenEBS LVM HelmRelease `lvmNode.nodeSelector` to include
      target regions. Pairs well with CPX41 upgrade (240GB → more headroom).
- [ ] Longhorn node tags: Kyverno mutate policy sets `node.longhorn.io/default-node-tags`
      annotation, but only fires at Node admission time — nodes created before Kyverno is
      deployed (i.e., bootstrap) never get tagged. Currently patched manually. Options:
      patch in `bootstrap.py` after Longhorn is up, or use a Longhorn `NodeLabel` feature
      if one is added upstream. Affects `hetzner-longhorn` and `proxmox-longhorn` SCs.
- [ ] NVIDIA GPU monitoring: add DCGM exporter ServiceMonitor + Grafana dashboard (gnetId 12239)
- [ ] etcd: add dedicated ServiceMonitor for full etcd metrics (current scrape is partial via apiserver)
- [ ] Prometheus: investigate memory growth and right-size (OOM-killed 8x at 2Gi, pinned to wyrm2 at 6Gi)
- [ ] Prometheus: unpin from wyrm2 (blocked on PriorityClasses + right-sizing + VPS headroom)
- [x] ~~`talos-pve-cp-0`: evict Longhorn storage (pure CP node, has stopped replicas)~~
- [ ] Enable roaming-tolerant workloads on rugged (`grocy`, `scanner`, `activitywatch`,
      `proxmox-proxy`, `props`/`props-registry`)
- [ ] OpenClaw: obfuscation detection forces approval despite `security: full`
      (upstream `0e28e50b4`, PR #24287)
- [ ] OpenClaw: fix Ollama model discovery timeout on startup (Nebula not ready)
- [ ] OpenClaw: eliminate one-time token entry
- [ ] Plaid integration: fix onboarding (`link/token/create` returns 400)
- [ ] Grocy: provision API token for agent access
- [ ] Move more PVCs to `local-path` (Proxmox CSI 29 LUN limit). Candidates:
      `langfuse/langfuse-s3`, `monitoring/alertmanager-*`, `monitoring/prometheus-*`,
      `monitoring/storage-tempo-0`, `monitoring/kube-prometheus-stack-grafana`,
      `loki/storage-loki-stack-0`, `harbor/harbor-jobservice`
- [ ] Re-enable MFA (TOTP/WebAuthn) once device enrollment is set up
- [ ] Wire `scripts/check-authentik-login.py` into bootstrap/CI
- [ ] Gatus: Harbor robot token for authenticated `/v2/` probe
- [x] ~~Nix cache: initialize Attic cache~~ — done (moved to wyrm2, `local-path`)
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
- [x] ~~Delete `headscale-cleanup-*` GitHub releases~~ — done
- [ ] Decommission ansible-managed VPS:
  - [x] ~~Clean up LE certs~~ — deleted unused certs, kept `adgn.link`, `agentydragon.com`, `vps.agentydragon.com`
  - [ ] Clean up remaining: stale nginx sites, stale home dirs, `/tmp` tarball
  - [ ] Verify no remaining services depend on old VPS
  - [ ] Update DNS records, tear down VPS

### File Sync (Syncthing Replacement)

See <plans/file-sync-evaluation.md>.

## VPS-Only Resilience Invariants

**Rule**: These services MUST work with VPS only (Proxmox completely down). No
`proxmox-csi-retain` storage or Proxmox-pinned workloads.

| Service   | Status | Storage            | Notes                     |
| --------- | ------ | ------------------ | ------------------------- |
| DNS       | OK     | `local-path`       | CNPG 2-instance on VPS    |
| Website   | OK     | None (stateless)   |                           |
| Ingress   | OK     | None (hostNetwork) | Cilium Gateway on VPS     |
| Authentik | OK     | `local-path`       | All components pinned     |
| Vault     | OK     | `local-path`       | Raft storage, VPS-capable |

**Compliance checklist** for critical-path changes:

1. No `proxmox-csi-retain` PVCs in dependency chain
2. No `topology.kubernetes.io/region: proxmox` affinity
3. Can schedule on VPS nodes
4. All upstream dependencies also pass 1-3

**Proxmox-dependent services** (tolerate downtime by design): Harbor, Gitea, Loki,
Nix cache, Grafana, BuildBuddy, Ollama, InvenTree, ActivityWatch.

## Operational Hardening

### Secrets: Vault SSOT

Runtime secrets use Terraform → Vault → ESO. Bootstrap secrets use SOPS (age-encrypted in git, decrypted by Flux). Zero ESO Password generators remain.
Stakater Reloader restarts pods on changes. See
<lessons_learned/2025-11-28-eso-password-generator-desync.md>.

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

`api.allegedly.works` exists (round-robin VPS IPs, `kube-api-proxy` DaemonSet on port 16443
with LE cert, managed by ClusterRRset CRDs + `k8s/kube-api-proxy/`). Bootstrappable from
cluster — DNS record is a declarative ClusterRRset, Flux deploys the proxy.

- [ ] Patch TF-generated kubeconfig post-bootstrap to use `api.allegedly.works:16443`

### OpenTofu State Backend

All 6 former TF roots consolidated into a single root at `cluster/terraform/main/` with
PG backend (CNPG `tofu-state-db`, schema `main`, 2 replicas on VPS `local-path`). Backup
CronJob writes `pg_dump` to `proxmox-csi-retain` PVC every 6 hours.

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

Flux `Receiver` at `flux-webhook.allegedly.works`. Harbor webhook auto-configured by
`harbor-webhook` Terraform.

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
Default mode "Off" (recommendation-only). Enable per namespace:

```bash
kubectl label ns <namespace> goldilocks.fairwinds.com/vpa-update-mode=auto
```

- **`auto`** (21 ns): activitywatch, airlock, atuin, gatus, gitea, google-workspace-mcp,
  grocy, harbor, headlamp, homeassistant-proxy, inventree, langfuse, matrix, nix-cache,
  ollama, openclaw-gateway, openclaw-mitmproxy, props, proxmox-proxy, scanner, tana-mcp
- **`initial`** (6 ns): authentik, cnpg-system, dns-system, loki, monitoring, vault

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
See <lessons_learned/2026-02-11-cilium-mtu-cross-node-packet-loss.md>.

### Storage Strategy

Minimize Hetzner volumes; generous on Proxmox.

| Location | Services                                       | Rationale                         |
| -------- | ---------------------------------------------- | --------------------------------- |
| VPS      | Vault, Authentik, Gateway, DNS, cert-mgr       | Always-on, critical path          |
| Home     | Harbor, Gitea, Loki, Grafana, media, Nix cache | Storage-heavy, tolerates downtime |

CNPG: individual clusters per app, all on `local-path`. Two profiles: VPS-HA
(2 instances, Hetzner) and Proxmox-single (1 instance). See <cnpg-conventions.md>.

## Related Documentation

- <bootstrap.md>
- <troubleshooting.md>
- <changelog.md>
- <lessons_learned/2025-11-28-eso-password-generator-desync.md>

**Monthly Cost**: ~EUR30 (2x CPX31). CPX41 upgrade planned (see <plans/2026-02-22-vps-cpx41-upgrade.md>).
