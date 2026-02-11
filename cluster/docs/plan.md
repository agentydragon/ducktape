# Cluster Roadmap

**Last Updated**: 2026-02-11

## 🔥 Immediate Next Steps

**Status**: Cluster torn down. Blocked on VPS KubeSpan identity collision.

### Problem: VPS KubeSpan Mesh Broken — Shared Node Identity

Last bootstrap (2026-02-11) stalled at 34/64 Ready. Only 1 of 4 nodes joined
Kubernetes. Root cause: **KubeSpan mesh completely non-functional**.

**Observed symptoms**:

- Both VPS member entries in Talos discovery showed the **same IP** (crossed/wrong IPs)
- All KubeSpan peers stuck in `state: unknown`, zero WireGuard handshakes
- `pve-cp-0` etcd crash-looping: "cannot fetch cluster info from peer urls: EOF"
- `vps-cp-1` etcd stuck in "Preparing" — waiting to join
- Phantom KubeSpan address (`fd05:...:4b69`) not matching any current node
- Only 1 of 4 nodes visible in `kubectl get nodes`

**Suspected root cause (unconfirmed)**: Both VPS nodes boot from the **same
Hetzner snapshot** (built by Packer via rescue+dd of a single Talos Image Factory
image). This _might_ cause shared Talos node identity or shared KubeSpan identity,
but we do not have positive confirmation of identity collision. We only know
KubeSpan ended up in a broken state.

### How Talos Installation Currently Works

| Node type   | Image source             | Disk mechanism                  | Identity isolation            |
| ----------- | ------------------------ | ------------------------------- | ----------------------------- |
| Hetzner VPS | Packer snapshot (shared) | Both servers boot same snapshot | **Unknown — likely broken**   |
| Proxmox     | QCOW2 download (shared)  | `import_from` copies per-VM     | Each VM gets independent disk |

**VPS flow**: Image Factory → raw.xz → Packer dd's to `/dev/sda` in rescue mode
→ snapshot → both `hcloud_server.vps` boot from it.

**Proxmox flow**: Image Factory → QCOW2 → `proxmox_virtual_environment_download_file`
→ `import_from` (copies per VM) → each VM boots independent disk.

Proxmox nodes get unique identities because `import_from` creates a copy. Hetzner
nodes share the exact same disk content from the snapshot.

### Research Needed

1. **When does Talos initialize `node-identity.yaml`?** During image build, or
   on first boot? If on first boot, the snapshot should be identity-free and both
   VPS nodes should get unique identities. If it's in the image, that's the bug.

2. **Is the collision in node identity or KubeSpan identity?** KubeSpan ULA
   address is derived from `ClusterID + first NIC MAC`. If both VPS nodes happen
   to get the same MAC from Hetzner (unlikely but possible), that would collide
   independently of node identity.

3. **Could stale discovery entries cause this?** The cluster ID (from persistent
   machine secrets in layer 00) survives destroy/recreate. Old node registrations
   at `discovery.talos.dev` may poison the mesh for new incarnations.

4. **What changed in last 48 hours?** Commit `4a82c185a` switched VPS from ISO
   boot to Packer snapshot boot. This is the most likely change that introduced
   the identity sharing.

### Proposed Fix: Per-Node Packer Snapshots

Generate independent Hetzner snapshots per VPS node. Each Packer build creates a
separate snapshot → separate disk content → separate identity initialization.

**TODO**: This is a **workaround without full understanding** of the root cause.
We do not have positive confirmation that shared identity is the problem — we
only observed broken KubeSpan state with crossed IPs. The research questions
above should be answered first to determine whether per-node snapshots actually
fix the issue, or if the root cause is something else entirely (stale discovery
entries, MAC collision, timing issue, etc.).

### After Fix: What to Verify

1. **Unique KubeSpan identities** — each node has distinct peer address:
   ```bash
   talosctl -n <vps-ip> -e <vps-ip> get kubespanpeerstatuses
   # Expect: exactly 3 peers, all "up", no duplicates
   ```
2. **VPS DNS resolution** — containerd can pull images:
   ```bash
   talosctl -n <vps-ip> -e <vps-ip> image pull docker.io/library/alpine:3.19
   ```
3. **Full convergence** — all 64 kustomizations Ready
4. **cert-manager challenges resolve**:
   ```bash
   dig @8.8.8.8 SOA _acme-challenge.vault.allegedly.works
   ```

