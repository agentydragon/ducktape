# Cluster Roadmap

**Status**: 5 nodes (2 VPS + 1 Proxmox CP + 1 GPU worker + 1 roaming laptop).
Cilium Gateway API, DNS automation, Authentik SSO. PowerDNS and Authentik on
CloudNativePG `local-path`. See <changelog.md> for history.

## Suspended Kustomizations

- **Kagent**: `kagent`, `kagent-namespace`, `kagent-secrets`
- **Grocy**: `grocy`, `grocy-namespace`
- **Gitea**: `gitea`, `gitea-namespace`, `gitea-secrets`, `gitea-admin-token`,
  `gitea-servicemonitor`, `authentik-blueprint-gitea-secret` — VPS memory pressure
  (2026-03-17 OOM). Re-enable after rebalancing or CPX41 upgrade.
- **BuildBuddy Executor**: `buildbuddy-executor` — scaled to 0. Re-enable when needed.
- **InvenTree**: `inventree`, `inventree-namespace`, `inventree-secrets`,
  `inventree-token-provisioner`, `authentik-blueprint-inventree-secret` — VPS memory pressure.

## Next Actions

- [ ] Prometheus: investigate memory growth and right-size (OOM-killed 8x at 2Gi, pinned to wyrm2 at 6Gi)
- [ ] Prometheus: unpin from wyrm2 (blocked on PriorityClasses + right-sizing + VPS headroom)
- [ ] `talos-pve-cp-0`: evict Longhorn storage (pure CP node, has stopped replicas)
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
- [ ] Nix cache: initialize Attic cache (`attic cache create main` + configure)
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

Runtime secrets use Terraform -> Vault -> ESO. Bootstrap secrets use SealedSecrets. Zero ESO Password generators remain.
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
with LE cert, managed by `dns-records` TF + `k8s/kube-api-proxy/`). Bootstrappable from
cluster — tofu-controller creates the DNS record, Flux deploys the proxy.

- [ ] Patch TF-generated kubeconfig post-bootstrap to use `api.allegedly.works:16443`

### Backup: persistent-auth Terraform State

`persistent-auth/terraform.tfstate` is local-only SSOT. Minimum: rclone to encrypted cloud.
Better: S3 backend with OpenTofu state encryption + versioning.

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

CNPG: individual clusters per app (Authentik, PowerDNS) on `local-path` for fault isolation.

## Related Documentation

- <bootstrap.md>
- <troubleshooting.md>
- <changelog.md>
- <lessons_learned/2025-11-28-eso-password-generator-desync.md>

**Monthly Cost**: ~EUR30 (2x CPX31). CPX41 upgrade planned (see <plans/2026-02-22-vps-cpx41-upgrade.md>).
