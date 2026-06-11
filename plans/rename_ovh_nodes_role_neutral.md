# Rename OVH Kimsufi nodes to role-neutral provider hostnames

Status: piloting (2026-06-01). Next pilot: **`talos-ks-game-worker-1`**
(zero singleton PVs). Strategy: delete-and-rebuild for app-replicated PVs;
backup-and-restore or delete for the handful of true singletons.

Prep done so far (2026-06-01):

- Volsync backups configured for `grocy-sf` and `grocy-vallejo`
  (commit `39778904f`). First syncs succeeded at 23:24Z / 23:25Z. Tana-MCP
  already had a pre-existing volsync backup.

## Why

The five Talos nodes on OVH bare metal carry hostnames that either leak their role
or actively lie about it. From `cluster/terraform/main/ovh-nodes.tf`:

| Current hostname         | Configured `role` in tofu | Truth                                                                     |
| ------------------------ | ------------------------- | ------------------------------------------------------------------------- |
| `talos-kimsufi-cp-0`     | `controlplane`            | Name leaks role — fine semantically, but inconsistent with renaming goal. |
| `talos-kimsufi-worker-0` | `controlplane`            | **Name lies.** Node is a control plane.                                   |
| `talos-kimsufi-worker-1` | `controlplane`            | **Name lies.** Node is a control plane.                                   |
| `talos-ks-game-worker-0` | `worker`                  | Name accurate but still role-tagged.                                      |
| `talos-ks-game-worker-1` | `worker`                  | Name accurate but still role-tagged.                                      |

Renaming once, to the provider's own identifier, eliminates both the lies and the
role-coupling. Future role reshuffles (worker → CP promotion) become a Talos
machine-type change with no hostname churn.

## Target names

`ovh-` prefix plus the OVH service identifier (the `nsXXXXXX` prefix of the
OVH service name). Single DNS label, no dots:

| Old (current)            | New            | OVH service                | Nebula IP  | OVH public IP  |
| ------------------------ | -------------- | -------------------------- | ---------- | -------------- |
| `talos-kimsufi-cp-0`     | `ovh-ns102453` | ns102453.ip-147-135-37.us  | 10.42.0.15 | 147.135.37.175 |
| `talos-kimsufi-worker-0` | `ovh-ns103656` | ns103656.ip-147-135-39.us  | 10.42.0.13 | 147.135.39.162 |
| `talos-kimsufi-worker-1` | `ovh-ns103711` | ns103711.ip-147-135-39.us  | 10.42.0.14 | 147.135.39.176 |
| `talos-ks-game-worker-0` | `ovh-ns104952` | ns104952.ip-147-135-104.us | 10.42.0.16 | 147.135.104.5  |
| `talos-ks-game-worker-1` | `ovh-ns104963` | ns104963.ip-147-135-104.us | 10.42.0.17 | 147.135.104.16 |

The `ovh-` prefix communicates provider context at a glance. The numeric ID is
the actual OVH service identifier you'd see on invoices.

### Why single-label names (no dots) — pilot lesson 2026-06-01

The original plan used the full OVH FQDN (`ns104963.ip-147-135-104.us`) as the
Talos hostname. During the pilot of `talos-ks-game-worker-1`, the new node
registered in Kubernetes as just **`ns104963`** — Talos's `HostnameConfig`
controller splits the FQDN at the first dot into `HOSTNAME=ns104963` and
`DOMAINNAME=ip-147-135-104.us`, and kubelet's hostname-detection then uses
only the (short) hostname. This silently truncated the K8s node name and
desynced it from `local-path-provisioner`'s `nodePathMap` keys.

Format assert: `cluster/validation/test_nebula_mesh.py::test_host_names_have_no_dots`
fails the build if any roster key contains a dot.

## Strategy: delete-and-rebuild via app replication

Discovered during pre-flight on 2026-06-01: `local-path-provisioner` writes each
PV's `spec.nodeAffinity` with a `kubernetes.io/hostname` In selector against the
specific node it provisioned on. A rename leaves the PV pointing at a hostname
that no longer exists. `spec.nodeAffinity` is gated mutable on the
`MutablePVNodeAffinity` feature gate (alpha in k8s 1.31, beta in 1.32). The
cluster runs k8s 1.35.1 but the dry-run patch was rejected — the gate is
effectively off here, and we chose **not** to enable it (rolling all CPs through
a gate change just to do a one-shot rename has too much surface area). Verified:

```
$ kubectl patch pv pvc-0f5010b5-... --type=json --dry-run=server \
    -p '[{"op":"replace","path":"/spec/nodeAffinity/.../values/0","value":"ovh-ns104952"}]'
The PersistentVolume "pvc-0f5010b5-..." is invalid: nodeAffinity: Invalid
  value: ... field is immutable, except for updating from beta label to GA
```

Of the 44 hostname-pinned `local-path-ovh` PVs across all five nodes, only **4
are real singletons** (no app-level replica to resync from). Everything else has
a CNPG `instances: 2` peer, a multi-replica StatefulSet peer, or
SeaweedFS-level (3-way) replication. So the strategy is per-PV:

- **App-replicated.** Drain forces app-level failover; after rename, delete the
  orphaned PVC; the controller (CNPG, StatefulSet, SeaweedFS) re-provisions the
  PVC on the renamed node and resyncs from its peer. Cost: a brief degraded
  state until resync completes.
- **Singleton, worth-keeping data.** Stop the workload, copy the data out of
  `/var/mnt/seaweedfs-data/local-path/<pvc-id>/` to a backup location, set PV
  reclaim policy to `Retain`, delete the PVC, do the rename, create a new
  PV+PVC with the new node affinity pointing at the same hostPath, restore the
  data, start the workload.