### Known Issues to Watch

- **BuildBuddy executor** `Failed` — pinned to Proxmox nodes, may need resource adjustment
- **Kyverno webhook timeouts** — may be cross-node networking transient

### Next Action: Strip Cilium to Talos-Recommended Defaults

See <../investigations/2026-02-11-bootstrap-cross-node-and-kyverno/recommendation-minimal-networking.md>
for full analysis, proposed config, network stack diagrams, and diagnostic checklist.

**Summary**: The current Cilium config has 10+ non-default options that Talos docs
explicitly warn against with KubeSpan. The DNS failure and cross-node instability
were consequences of these options, not inherent incompatibilities. Strip to the 7
Talos-recommended values + `mtu: 1370` (for VXLAN+WireGuard double encapsulation)

- hubble. Revert DNS workarounds (`forwardKubeDNSToHost`, explicit nameservers,
  CoreDNS upstream) to defaults. Fallback plan ready if HostDNS still fails.

---

## 🎯 Target Architecture

The cluster will run entirely on Talos:

- **2x Hetzner VPS** - Control plane nodes with public IPs
- **1+ Proxmox VMs** - Control plane + workers on home server (atlas)

No separate ansible-managed VPS. Everything currently on the VPS must move into the cluster.

**End state**: Cluster handles everything on `allegedly.works` (test) then `agentydragon.com` (production).

## Domain Strategy

| Domain             | Purpose                     | Status                    |
| ------------------ | --------------------------- | ------------------------- |
| `allegedly.works`  | Test/staging cluster        | Registered, pending setup |
| `agentydragon.com` | Production (future cutover) | On ansible VPS            |

## Current Nodes

| Node               | Location | Role          | IP            |
| ------------------ | -------- | ------------- | ------------- |
| talos-vps-cp-0     | Hetzner  | control-plane | (new on boot) |
| talos-vps-cp-1     | Hetzner  | control-plane | (new on boot) |
| talos-pve-cp-0     | Proxmox  | control-plane | 10.2.1.1      |
| talos-pve-worker-0 | Proxmox  | worker        | 10.2.2.1      |

## Core Services (already configured)

| Component              | Status | Notes                                      |
| ---------------------- | ------ | ------------------------------------------ |
| Flux CD                | ✅     | GitOps                                     |
| ingress-nginx          | ✅     | hostNetwork on VPS nodes                   |
| cert-manager           | ✅     | DNS-01 via PowerDNS                        |
| PowerDNS               | ✅     | hostNetwork on VPS nodes                   |
| Vault                  | ✅     | With OIDC auth                             |
| Authentik              | ✅     | SSO provider                               |
| External Secrets       | ✅     | Vault integration                          |
| Monitoring             | ✅     | Prometheus/Grafana/Loki                    |
| Proxmox CSI            | ✅     | Storage for home nodes                     |
| local-path-provisioner | ✅     | Storage for VPS nodes                      |
| Stakater Reloader      | ✅     | Deployed, adopted (7/7 services)           |
| DNS Automation         | ✅     | tofu-controller manages Route53 + PowerDNS |

## Applications (already configured)

| App            | Purpose            | SSO |
| -------------- | ------------------ | --- |
| Harbor         | Container registry | ✅  |
| Gitea          | Git hosting        | ✅  |
| Matrix/Element | Chat               | ✅  |
| Nix cache      | Binary cache       | -   |
| BuildBuddy     | Remote build exec  | -   |
| Headscale      | Tailscale control  | -   |
| Website        | Static placeholder | -   |

## Applications (disabled - need flux-kustomization.yaml)

| App       | Purpose          | Status                                       |
| --------- | ---------------- | -------------------------------------------- |
| Firecrawl | Web scraping API | Helm chart + manifests exist, needs enabling |
| Devbot    | Agent workload   | Manifests exist, needs enabling              |

TODO: Re-add flux-kustomization.yaml files and integrate into root kustomization.yaml
when ready to deploy these applications.

---

## 🚨 Minimal Requirements for Go-Live

### Public Traffic Routing

| Status | ✅ Configured (hostNetwork) |
| ------ | --------------------------- |

ingress-nginx and PowerDNS run with `hostNetwork: true`, binding directly to VPS node IPs.

**Traffic flow**:

```text
Internet → VPS public IP:443 → ingress-nginx pod (hostNetwork) → backend services
Internet → VPS public IP:53  → PowerDNS pod (hostNetwork) → DNS responses
```

