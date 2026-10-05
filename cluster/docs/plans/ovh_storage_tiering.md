# Plan: rename the remaining OVH SSD data-disk mounts

Rename the two SSD control-plane nodes (`104952`, `104963`) from
`/var/mnt/seaweedfs-data` to `/var/mnt/local-path-ovh-ssd`. The three-server SSD
SeaweedFS group allows evacuation while retaining two-copy durability. The rename
is cosmetic and can wait for a useful maintenance window.

Reference material:
<../lessons_learned/2026_07_04_seaweedfs_volumetopology_and_operator.md>,
<../lessons_learned/2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md>,
<../lessons_learned/2026_07_05_nebula_overlay_packet_loss_investigation.md> (issue #2917 — why
OVH inter-node moves are slow/flaky), <../runbooks/seaweedfs_pvc_storageclass_migration.md>
(reusable PVC storage-class migration), <../../skills/cnpg_region_switch/RUNBOOK.md>.

## The 2 SSD nodes

| Node           | Box     | Role          | etcd disk  | Data disk (rename target)              |
| -------------- | ------- | ------------- | ---------- | -------------------------------------- |
| `ovh-ns104952` | KS-GAME | control-plane | NVMe#1 SSD | NVMe#2 → `/var/mnt/local-path-ovh-ssd` |
| `ovh-ns104963` | KS-GAME | control-plane | NVMe#1 SSD | NVMe#2 → `/var/mnt/local-path-ovh-ssd` |

- **etcd rides NVMe#1** (the install disk), not the data disk — the rename repartitions NVMe#2
  only, no reboot, etcd untouched (as proven on the `103656` anchor).
- **SSD headroom is generous** (~350 GiB free each). What lives on NVMe#2 is movable bulk (the
  `ssd` SeaweedFS volume server, VM roots `gecko`/`agent-box`) + a few-GiB small-CNPG footprint;
  no hot DB depends on the data disk staying put.

## Rename mechanism

Two per-node surfaces flip in the **same commit**, per node:

1. Talos: the UserVolume name is `contains(data_disk_mount_renamed_nodes, node) ?
"local-path-ovh-${storage_tier}" : "seaweedfs-data"` (<../../terraform/main/ovh-nodes.tf>) —
   an opt-in `toset()`; add one hostname to roll that node. **Renaming a UserVolume
   repartitions/wipes that disk.**
2. local-path: that node's `nodePathMap` entry in
   <../../cdk8s/local_path_provisioner.py> → `/var/mnt/local-path-ovh-ssd/local-path`.