- **Singleton, disposable.** Just delete the workload + PVC; let it recreate
  empty (or not at all, if we're decommissioning the workload).

## Singleton inventory (2026-06-01)

| PV                               | Node                     | Size | Workload                | Handling                                                               | Backup state (2026-06-01)                                                   |
| -------------------------------- | ------------------------ | ---- | ----------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `gecko/gecko-root`               | `talos-ks-game-worker-0` | 20Gi | KubeVirt VM root disk   | **Delete the VM.** No backup. (User authorized.)                       | N/A                                                                         |
| `grocy-sf/grocy-config-ovh`      | `talos-kimsufi-worker-1` | 1Gi  | Grocy app config files  | Backup-and-restore from `grocy-config-ovh-backup` PVC (seaweedfs-ovh). | ✅ volsync configured (commit 39778904f); first sync 2026-06-01T23:25Z      |
| `grocy-vallejo/grocy-config-ovh` | `talos-kimsufi-worker-1` | 1Gi  | Grocy app config files  | Backup-and-restore from `grocy-config-ovh-backup` PVC (seaweedfs-ovh). | ✅ volsync configured (commit 39778904f); first sync 2026-06-01T23:24Z      |
| `tana-mcp/tana-mcp-config-ovh`   | `talos-kimsufi-worker-1` | 10Gi | Tana MCP config         | Backup-and-restore from `tana-mcp-config-backup` PVC (seaweedfs-ovh).  | ✅ pre-existing volsync (`cluster/k8s/agents/tana-mcp/volsync-backup.yaml`) |
| `swfs-bench/bench-ovh`           | `talos-kimsufi-worker-1` | 2Gi  | SeaweedFS bench fixture | Disposable — delete with workload.                                     | N/A                                                                         |

**Prerequisite for renaming `talos-kimsufi-worker-1`:** verify all three volsync
ReplicationSources have a recent `lastSyncTime` and non-zero `lastSyncDuration`
before draining. `kubectl get replicationsource -A | grep -E 'grocy|tana'`.

CNPG primaries that happen to live on a renamed node (e.g. `atuin-db-1` on
`talos-ks-game-worker-0`) are app-replicated; drain triggers failover before
the PV becomes orphaned. They are not in the singleton inventory.

## The irreducible cost

Kubernetes node names are immutable post-registration. To "rename" a node:

1. `kubectl cordon` + `kubectl drain` the old name.
2. Reconfigure Talos `HostnameConfig` to the new hostname (machine-config
   apply, then reboot).
3. Kubelet starts under the new hostname and registers as a new Node object.
   The old Node object stays until manually deleted.
4. PVs pinned to the old hostname are now orphaned. Per the strategy above:
   delete PVCs for app-replicated workloads; do the backup-and-restore dance
   for singletons.
5. `kubectl delete node <old-name>` once the new name is `Ready`.

**No `dd` reinstall, no OVH rescue boot is needed.** The Talos installer doesn't
care about hostname — that's a runtime concern. Skip the multi-step rescue dance
from `cluster/docs/kimsufi_provisioning.md` for this operation.

## Files that must change per node

Edits are made in the repo, committed, then applied via **direct `tofu plan`
followed by carefully-targeted `tofu apply`** — _not_ `bazel run
//cluster:bootstrap`, which auto-approves and applies broadly. Lesson
`cluster/docs/lessons_learned/2026_03_07_talosctl_upgrade_hostname_loss.md`
spells out why: "Never `tofu apply -auto-approve` with `-target` — review full
plan first."

**Per-node (in one commit, per node, before that node's drain):**

| File                                                          | Change                                                                                        |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `nebula-mesh.json`                                            | Rename the JSON key. `hosts."talos-ks-game-worker-1"` → `hosts."ovh-ns104963"`.               |
| `cluster/terraform/main/ovh-nodes.tf`                         | Update the `hostname` field inside `kimsufi_servers`/`kimsufi_cp_servers` for that node only. |
| `cluster/terraform/main/nebula.tf`                            | Update the logical-key → hostname map (`ks_game_worker1 = "ovh-ns104963"`).                   |
| `cluster/k8s/local-path-provisioner/helmrelease.yaml` (47–61) | Rename the corresponding `- node: ...` entry under `nodePathMap`.                             |

These four files must change as one logical unit per node — see the per-node
procedure below.

**Documentation / one-shot bulk edit (do all at once at the end):**

| File                                                                       | Change                                              |
| -------------------------------------------------------------------------- | --------------------------------------------------- |
| `cluster/README.md` (Node Types table, lines ~40-42)                       | Rename node entries.                                |
| `cluster/docs/kimsufi_provisioning.md` (provisioning table)                | Rename hostname column.                             |
| `cluster/docs/plan.md` (operational sequences)                             | Rename hostname mentions.                           |
| `cluster/docs/seaweedfs_trial_baseline.md`                                 | Rename node list.                                   |
| `cluster/docs/seaweedfs_csi_bench.md`                                      | Rename node list.                                   |
| `cluster/docs/troubleshooting.md` (lighthouse IPs section)                 | Rename hostname mentions.                           |
| `cluster/docs/lessons_learned/2026_05_24_vps_cp_nebula_cert_corruption.md` | Optional: add a "post-rename: see new name X" note. |
| `tf/gitops/dns-records/main.tf` (lines 22–26, 33–34)                       | Update inline comments only — IP data unchanged.    |
| `plans/nebula_mesh_ssot.md`                                                | Update host listing if still active.                |

**Files to LEAVE ALONE (historical snapshots):**

- `cluster/debug/2026-05-31-kimsufi-worker-1-notready/*.yaml` — captured kubectl
  output from a past incident. Frozen by design.
- `cluster/archive/2026_05_valkey_kimsufi_local_storage_migration.md`,
  `cluster/archive/2026_05_grocy_volsync_pvc_migration.md` — archived runbooks
  documenting past state.
- `cluster/debug/2026-05-18-rugged-nebula-roaming.md` — debug note.

## Terraform side effects

Updating `nebula-mesh.json` keys propagates through `local.talos_nebula_nodes` in
`cluster/terraform/main/persistent-auth.tf`. The per-node cert resource is
`for_each = local.talos_nebula_nodes`:

```hcl
resource "null_resource" "nebula_node_cert" {
  for_each = local.talos_nebula_nodes
  ...
  provisioner "local-exec" {
    command = <<-EOT
      nebula-cert sign -name "${each.key}" -ip "${each.value.ip}" ...
    EOT
  }
}
```

Renaming the for_each key triggers **destroy of the old cert + create of the new
cert**. The Nebula CA (`local_file.nebula_ca_crt`) is the only
`prevent_destroy`-protected piece in the cert chain; verify before running:

```bash
grep -B 3 'prevent_destroy' cluster/terraform/main/persistent-auth.tf
```

If `null_resource.nebula_node_cert` ever grows a `prevent_destroy`, this plan
needs revisiting.

### ⚠ Cross-node config drift to watch for

The Nebula machine-config patches reference all peers (lighthouses,
static*host_map). Renaming one host's JSON key can cause `nebula_machine_patches[*]`for \_other* nodes to update too, which would re-apply _their_ machine configs
when we apply tofu. Same risk with`local.kimsufi_eno1_peer_routes`in`ovh-nodes.tf` (it iterates every peer to compute per-node routes).

**Before every `tofu apply`** in this project:

1. Run `tofu plan -target=...` (no auto-approve) and read the full output.
2. Identify every resource the plan will create/update/destroy.
3. Confirm only the target node's `talos_machine_configuration_apply.kimsufi[<key>]`
   (or `kimsufi_cp[<key>]`) is in the changes.
4. If a sibling node's machine config also wants to update, **stop** and
   either:
   - Constrain the apply more (separate apply for the cert vs the
     machine-config, so the renamed node gets its new identity first
     without dragging siblings).
   - Patch the config (`apply_mode = "staged_if_needing_reboot"` already
     prevents accidental sibling reboot for the two `kimsufi_workerN`
     entries — but `ks_game_worker*` defaults to `auto`; check
     `cluster/terraform/main/ovh-nodes.tf` line ~41,52).

## Ordering (cost-based)

After categorization, order is by total handling cost (replicated-PV count +
singleton cost), not by role:

| #   | Node                     | New name       | PVs total | Singletons | Notes                                                          |
| --- | ------------------------ | -------------- | --------- | ---------- | -------------------------------------------------------------- |
| 1   | `talos-ks-game-worker-1` | `ovh-ns104963` | 3         | 0          | **Pilot.** True worker. Cleanest test of the mechanism.        |
| 2   | `talos-kimsufi-cp-0`     | `ovh-ns102453` | 1         | 0          | First CP — quorum-sensitive. SeaweedFS volume self-rebalances. |
| 3   | `talos-kimsufi-worker-0` | `ovh-ns103656` | 15        | 0          | Actually a CP. Lots of CNPG/Valkey delete-and-rebuilds.        |
| 4   | `talos-ks-game-worker-0` | `ovh-ns104952` | 5         | 1 (gecko)  | Delete the VM before drain.                                    |
| 5   | `talos-kimsufi-worker-1` | `ovh-ns103711` | 20        | 3          | Heaviest. Three backup-and-restore singletons.                 |

**Stop and reassess after step 1** (the pilot) before continuing. CP renames
(steps 2, 3) are quorum-gated — between each, `talosctl -n <surviving-cp> etcd
status` must show 3 healthy members.

## Per-node procedure (delete-and-rebuild variant)

For node `<OLD>` → `<NEW>`:

### 1. Pre-flight

```bash
# Cluster + etcd healthy
kubectl get nodes
talosctl -n 10.42.0.15 etcd status   # talos-kimsufi-cp-0 IP

# Pods on this node enumerated (so we know what's about to move)
kubectl get pods -A --field-selector spec.nodeName=<OLD> -o wide

# Pinned PVs on this node — bucket into replicated vs singleton
kubectl get pv -o json | jq -r '
  .items[]
  | select(.spec.storageClassName == "local-path-ovh")
  | select(.spec.nodeAffinity.required.nodeSelectorTerms[]?.matchExpressions[]?.values[]? == "<OLD>")
  | "\(.metadata.name)\t\(.spec.claimRef.namespace)/\(.spec.claimRef.name)\t\(.spec.capacity.storage)"'
```

Cross-check each pinned PV against the singleton inventory above. Any PV not
in the inventory should be app-replicated — verify with `kubectl get
cluster.postgresql.cnpg.io -n <ns> <name>` or the StatefulSet replica count.

### 2. Edit, commit, push

Touch only the four per-node files listed above. One commit:

```text
cluster: rename <OLD> to <NEW>

Role-neutral, verbatim OVH service name. See plans/rename_ovh_nodes_role_neutral.md.
```

Push so Flux can reconcile the local-path-provisioner change when ready.

### 3. Handle this node's singletons (if any) BEFORE drain

See the "Singleton sub-procedures" appendix. For each singleton on this node:

- If `Delete`: stop the workload, delete the PVC.
- If `Backup-and-restore`: stop workload → copy data off-host → set PV reclaim
  to `Retain` → delete PVC. Data must be safe in a backup location before
  drain.

### 4. Drain

```bash
kubectl cordon <OLD>
kubectl drain <OLD> --ignore-daemonsets --delete-emptydir-data --timeout=10m
```

If drain stalls on a PDB, identify the workload and either bump replicas
elsewhere or accept the disruption (`--disable-eviction`) — don't extend the
timeout. CNPG primaries on this node failover automatically when drained;
watch for the new primary in `kubectl get cluster.postgresql.cnpg.io -A`.

### 5. Tofu plan — inspect changed entities

```bash
cd cluster/terraform/main
tofu plan \
  -target='null_resource.nebula_node_cert' \
  -target='talos_machine_configuration_apply.kimsufi["<tfkey>"]' \
  -out=/tmp/rename-<tfkey>.tfplan
```

Where `<tfkey>` is the local-map key: `kimsufi_worker0`, `kimsufi_worker1`,
`ks_game_worker0`, `ks_game_worker1`, or use
`talos_machine_configuration_apply.kimsufi_cp["kimsufi_cp0"]` for the CP node.

**Read the plan output carefully.** Verify:

- Exactly one `null_resource.nebula_node_cert` destroy (old key) + one create
  (new key).
- Exactly one `talos_machine_configuration_apply` update — for the target node
  only.
- No other `talos_machine_configuration_apply.*` entries in the changes.
- No `ovh_dedicated_server.*` in the changes (would mean we touched something
  we shouldn't).
- No `prevent_destroy` resources flagged for destroy.

If a sibling node's `talos_machine_configuration_apply` shows in the plan,
stop. Inspect: it's likely the `nebula_machine_patches` or
`kimsufi_eno1_peer_routes` cascade. Decide whether to:

- Apply the cert change alone first (`-target=null_resource.nebula_node_cert`)
  so subsequent siblings see the updated peer map but don't reboot yet, then
  apply per-node machine config in sequence.
- Or accept that siblings will get a no-op-or-staged reapply (since
  `kimsufi_worker[01]` use `apply_mode = "staged_if_needing_reboot"`, they
  won't actually reboot for a route-table update — but `ks_game_worker*`
  default to `auto`, so check there).

### 6. Tofu apply (the inspected plan)

Only after the plan has been read and approved:

```bash
tofu apply /tmp/rename-<tfkey>.tfplan
```

No `-auto-approve` flag, no naked `-target=`. The plan file pins exactly what
was reviewed.

### 7. Wait for new node Ready

```bash
kubectl get nodes -w | grep -E '<NEW>|<OLD>'
```

Talos `apply-config` rebooted the node (assuming the config delta required it,
which a hostname change does). Kubelet starts under the new hostname,
registers a new Node object, Cilium pod restarts on the new name. The old
Node object will linger as `NotReady`. Delete it once `<NEW>` is `Ready`:

```bash
kubectl delete node <OLD>
```

### 8. Delete stale PVCs for app-replicated workloads

For every pinned PV on `<OLD>` that we did NOT pre-handle as a singleton
(i.e. all app-replicated ones), delete the PVC. The controller (CNPG,
StatefulSet) sees its claim missing and recreates it; local-path-provisioner
provisions a fresh empty path on `<NEW>`; the app resyncs from its peer.

```bash
# Show the list one more time
kubectl get pv -o json | jq -r '
  .items[]
  | select(.spec.nodeAffinity.required.nodeSelectorTerms[]?.matchExpressions[]?.values[]? == "<OLD>")
  | "\(.spec.claimRef.namespace)/\(.spec.claimRef.name)"'

# Delete each PVC (skips singletons that you already pre-handled)
kubectl delete pvc -n <ns> <pvc>
```

The PVs themselves can be deleted after the PVCs (or left to garbage-collect;
local-path-provisioner cleans up on PV deletion).

Watch the controllers re-provision:

```bash
kubectl get cluster.postgresql.cnpg.io -A -w           # CNPG instance resync
kubectl get statefulset -A -w                          # StatefulSet replica recreation
```

### 9. Restore singletons (if any)

See the appendix. For each pre-handled singleton, create a new PV+PVC with
the new node affinity and restore the data file before starting the workload.

### 10. Reconcile Flux for local-path-provisioner

```bash
flux reconcile helmrelease local-path-provisioner -n local-path-storage
```

This picks up the renamed `nodePathMap` entry. New PVCs in the OVH zone now
route to `<NEW>`.

### 11. Verify

```bash
# Node Ready, taints sane
kubectl get node <NEW> -o yaml | yq '.spec.taints, .status.conditions'

# No stuck pods
kubectl get pods -A -o wide | grep -v Running | grep -v Completed

# All local-path-ovh PVCs bound
kubectl get pvc -A -o json | jq '.items[] | select(.spec.storageClassName == "local-path-ovh") | select(.status.phase != "Bound") | "\(.metadata.namespace)/\(.metadata.name): \(.status.phase)"'

# Nebula peering working
talosctl -n <IP-of-new-node> service nebula status

# Gatus health checks green
kubectl get -n gatus statefulsets
```

For CP renames, also:

```bash
talosctl -n <surviving-CP-IP> etcd status   # 3 members, all healthy
kubectl get --raw='/livez?verbose'           # API healthy through HAProxy
```

### 12. Rollback boundary

Rollback is possible **only before** kubelet re-registers under the new name
(roughly, before step 7). If the tofu apply failed mid-way or the node fails
to come back Ready:

1. Revert the four-file commit (`git revert`, push).
2. Re-run `tofu plan` + `tofu apply` retargeted at the same resources — this
   regenerates the cert and config under the OLD name.
3. `kubectl uncordon <OLD>` if it's still in the API server.

Once `<NEW>` is Ready and old PVCs have been deleted, **do not** roll back —
the apps have already started rebuilding their state under the new name. Fix
forward instead.

## Singleton sub-procedures

### gecko/gecko-root (delete-the-VM path)

User authorization (2026-06-01): the `gecko` VM is deletable, no backup
needed. Run before draining `talos-ks-game-worker-0`:

```bash
kubectl -n gecko delete virtualmachine gecko
kubectl -n gecko delete pvc gecko-root   # if still hanging around
```

(KubeVirt cleans up the virt-launcher pod automatically when the
VirtualMachine is deleted.)

### Grocy / Tana-MCP configs (volsync backup-and-restore path)

All three namespaces have a working `volsync` ReplicationSource as of
2026-06-01 (see Singleton inventory). The restore mechanism is symmetric:
the backup PVC (`*-config-ovh-backup` on `seaweedfs-ovh`) is the source of
truth, and a `ReplicationDestination` exists in each namespace that can
write back into the live PVC.

Per singleton (template `<NS>/<PVC>` where source PVC is
`<NS>/grocy-config-ovh` or `tana-mcp/tana-mcp-config-ovh`):

#### Before draining the host node

```bash
# 1. Force a fresh backup so we have the latest data, not 6h-stale.
#    Volsync trigger.manual conflicts with trigger.schedule, so pause the
#    schedule, run manual, then restore the schedule afterward.
kubectl -n <NS> patch replicationsource <RS-NAME> --type=merge \
  -p '{"spec":{"trigger":{"manual":"pre-rename-2026-06-01"}}}'
# 2. Wait for the manual sync to complete:
until kubectl -n <NS> get replicationsource <RS-NAME> \
    -o jsonpath='{.status.lastManualSync}' | grep -q 'pre-rename-2026-06-01'; do
  sleep 5
done
# 3. Restore the schedule.
kubectl -n <NS> patch replicationsource <RS-NAME> --type=merge \
  -p '{"spec":{"trigger":{"schedule":"<original-cron>"}}}'

# 4. Scale workload to 0 (so no writes during/after rename).
kubectl -n <NS> scale deployment/<workload> --replicas=0

# 5. Delete the orphaned PVC. The live data path under
#    /var/mnt/seaweedfs-data/local-path/<pv-uid>/ on the old node is
#    irrelevant — we're restoring from the backup PVC, which is on
#    SeaweedFS and survives the rename.
kubectl -n <NS> delete pvc <PVC>
```

#### After the node rename completes

```bash
# 6. The local-path provisioner re-provisions an empty PVC on the renamed
#    node when the workload's StatefulSet/Deployment tries to mount it.
#    For grocy/tana, the PVC manifest is part of Flux. Re-create by
#    reconciling the namespace's kustomization:
flux reconcile kustomization <KS-NAME>

# 7. Trigger a one-shot restore from the backup PVC into the new live PVC.
#    Volsync's existing ReplicationDestination accepts a manual trigger:
kubectl -n <NS> patch replicationdestination <RD-NAME> --type=merge \
  -p '{"spec":{"trigger":{"manual":"restore-2026-06-01"}}}'

# 8. Wait for completion.
until kubectl -n <NS> get replicationdestination <RD-NAME> \
    -o jsonpath='{.status.lastManualSync}' | grep -q 'restore-2026-06-01'; do
  sleep 5
done

# 9. Scale workload back up. Verify config is present.
kubectl -n <NS> scale deployment/<workload> --replicas=1
```

Note: the existing `ReplicationDestination` resources have
`destinationPVC: grocy-config-ovh-backup` (i.e., they restore TO the backup
PVC, not FROM it). For step 7 to work as a _restore_, we need a temporary
RD pointing the other way (`destinationPVC: grocy-config-ovh`) OR we just
mount the backup PVC into a one-shot Job that `rsync`s its content into a
freshly-provisioned `grocy-config-ovh`. Verify the right shape at restore
time; this plan deliberately leaves the implementation open since the
restore is only run once per rename.

## Risks called out

- **Cross-node config drift.** See "Cross-node config drift to watch for"
  above. Inspect every `tofu plan` for unintended sibling-node updates.
- **Quorum during CP renames.** Cluster has 3 CPs (`talos-kimsufi-cp-0` plus
  the two mislabeled `kimsufi-worker-{0,1}`). Renaming one at a time is fine;
  renaming two concurrently breaks the cluster.
- **Nebula cert race.** Tofu apply destroys the old cert and creates the new
  one. The new cert must be in the machine config before the node reboots,
  or Nebula won't come up. Talos `apply-config --mode=reboot` orders this
  correctly (config commit precedes the reboot).
- **podCIDR reassignment.** Per lesson
  `2026_03_07_talosctl_upgrade_hostname_loss.md`, when a node re-registers
  under a new name, kube-controller-manager assigns a fresh podCIDR. After
  Cilium restart, daemonset pods carrying old-CIDR IPs lose connectivity.
  Mitigation: after step 7, scan for pods with IPs outside the new node's
  `spec.podCIDR` and `kubectl delete pod` them.
- **CNPG primary thrash.** If the renamed node hosts a primary (e.g.
  `atuin-db-1`), drain triggers failover and then the rebuild lands a new
  replica back on the renamed node. Watch for promote/demote storms in CNPG
  cluster events.
- **SeaweedFS volume rebalance.** When `mount0-seaweedfs-volume-N` PVCs get
  deleted on the renamed node and recreated empty, SeaweedFS replicates the
  volumes back from the other two replicas. ~1.8TB per node; can take hours.
  Drain during low-traffic windows.
- **OVH IP reassignment.** OVH bare-metal keeps the same public IP across
  reinstalls and reboots. Verify by comparing
  `data.ovh_dedicated_server.kimsufi` IPs before/after Tofu apply.
- **Hostname dots get split by Talos.** Pilot lesson 2026-06-01: Talos's
  `HostnameConfig` controller treats a hostname with dots as `host.domain` and
  writes only the (short) hostname to the kernel. Kubelet then registers the
  K8s node under the short name, desyncing from any repo file that referenced
  the full dotted string. Use single DNS labels. Format assert enforces this
  in `cluster/validation/test_nebula_mesh.py::test_host_names_have_no_dots`.

## Follow-up: rekey Terraform local-map keys

Separate from the hostname rename, the Terraform local-map keys
(`ks_game_worker1`, `kimsufi_worker0`, etc.) still leak role/index. After
all five hostnames are renamed, do a closeout pass that renames the
local-map keys to match the new hostnames (e.g. `ovh_ns104963`).

Changing the `for_each` key naively makes Terraform plan a destroy+recreate
of every resource keyed on it — including `null_resource.install_talos_kimsufi`,
which triggers a fresh `dd` install via OVH rescue boot. Avoid that by
using `tofu state mv` for each resource keyed on the old name:

```
tofu state mv 'data.ovh_dedicated_server.kimsufi["ks_game_worker1"]' \
              'data.ovh_dedicated_server.kimsufi["ovh_ns104963"]'
# Repeat for ovh_dedicated_server.kimsufi, ovh_dedicated_server_update.kimsufi_*,
# ovh_dedicated_server_reboot_task.kimsufi_*, null_resource.install_talos_kimsufi,
# talos_machine_configuration_apply.kimsufi. ~8 state-mv operations per node.
```

After the moves, `tofu plan` should be a no-op. Run as its own commit per
node so any drift surfaces immediately.

## Closeout

Once all five nodes are renamed and stable for 24 hours:

1. Bulk doc edit: README, kimsufi*provisioning.md, plan.md, seaweedfs*\* docs,
   troubleshooting.md, dns-records main.tf comments.
2. Tombstone this plan: append a "DONE <date>" note at the top and leave for
   one release cycle, then delete per the `plans/` convention.
3. Add a brief lesson under `cluster/docs/lessons_learned/` covering anything
   surprising encountered during the renames (especially: how the
   delete-and-rebuild dance interacted with SeaweedFS volume rebalance, and
   any cross-node config drift seen at plan time).