**Failover via DNS**: DNS returns two A records (both VPS IPs). Modern browsers handle failover automatically.

### Website Hosting

| Status | ✅ Manifests created |
| ------ | -------------------- |

- [ ] Personal website accessible (test domain first, then `agentydragon.com`)
- **Current state**: Hakyll-built site, rsync to ansible VPS, served by nginx
- **Initial implementation**: Simple nginx + static HTML placeholder
- **Location**: `k8s/applications/website/`

### Atlas Proxmox Access

| Status | ✅ Via Headscale mesh |
| ------ | --------------------- |

- [ ] Atlas joins headscale mesh
- Access via tailscale IP: `ssh root@100.64.x.x` or `https://100.64.x.x:8006`
- No public ingress needed - internal mesh access only

**Dependency**: Requires headscale running first. Once atlas joins the mesh, it's accessible from any device in the tailnet.

**Deferred**: Public DNS access (`atlas.allegedly.works`) is optional - would require proxy pod on Proxmox worker or tailscale on VPS nodes. Not needed for go-live.

### Headscale Server

| Status | ✅ Manifests created |
| ------ | -------------------- |

- [ ] Headscale server running as cluster workload
- [ ] Stable public endpoint for Tailscale clients
- [ ] Persistent storage for database (SQLite on PVC)
- **Location**: `k8s/applications/headscale/`

**Architecture**: Headscale exposed via public ingress on VPS nodes. Non-cluster devices (laptops, phones, atlas) connect via public DNS.

### kubectl Access

| Status | ✅ Configured |
| ------ | ------------- |

- [x] Working kubectl access to the cluster
- Via KUBECONFIG from terraform output
- direnv auto-exports when in cluster directory

---

## ⚠️ Remaining Work Before Bootstrap

### Domain Switchover

✅ **Complete** - All manifests updated from `test-cluster.agentydragon.com` to `allegedly.works`

### VPS IP Configuration

✅ **Automated via DNS Automation** (tofu-controller)

After cluster boots:

- Route 53 glue records (ns1/ns2.allegedly.works) are created automatically
- PowerDNS NS A records within zone are created automatically
- VPS IPs are read from `cluster-info` ConfigMap created by infrastructure terraform

**Remaining manual step**:

- `k8s/external-dns/deployment.yaml` `--default-targets` - still hardcoded (optional future automation)

### Registrar DNS Configuration

✅ **Route 53 glue records managed by tofu-controller**

NS delegation is configured at Route 53 (zone `Z02901943N8ZFQFOD9P5I`):

- NS records pointing to ns1/ns2.allegedly.works
- Glue A records automatically updated when VPS IPs change

### PowerDNS Zone

Create zone for `allegedly.works` in PowerDNS (update `k8s/powerdns-zones/clusterzone.yaml`).

### Deferred

`k8s/applications/atlas-proxy/` - not needed for go-live, atlas access via headscale mesh instead.

---

## 📋 Migration Path

### Phase 1: Cluster Bootstrap

1. [ ] Run `bazel run //cluster:bootstrap` to create VPS nodes (new public IPs assigned)
2. [ ] Verify VPS IPs in ConfigMap:
   ```bash
   kubectl get configmap cluster-info -n kube-system -o jsonpath='{.data.vps_nodes}' | jq
   ```
3. [x] ~~Update cluster configs with new VPS IPs~~ - **Automated via DNS automation**
   - Route 53 glue records: tofu-controller creates automatically
   - PowerDNS NS A records: tofu-controller creates automatically
   - `k8s/external-dns/deployment.yaml` `--default-targets`: still manual (optional)
4. [ ] Verify DNS automation applied:
   ```bash
   kubectl get terraform dns-records -n flux-system
   aws route53 list-resource-record-sets --hosted-zone-id Z02901943N8ZFQFOD9P5I \
     --query "ResourceRecordSets[?Name=='ns1.allegedly.works.']"
   ```
5. [ ] Verify DNS resolution: `dig @ns1.allegedly.works allegedly.works`
6. [ ] Verify certs issue: `kubectl get certificates -A`

### Phase 2: Deploy Missing Services

5. [ ] Deploy headscale, test with a device
6. [ ] Deploy website, verify accessible
7. [ ] Configure atlas proxy (update IP)
8. [ ] Test all services on allegedly.works

### Phase 3: Production Cutover