If (1) renames but (2) lags, new PVCs land on the root filesystem — so the node is
**cordoned+drained** across the wipe (nothing provisions on it until both surfaces are on the
new path and it's uncordoned). The wipe is safe because nearly all data is replicated or
rebuildable (CNPG re-clones, SeaweedFS re-replicates, Loki/Mimir/Tempo are S3-backed, Valkey is
cache); the exceptions are handled explicitly (G-losable below).

**Application discipline:** never `bazel run //cluster:bootstrap` (hits all nodes) — `tofu plan`
against `cluster/terraform/main/`, read the diff, `tofu apply -target=<addr>` for the one node,
re-plan to confirm the residual is empty.

## Evacuation and cutover constraints

The SSD tier has three servers at repl `001`; move one node at a time so volumes
retain two-copy durability on the other two servers. Back up SSD SeaweedFS data
before maintenance, verify replication after each node, and wait for SSD-pinned CNPG
instances to re-clone and return to streaming. These are control planes, so gate each
drain on healthy etcd and do not cut over Forgejo without approval.

## Procedure the SSD rename reuses (proven on the HDD roll)

### SeaweedFS re-replication is scheduled and copy-only

The SeaweedFS master does not self-heal. The operator-managed `replication-repair` AdminScript
runs `volume.fix.replication -apply -doDelete=false` hourly under an admin **`lock`**. This
restores missing copies but deliberately does not delete excess or misplaced replicas. If a
temporarily unavailable volume server returns after its data was copied elsewhere, expect an
over-replication alert and review cleanup separately. The schedule is periodic, not gated on an
alert duration, so even a brief mismatch present at the scheduled time can start a copy.

For immediate/manual repair, use the same copy-only command from a master pod:

```bash
# weed shell reads commands on stdin (no `-c` flag on this build)
printf 'volume.fix.replication -collectionPattern=* -apply=false\n' | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell   # dry-run
printf 'lock\nvolume.fix.replication -apply -doDelete=false -maxParallelization=2 -maxParallelizationPerServer=1\nunlock\n' | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell
```

`volume.fix.replication` defaults to also deleting surplus/misplaced replicas when applying;
keep `-doDelete=false` unless a human has reviewed that cleanup (the boolean value must be
attached to the flag). It fixes **one** missing replica per volume per run and needs a target
server with a **free volume slot** — a server's
capacity is a slot count (disk ÷ `volumeSizeLimitMB`, 16 GB here; the hdd group also carries
`maxVolumeCounts: 400` + `minFreeSpacePercent: 10` overcommit), so a slot-full server can't
receive replicas even with disk free.

Before any planned volume-server outage, evacuation, or storage rename, set
`spec.suspend: true` on `replication-repair` in Git and reconcile it. Suspending the CronJob
prevents new runs but does not stop an active Job; wait for active Jobs to finish before
starting the maintenance. Restore `suspend: false` after the server is healthy and placement is
stable. The **`SeaweedFSReplicaPlacementMismatch`** alert
(<../../cdk8s/seaweedfs/monitoring.py>) remains enabled to catch stalled repairs
and over-/misplaced replicas.

### Refresh FUSE clients before deleting a volume server (gotcha)

`weed mount` clients cache volume locations and only re-resolve off an **alive** server's 404; a
**deleted** server (DNS `no such host`) leaves the cache stale → I/O errors / SIGBUS. So make
server deletion the **last, quiescence-gated** step: move data off (server stays running, empty)
→ verify 2-copy (G-swfs) → refresh clients while the emptied server is still alive → confirm
idle → **only then** delete. Full RCA:
<../lessons_learned/2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md>.

### Operational lessons from the HDD roll

- **`volumeServer.evacuate` aborts on the first transient gRPC error** (the lossy overlay,
  #2917). Wrap it in a **retry loop** that re-runs until the server shows 0 volumes.
- **The last volume often won't evacuate** — it ended up over-replicated (3 copies) from
  interrupted moves, so `evacuate` can't move it. **Safe to wipe anyway** (the wipe drops the
  redundant copy → back to 2), or force one replica off with `volume.delete -volumeId X -node
<server>` (a full/`ReadOnly:true` volume may ignore it).
- **CNPG re-clone** = `kubectl delete pvc <inst> --wait=false; kubectl delete pod <inst>
--force` → the operator re-clones onto the fresh disk from the surviving instance.
- **Post-wipe recovery, per node:** reconcile `local-path-provisioner` (nodePathMap flip) →
  uncordon → CNPG re-clones → delete-and-recreate the disposable STS PVCs (Valkey caches,
  `loki-write`/`mimir-ingester`/`alertmanager`, SeaweedFS volume-server PVC) → GC the Released
  old-path PVs. The ConfigMap is `local-path-storage/local-path-config` (not
  `-provisioner-config`).

### Health gates (fence every destructive step)

**G-all** = all green; hold before wiping a node and after each node op.

- **G-etcd** — every member `HEALTH OK`, same DB revision/raft index, no alarms/leader churn,
  `ControlPlaneLeasePutLatency*` not firing. Both SSD nodes are CPs, so their drains touch
  quorum directly — a wobble is a stop signal.
- **G-nodes** — all `Ready` except the one intentionally cordoned.
- **G-swfs** — dry-run `volume.fix.replication` returns empty, `volume.list`/`cluster.check`
  clean, filer up. Gate every volume-server wipe before (source copy exists) and after
  (re-replicated back to 2).
- **G-cnpg** — each affected cluster healthy, all instances `streaming`, lag ≈ 0. Gate on ≥1
  healthy instance elsewhere as re-clone source; after: re-cloned instance `streaming`.
- **G-flux** — kustomizations/helmreleases `Ready` (allow known-suspended).
- **G-public** — Gatus green; `git.allegedly.works`/`auth.allegedly.works` reachable; a test
  `git clone` succeeds.
- **G-losable** (before wiping node `N`) — list local-path PVs pinned to `N`; the only
  non-replicated ones may be the pre-accepted disposables (`gecko/gecko-root`,
  `agent-box/agent-box-root`, `codex-nix-pod/*`). **Halt on anything else** so a newly-created
  single-copy PVC is never destroyed silently.

```bash
node=<N>
kubectl get pv -o json | jq -r --arg n "$node" '
  .items[]
  | select((.spec.storageClassName // "") | test("local-path"))
  | select([ .spec.nodeAffinity.required.nodeSelectorTerms[]?.matchExpressions[]?
             | select(.key == "kubernetes.io/hostname") | .values[] ] | index($n))
  | "\(.spec.claimRef.namespace)/\(.spec.claimRef.name)\t\(.spec.storageClassName)"'
```

## Control-plane membership checklist (any CP add/remove)

CP membership is the per-host `role` in `nebula-mesh.json` (leave `lighthouse`/`relay`/
`cert_groups` alone): the Talos machine type in `ovh-nodes.tf`, the etcd metrics scrape
EndpointSlice (`cluster/generated/platform-monitoring/platform-monitoring.k8s.yaml`, or
`ControlPlaneLeasePutLatency` alerts point nowhere) and the `api.allegedly.works` A records
(`cluster/generated/external-dns-records/`) are all derived from it — `bb run //cluster/cdk8s:generate_manifests`
after the edit. Any control-plane add/remove also updates in the same change:

- `cluster/terraform/main/infrastructure.tf` — `primary_controlplane_ip` + the
  `talos_machine_bootstrap`/`talos_cluster_kubeconfig` `ignore_changes` guards, if the anchor moves.
- `cluster/README.md` — the "Node Types" table (human-facing CP/worker roster).
