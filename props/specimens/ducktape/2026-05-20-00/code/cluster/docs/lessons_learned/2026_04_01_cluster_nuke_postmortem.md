# Cluster Nuke Postmortem — 2026-04-01

**Status**: Resolved (cluster destroyed, awaiting bootstrap)
**Impact**: Full cluster outage. All 4 Hetzner VPS servers deleted, Proxmox CP left with dead etcd.
**Duration**: ~3 hours active incident, cluster offline pending rebuild.

## Timeline

### Session 1: `0f9cce59` — Code commit (April 1, ~12:10)

1. User requested adding 2x CPX31 VPS worker nodes to the cluster
2. Agent wrote Terraform config for workers (`vps_worker0`, `vps_worker1`), Nebula certs,
   Longhorn Hetzner-only restriction
3. Committed as `852bb7df3` ("Add 2x CPX31 VPS workers, restrict Longhorn to Hetzner")
4. Ran `tofu plan` — showed 8 to add, 8 to change, 3 to destroy (safe plan)
5. Started `tofu apply -auto-approve` but **user interrupted during state refresh** (exit 137)
6. No infrastructure was created — apply never reached the resource creation phase

### Session 2: `45d84d85` — Recovery attempt (April 1, ~15:30)

User asked "is cluster ollama up?" — agent discovered widespread kustomization failures.