9. [ ] Migrate Tailscale devices from ansible VPS headscale to cluster headscale
10. [ ] Update `agentydragon.com` DNS to point to cluster
11. [ ] Decommission ansible-managed VPS

---

## 🔧 Operational Hardening

### ESO Password Generator Volatility Fix

**Problem**: ESO Password generators regenerate on every `refreshInterval`. Applications that persist
credentials (PostgreSQL, Authentik) don't auto-update, causing authentication failures after refresh.

**Current Workaround**: `refreshInterval: 8760h` (1 year) - stops regeneration but prevents rotation.

#### Phase 1: Reloader Adoption ✅ Complete

Stakater Reloader auto-restarts pods when secrets change. All services now have
`reloader.stakater.com/auto: "true"` annotation (PowerDNS, Grafana, Gitea, Matrix, Vault, Authentik, Nix-cache).

After cluster is stable, can reduce `refreshInterval` to 24h-168h.

**Note**: Reloader handles pod-level secret consumption. Does NOT fix init-time persistence
(PostgreSQL passwords written to DB on first boot). Phase 2 addresses that.

#### Phase 2: Migrate Password Generators to Vault SSOT

Replace ESO Password generators with Vault KV sources. Terraform generates once → stores in Vault →
ESO reads stable value.

**ESO Password generators to migrate**:

| File                                                       | Secret                                      | Notes                       |
| ---------------------------------------------------------- | ------------------------------------------- | --------------------------- |
| `k8s/powerdns/externalsecret-api-key.yaml`                 | PowerDNS API key                            | Breaks cert-manager webhook |
| `k8s/authentik/postgres-external-secret.yaml`              | PostgreSQL password                         | Init-time persistence       |
| `k8s/authentik/admin-password-external-secret.yaml`        | Admin password                              |                             |
| `k8s/authentik/secret-key-external-secret.yaml`            | Secret key                                  |                             |
| `k8s/authentik-blueprint/users/password-secret.yaml`       | User password                               |                             |
| `k8s/applications/gitea/secrets.yaml`                      | Admin password                              |                             |
| `k8s/applications/matrix/secrets.yaml`                     | 3 secrets (signing, registration, macaroon) |                             |
| `k8s/monitoring-stack/admin-password-external-secret.yaml` | Grafana admin                               |                             |

**Implementation**:

1. Create `terraform/gitops/secrets/` module with `random_password` resources
2. Store in Vault KV at `kv/cluster/{service}/{secret}`
3. Update ExternalSecrets to use `remoteRef` instead of `generatorRef`
4. Remove Password generator resources

See `docs/archive/SECRET_SYNCHRONIZATION_ANALYSIS.md` for detailed analysis.

---

### Kyverno GitOps Enforcement

**Status**: ✅ Deployed (Audit mode)

Kyverno deployed with `require-gitops` ClusterPolicy. Separated into own kustomization with
ValidatingWebhookConfiguration health check to ensure webhook is operational before other
workloads deploy.

**Location**: `k8s/kyverno/` (separate from core)

**Dependency chain**: cert-manager → kyverno → core/metrics-server → everything else

**Current mode**: `validationFailureAction: Audit` - logs violations but doesn't block.
Change to `Enforce` after validation in live cluster.

---

### TODO: Firewall Hardening

**Problem**: All cluster ports exposed to 0.0.0.0/0 including K8s API, Talos API, etcd, kubelet.

**Current state** (`terraform/01-infrastructure/main.tf` lines 128-221):

```hcl
# All rules have: source_ips = ["0.0.0.0/0", "::/0"]
```

**Recommended changes**:

| Port        | Service      | Current   | Should Be                 |
| ----------- | ------------ | --------- | ------------------------- |
| 80, 443     | HTTP/HTTPS   | 0.0.0.0/0 | ✅ Keep (public ingress)  |
| 53          | DNS          | 0.0.0.0/0 | ✅ Keep (public DNS)      |
| 6443        | K8s API      | 0.0.0.0/0 | Restrict to known IPs     |
| 50000-50001 | Talos API    | 0.0.0.0/0 | Restrict to known IPs     |
| 51820       | KubeSpan     | 0.0.0.0/0 | Restrict to VPS + Proxmox |
| 8472        | Cilium VXLAN | 0.0.0.0/0 | Restrict to VPS + Proxmox |
| 2379-2380   | etcd         | 0.0.0.0/0 | Restrict to VPS + Proxmox |
| 10250       | kubelet      | 0.0.0.0/0 | Restrict to VPS + Proxmox |

**Implementation approach**:

```hcl
locals {
  # Known admin IPs (update with your IPs)
  admin_ips = ["YOUR_HOME_IP/32", "YOUR_MOBILE_IP/32"]

  # Inter-node communication (VPS public IPs + Proxmox subnet via KubeSpan)
  cluster_ips = concat(
    [for s in hcloud_server.vps : "${s.ipv4_address}/32"],
    ["10.2.0.0/16"]  # Proxmox subnet reachable via KubeSpan
  )
}

# K8s API - admin only
rule {
  port       = "6443"
  source_ips = local.admin_ips
}

# etcd - cluster internal only
rule {
  port       = "2379-2380"
  source_ips = local.cluster_ips
}
```

---

### TODO: Remote Proxmox API Access

**Current state**: Proxmox API only reachable via VLAN IP (10.2.0.2) from home network.

- CSI driver uses 10.2.0.2:8006 (works because pods run on Proxmox VMs)
- Terraform provisioning also uses 10.2.0.2:8006 (only works from home)

**Future enhancement**: Split CSI and provisioning hosts, or add Tailscale route.

Options:

1. **Separate variables** - `proxmox_csi_host` (10.2.0.2) vs `proxmox_api_host` (Tailscale)
2. **Tailscale on Proxmox VLAN** - Route 10.2.0.0/16 via Tailscale for remote access
3. **Keep as-is** - Accept that Proxmox provisioning requires home network

---

### TODO: Multi-Endpoint Kubeconfig via DNS

**Current state**: `local_file.kubeconfig` points to a single VPS IP (the bootstrap node). If that node is down, `kubectl` can't connect.

**Desired state**: Kubeconfig uses a DNS name (e.g., `api.allegedly.works`) that resolves to all control plane nodes. Clients automatically fail over to a healthy node.

**Prerequisites**: Cluster DNS (PowerDNS) must be running first — chicken-and-egg with bootstrap.

**Implementation**:

1. Add `api.allegedly.works` A records pointing to all VPS control plane IPs (via DNS automation)
2. Change `local_file.kubeconfig` to use `https://api.allegedly.works:6443`
3. Bootstrap still needs direct IP for initial kubeconfig (before DNS is available)
4. Post-bootstrap step: regenerate kubeconfig with DNS name once DNS is live

---

### TODO: Terraform State Backup

**Problem**: If `terraform/00-persistent-auth/terraform.tfstate` is lost, all SealedSecrets become
undecryptable. This is the single source of truth for the sealed-secrets keypair.

**Current state**: Local file only, no backup.

**Options**:

1. **rclone + Google Drive** (documented in Future Directions below)
2. **Encrypted S3/GCS backend** - Terraform native, but exposes to cloud provider
3. **git-crypt in separate repo** - Version controlled but complex
4. **Manual backup script** - Simple, run after `terraform apply`

**Minimum viable implementation**:

```bash
#!/bin/bash
# scripts/backup-terraform-state.sh
set -e
BACKUP_DIR="$HOME/gdrive-backup/terraform-state"
mkdir -p "$BACKUP_DIR"
for state in terraform/*/terraform.tfstate; do
  cp "$state" "$BACKUP_DIR/$(dirname $state | tr / -)-$(date +%Y%m%d).tfstate"
done
echo "Backed up to $BACKUP_DIR"
```

Add to post-apply hook or document as manual step.

---

### TODO: GitHub Webhook for Instant Reconciliation

**Problem**: Flux polls git repository on interval (default 1m). Changes aren't applied instantly.

**Solution**: Configure GitHub webhook to notify Flux receiver, triggering immediate reconciliation on push.

**Implementation**:

1. Create Flux `Receiver` resource (webhook endpoint)
2. Create sealed secret with webhook token
3. Configure GitHub repo webhook to POST to receiver URL
4. Receiver triggers GitRepository reconciliation

**Reference**: <https://fluxcd.io/flux/guides/webhook-receivers/>

---

### Flux Reconciliation Failure Alerts

**Status**: ✅ Configured

- ntfy.sh push notifications: `k8s/flux-system/flux-alerts.yaml`
- Grafana/Prometheus alerting: `k8s/monitoring-stack/flux-prometheus-rule.yaml`

---

### TODO: Flux Kustomization Dependency Graph UI