1. **Discovery**: Ollama blocked on `ollama-secrets` → `external-secrets-config` → Vault →
   `vault-operator` → `proxmox-csi` → `sealed-secrets` → `cert-manager`. Root: stale
   reconciliation status (cert-manager was Ready but downstream thought it wasn't).

2. **Force reconcile**: `flux reconcile kustomization sealed-secrets` succeeded, unblocking
   some of the chain.

3. **Nebula down on wyrm2**: `lookup talos-vps-cp-0.nebula.allegedly.works: i/o timeout`.
   Haproxy on wyrm2 had no healthy backends → kubelet couldn't register → wyrm2 NotReady.

4. **Worker creation attempt**: User said "make new vps's". Agent noticed workers weren't in
   tofu state (session 1 was interrupted). Servers already existed in Hetzner from a prior
   attempt (IDs 125604446, 125604447).

5. **Import block mistake**: Agent added `import {}` blocks to `hetzner-nodes.tf` to import
   both workers atomically. **However, `ssh_keys` was not in `ignore_changes`**, so tofu
   planned: import → **destroy** → recreate. The `tofu apply -auto-approve -target` ran and
   **destroyed both existing workers and created new ones**.

6. **Wrong user_data**: The newly created servers booted with user_data from the Talos snapshot.
   VNC console showed: `"etcd config is only allowed on control plane machines"` — the
   snapshot's initial config was a controlplane config (from the Hetzner Packer build),
   and `talos_machine_configuration_apply` (which delivers the correct worker config via
   Talos API) hadn't run yet because the workers couldn't boot far enough to accept it.

7. **State push failed**: `talos_machine_configuration_apply` timed out (workers not ready).
   Meanwhile, the kubectl port-forward to PG died. Tofu wrote `errored.tfstate` locally
   and left a stale lock.

8. **API server flapping**: Both VPS control plane API servers started returning connection
   refused intermittently. CNPG `tofu-state-db` had no primary (both instances stuck as
   replicas with "operation not permitted" on ClusterIP).

9. **Manual nuke**: After determining the cluster was unrecoverable without significant
   manual intervention, user authorized deletion of all 4 Hetzner servers via `hcloud
server delete`.

## Root Causes

### RC1: Import blocks triggered forced replacement

`hcloud_server` has `lifecycle { ignore_changes = [user_data, image] }` but NOT
`ssh_keys`. When the import block imported the servers, tofu saw `ssh_keys` as a new
field and planned forced replacement (destroy + create).

**Fix**: Add `ssh_keys` to `ignore_changes` on `hcloud_server.vps`.

### RC2: Workers booted with controlplane user_data

Hetzner servers boot from a Packer-built Talos snapshot. The `user_data` (machine
config) is baked in at server creation time. The snapshot's initial config is a
controlplane config. Workers need `talos_machine_configuration_apply` to deliver the
correct worker config via the Talos API after boot.

When workers were recreated by the import-triggered replacement, they got fresh
snapshot boots with CP config. Since Talos validates the config at boot, workers
rejected it and entered a boot loop before the Talos API became available for the
config apply.

**Fix**: This is by design — `user_data` is the bootstrap config, real config comes
via API. The import-triggered replacement was the actual bug (RC1).

### RC3: PG state backend inside the managed cluster

The tofu PG backend (`tofu-state-db`) runs inside the cluster being managed. When the
cluster became unstable, PG became unreachable, and state operations failed with
`errored.tfstate` written locally.

**Mitigation**: The `errored.tfstate` local fallback worked — state was preserved.
But recovery required manual intervention to push state back once PG was accessible.

### RC4: Nebula DNS resolution failure on wyrm2

wyrm2's Nebula couldn't resolve control plane hostnames
(`talos-vps-cp-0.nebula.allegedly.works: i/o timeout`). This meant haproxy had no
healthy backends, kubelet couldn't reach the API server, and wyrm2 was NotReady.

This was a pre-existing issue — Nebula static_host_map on wyrm2 relies on DNS
resolution for lighthouse discovery.

### RC5: Cascading failures from wyrm2 being the operator workstation

wyrm2 is both a k8s worker and the machine running tofu. When wyrm2 lost cluster
connectivity (Nebula down → haproxy down → kubelet unregistered), it couldn't:

- Port-forward to PG (for tofu state)
- Run kubectl commands (for diagnosis)
- Push errored.tfstate (for recovery)

The only remaining access was `talosctl` via public VPS IPs and `hcloud` CLI.

## Architectural Footguns Identified

### 1. Tofu state stored inside the managed cluster

**Problem**: PG backend runs in the cluster. Cluster dies → state inaccessible →
can't fix cluster.

**Mitigation options**:

- External state backend (S3, GCS, or Terraform Cloud)
- Automated periodic `errored.tfstate` backup to local disk / external storage
- PG backup CronJob writes to external storage (partially exists: pg_dump to PVC)

### 2. Tofu runs from a machine managed by tofu

**Problem**: `module.wyrm2` in the same TF root manages the VM running tofu. A
`tofu apply` that changes wyrm2 config (bridge, disks, resources) can reboot the
machine mid-apply.

**Mitigation options**:

- Exclude `module.wyrm2` from the main TF root (separate root or manual management)
- Never run tofu against wyrm2's own config from wyrm2 itself
- Add guard in bootstrap script to skip wyrm2 changes when running on wyrm2

### 3. `ignore_changes` doesn't cover all immutable fields

**Problem**: `hcloud_server` ignores `user_data` and `image` but not `ssh_keys`.
Import or any state drift on `ssh_keys` forces replacement.

**Fix**: Add `ssh_keys` to `ignore_changes`.

## Incident 2: wyrm2 rebooted during bootstrap recovery

During the bootstrap recovery attempt (same day), the bootstrap script's Phase 2
targeted apply included `proxmox_virtual_environment_vm.talos["pve_cp0"]`. Although
`module.wyrm2` was not in the target list, the Proxmox provider applied a pending
config change (bridge `vmbr0→vmbr4`) to wyrm2 as a side effect, triggering
`qmshutdown:110:root@pam!tofu` at 21:31.

This is the exact footgun identified in "Architectural Footgun #2" above — tofu
managing wyrm2 from wyrm2. The fix is to use `-exclude=module.wyrm2` on all tofu
applies run from wyrm2.

## Recovery Changes (2026-04-02 bootstrap)

- SealedSecrets fully replaced with SOPS (all 26 secret files converted)
- Nebula CA moved from tofu-generated to SOPS (`secrets/nebula-ca.yaml`)
- Flux deploy key moved from tofu-generated to SOPS (`secrets/shared/flux-deploy-key.yaml`)
- Cluster age keypair moved to SOPS (`secrets/shared/cluster-secrets-age.yaml`)
- Sealed-secrets controller removed from cluster
- `flux-system` namespace now created by tofu in Phase 2 (for SOPS age secret)
- DNS records moved from tofu-controller to declarative ClusterRRset CRDs

**Key lesson**: ALWAYS verify `tofu state list` shows resources in the target
backend before deleting any backups or source backends. `tofu init -migrate-state`
to PG can silently write nothing.

## Action Items

- [x] Add `ssh_keys` to `hcloud_server.vps` `ignore_changes` lifecycle
- [x] Split `common_cluster_config` into CP and worker variants (workers had CP-only
      etcd/apiServer/kubernetesTalosAPIAccess settings causing boot loop)
- [x] Fix Proxmox SSH address to use VLAN IP directly (avoid Nebula DNS chicken-and-egg)
- [x] Bootstrap cluster from `errored.tfstate` + temp local PG (completed 2026-04-02)
- [x] Always use `-exclude=module.wyrm2` when running tofu from wyrm2
      (implemented in `cluster/bootstrap.py` — auto-detects hostname and aborts without exclude)
- [ ] Consider external state backend or automated state backup
- [ ] Add AGENTS.md guidance: never use `import {}` blocks without reviewing
      full plan for forced replacements