**Priority**: Low

Deploy a web UI that visualizes Flux kustomization status and dependency DAG as a node/edge graph.

**Options**:

- **Weave GitOps** — official Flux UI, shows kustomizations, HelmReleases, sources, dependency graph. Helm chart at `oci://ghcr.io/weaveworks/charts/weave-gitops`.
- **Capacitor** — lighter Flux dashboard, less mature.
- **Custom Grafana panel** — Flux Prometheus metrics exist but no dependency graph support.

---

## 🔀 Future Directions

### Terraform State Backup (rclone + Google Drive)

Protect terraform state with encrypted cloud backup.

**Implementation**:

- [ ] Configure rclone with Google Drive
- [ ] Encrypt terraform state before upload
- [ ] Create backup script in scripts/
- [ ] Document restore procedure
- [ ] Optional: Automated backup on terraform apply

**Scope**: `terraform/*/terraform.tfstate` files (contain all secrets)

### GPU Workloads (Ollama + Auth Proxy)

Move GPU from standalone VM (wyrm) to k8s cluster for LLM inference.

**Current State**: RTX 5090 passed through to wyrm VM, Ollama running as systemd service

**Target State**: GPU passed to k8s worker node, Ollama in pod with auth proxy

**Architecture**:

```text
┌─────────────────────────────────────────────────────────────┐
│  Internet → Ingress → Auth Proxy → Ollama Pod              │
│                         ↓                                    │
│                   API Key Validation                        │
│                   (nginx/Caddy sidecar)                     │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│  GPU Worker Node (Proxmox VM with PCIe passthrough)         │
│  - NVIDIA driver + container toolkit                        │
│  - Node label: nvidia.com/gpu=true                          │
│  - Talos extension: nvidia-container-toolkit                │
└─────────────────────────────────────────────────────────────┘
```

**Implementation**:

- [ ] Configure Proxmox VM with GPU passthrough (PCIe device 01:00)
- [ ] Add Talos nvidia-container-toolkit extension to worker image
- [ ] Deploy NVIDIA device plugin DaemonSet
- [ ] Create Ollama Deployment with GPU resource request
- [ ] Add auth proxy sidecar (nginx with `auth_request` or Caddy with `basicauth`)
- [ ] Store API keys in Vault, sync via ESO
- [ ] Expose via Ingress with TLS

**Auth Proxy Options**:

1. **nginx sidecar** - `auth_request` directive validates Bearer token against configmap/secret
2. **Caddy sidecar** - `basicauth` or forward_auth to validate API key
3. **oauth2-proxy** - Full OIDC if multi-user access control needed

**Why k8s instead of standalone VM**:

- Unified management (GitOps, monitoring, secrets)
- Easier scaling (add more GPU nodes)
- Ingress/TLS handled by existing infrastructure
- API key rotation via Vault/ESO

### BuildBuddy Remote Executor

Remote build execution via BuildBuddy Cloud.

**Status**: Deployed (pending cluster bring-up for verification)

**Implementation**:

- [x] HelmRelease using official `buildbuddy-executor` chart from `https://helm.buildbuddy.io`
- [x] API key sealed as SealedSecret, injected via Flux `valuesFrom`
- [x] Pinned to Proxmox nodes (`topology.kubernetes.io/region: proxmox`)
- [x] 2 replicas, 2 CPU / 8Gi limits each
- [ ] Verify executor connects to BuildBuddy Cloud after cluster bootstrap

**Location**: `k8s/applications/buildbuddy-executor/`

### Shared PostgreSQL / MariaDB Galera

Migrate from current single-instance MariaDB to replicated Galera cluster.

**Current State**: PowerDNS with single MariaDB on Proxmox CSI

**Target State**: PowerDNS + MariaDB Galera (3-node) + powerdns-operator

**Galera Node Placement** (for quorum):

| Node     | Location       | Storage    | Purpose       |
| -------- | -------------- | ---------- | ------------- |
| galera-0 | talos-vps-cp-0 | local-path | Primary VPS   |
| galera-1 | talos-vps-cp-1 | local-path | Secondary VPS |
| galera-2 | talos-pve-\*   | local-path | Tie-breaker   |

Any single node failure maintains 2/3 quorum.

**Implementation**:

- [ ] Deploy `mariadb-galera` as separate HelmRelease (Bitnami chart)
- [ ] Configure pod anti-affinity to spread across VPS + Proxmox
- [ ] Use `local-path` storage (no Hetzner volume costs)
- [ ] Modify PowerDNS to connect to Galera cluster
- [ ] Deploy `powerdns-operator` for ClusterZone CRD
- [ ] Create `powerdns-zones` with declarative zone + records
- [ ] Verify ExternalDNS auto-creates records from Ingress annotations

See **DNS Architecture** section below for details.

---

## 📋 Future Services (Lower Priority)

- [ ] Jellyfin (media streaming)
- [ ] \*arr stack (media automation)
- [ ] Paperless-ngx (document management)
- [ ] Syncthing (file sync)
- [ ] Bazel Remote Cache

---

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
- MTU must be set to 1370 to avoid fragmentation (VXLAN 50 + WireGuard 80 = 130 byte overhead)

**Firewall**: UDP 8472 required for VXLAN overlay

See <../investigations/2026-02-11-bootstrap-cross-node-and-kyverno/recommendation-minimal-networking.md>
for network stack diagrams and diagnostic checklist.

### KubePrism for Cluster Endpoint

**Decision**: Use `localhost:7445` as cluster_endpoint

**Rationale**:

- No VIP possible across VPS and home networks
- KubePrism runs on every node, proxies to available API servers
- Kubeconfig patched post-bootstrap to use real VPS IP

### DNS Architecture

**Decision**: PowerDNS + MariaDB Galera + powerdns-operator + ExternalDNS

**Old Architecture** (Proxmox-only era):

- Cluster PowerDNS on MetalLB VIP (internal)
- VPS PowerDNS in Docker (external, public-facing)
- AXFR replication from cluster → VPS
- Complex, two separate systems

**New Architecture** (Hybrid VPS + Proxmox):

- VPS nodes ARE Kubernetes nodes with public IPs
- PowerDNS pod runs directly in cluster, accessible via VPS public IPs
- No AXFR needed - single source of truth
- MariaDB Galera for database redundancy (3-node across VPS + Proxmox)

```text
┌─────────────────────────────────────────────────────────────┐
│  ExternalDNS (watches Ingress → auto-creates A records)    │
│  powerdns-operator (ClusterZone CRD → manages zones)       │
└─────────────────────┬───────────────────────────────────────┘
                      ↓
┌─────────────────────────────────────────────────────────────┐
│  PowerDNS (Deployment, connects to Galera)                 │
└─────────────────────┬───────────────────────────────────────┘
                      ↓
┌─────────────────────────────────────────────────────────────┐
│  MariaDB Galera (3-node, synchronous replication)          │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐               │
│  │ VPS-0     │◄─►│ VPS-1     │◄─►│ Proxmox   │              │
│  │ local-path│  │ local-path│  │ local-path│               │
│  └───────────┘  └───────────┘  └───────────┘               │
└─────────────────────────────────────────────────────────────┘
```

**Benefits**:

- No Hetzner volume costs (local-path storage)
- Survives single node failure (2/3 quorum)
- Fully declarative (zones via CRD, records via Ingress annotations)
- No AXFR complexity

**Components**:

- `mariadb-galera` - Bitnami Helm chart
- `powerdns` - Custom chart, connects to Galera
- `powerdns-operator` - Provides ClusterZone/ClusterRRset CRDs
- `external-dns` - Already deployed, auto-creates records

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
| VPS      | Vault, Authentik, Ingress, DNS, cert-manager   | Always-on, critical path             |
| Home     | Harbor, Gitea, Loki, Grafana, media, Nix cache | Storage-heavy, can tolerate downtime |

#### Shared PostgreSQL Option

- Single PostgreSQL pod on VPS with Hetzner volume
- Multiple databases: `vault`, `authentik`, etc.
- Secrets persist across cluster destroy/recreate

---

## 🔗 Related Documentation

- **Bootstrap Procedures**: `docs/bootstrap.md`
- **Troubleshooting**: `docs/troubleshooting.md`
- **Secret Sync Analysis**: `docs/archive/SECRET_SYNCHRONIZATION_ANALYSIS.md`

---

## 📊 Cluster Specifications

- **Nodes**: 4 (2 VPS control-plane, 1 Proxmox control-plane, 1 Proxmox worker)
- **Talos**: v1.12.3
- **Kubernetes**: v1.32.0
- **CNI**: Cilium (VXLAN tunnel mode)
- **Monthly Cost**: ~€30 (2x CPX31 + backups)
