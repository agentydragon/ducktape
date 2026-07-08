# Plan: OVH data-disk mount rename (storage tiering — Stage 2 remainder)

**Status: active — only the 2 SSD control-plane nodes remain.** The etcd-onto-NVMe
control-plane reshuffle and all three KS-5 HDD-node data-disk renames are done (2026-07-05):
control plane `{103656, 104952, 104963}`, etcd on 2× KS-GAME NVMe + the KS-5 anchor `103656`;
`102453`/`103711` are workers. The foundation is live — media-scoped StorageClasses
(`local-path-ovh-{hdd,ssd}`), the SeaweedFS `volumeTopology` layer (`hdd` 3× KS-5, `ssd` 2×
KS-GAME; operator 1.0.30), Forgejo git + both SSD-destined DBs on SSD, and the per-node rename
mechanism (`data_disk_mount_renamed_nodes` opt-in + `nodePathMap` flip).

**What's left:** rename the **2 SSD control-plane nodes** (`104952`, `104963`) off
`/var/mnt/seaweedfs-data` → `/var/mnt/local-path-ovh-ssd` — harder because the SSD SeaweedFS
tier has only 2 servers at repl `001`, so there's no evacuation buffer (see below). Then an
optional **Stage 3** (3rd NVMe box). The rename is **cosmetic** (naming the mount for the
disk/tier instead of the leftover `seaweedfs-data`), so deferring either is fine.

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

## The rename mechanism (built)

Two per-node surfaces flip in the **same commit**, per node:

1. Talos: the UserVolume name is `contains(data_disk_mount_renamed_nodes, node) ?
"local-path-ovh-${storage_tier}" : "seaweedfs-data"` (<../../terraform/main/ovh-nodes.tf>) —
   an opt-in `toset()`; add one hostname to roll that node. **Renaming a UserVolume
   repartitions/wipes that disk.**
2. local-path: that node's `nodePathMap` entry in
   <../../k8s/local-path-provisioner/helmrelease.yaml> → `/var/mnt/local-path-ovh-ssd/local-path`.

If (1) renames but (2) lags, new PVCs land on the root filesystem — so the node is
**cordoned+drained** across the wipe (nothing provisions on it until both surfaces are on the
new path and it's uncordoned). The wipe is safe because nearly all data is replicated or
rebuildable (CNPG re-clones, SeaweedFS re-replicates, Loki/Mimir/Tempo are S3-backed, Valkey is
cache); the exceptions are handled explicitly (G-losable below).

**Application discipline:** never `bazel run //cluster:bootstrap` (hits all nodes) — `tofu plan`
against `cluster/terraform/main/`, read the diff, `tofu apply -target=<addr>` for the one node,
re-plan to confirm the residual is empty.

## The hard part: no evacuation buffer

The HDD tier had 3 servers, so a node's volume server could evacuate to the other two and stay
2-copy. The SSD tier has only **2** servers at repl `001` — a volume's two copies are on those
two servers, so you can't evacuate one and stay 2-copy.

**Accepted approach (decided 2026-07-05, user-OK'd): ride a bounded single-copy window, one
node at a time.**

- **Back up first** — insurance for the one fatal case (the _surviving_ SSD node dying mid
  window): snapshot the SSD SeaweedFS data (Forgejo git) to **off the SSD tier** (HDD or
  external). 1-copy is accepted; a double-failure with no backup is not.
- **One SSD node at a time.** Wipe ssd-0 → its volumes ride 1 copy on ssd-1 → node returns →
  `volume.fix.replication -apply` back to 2 → **verify 2-copy (G-swfs) before touching ssd-1.**
- **SSD-pinned CNPG** (`forgejo-db-ssd`, `seaweedfs-filer-db-ssd`) likewise ride a
  single-healthy-instance window — the re-clone can't land until the drained node returns (no
  third SSD node). Same one-at-a-time gating.
- These are **control planes** — respect **G-etcd** on the drain (data-disk repartition is
  NVMe#2, no reboot, etcd on NVMe#1 untouched). **No Forgejo downtime without approval** — the
  SSD tier is Forgejo git's hot tier, so confirm before the cutover.
- **Alternative — do Stage 3 first** (3rd NVMe box → `ssd` group `replicas: 3`): then the SSD
  nodes evacuate exactly like the HDD ones did, zero single-copy window. Costs hardware, buys
  back the safety margin.

**Finalize (after both SSD nodes):** every OVH data disk at `/var/mnt/local-path-ovh-{hdd,ssd}`,
`nodePathMap` pointing there, `seaweedfs-data` name retired; re-check Nebula lighthouse
placement.

## Procedure the SSD rename reuses (proven on the HDD roll)

### SeaweedFS re-replication is manual

SeaweedFS does **not** auto-re-replicate (no operator/master heal). Restore replica count with a
`weed shell` command from a master pod; mutating commands need an admin **`lock`** first:

```bash
# weed shell reads commands on stdin (no `-c` flag on this build)
printf 'volume.fix.replication\n' | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell   # dry-run
printf 'lock\nvolume.fix.replication -apply\nunlock\n' | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell
```

`-apply` fixes **one** missing replica per volume per run and needs a target server with a
**free volume slot** — a server's capacity is a slot count (disk ÷ `volumeSizeLimitMB`, 16 GB
here; the hdd group also carries `maxVolumeCounts: 300` + `minFreeSpacePercent: 10`
overcommit), so a slot-full server can't receive replicas even with disk free. The
**`SeaweedFSReplicaPlacementMismatch`** alert
(<../../k8s/seaweedfs/monitoring/prometheusrule.yaml>) surfaces under-replication; keep it
alert-only (never auto-heal during a rename — re-replication must stay deliberate and gated).

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

Changing which nodes are control-plane is **not** just the Terraform `role` field. The Stage-2
reshuffle flipped the roles but left downstream rosters on the old CP set, which broke devel
(`test_nebula_mesh`/`test_dns_records`) and silently mis-pointed live etcd metrics + the
`api.allegedly.works` record. Any CP add/remove (Stage 3's `103656` removal + new-box addition)
must update **all** of these in the same change — the two validation tests enforce the last three:

- `cluster/terraform/main/ovh-nodes.tf` — the node `role` field (actual CP membership).
- `cluster/terraform/main/infrastructure.tf` — `primary_controlplane_ip` + the
  `talos_machine_bootstrap`/`talos_cluster_kubeconfig` `ignore_changes` guards, if the anchor moves.
- `nebula-mesh.json` — the per-host `role` (leave `lighthouse`/`relay`/`cert_groups` alone).
- `cluster/k8s/monitoring/etcd/endpoints.yaml` — the etcd metrics scrape EndpointSlice (exactly
  the nodes running etcd, or `ControlPlaneLeasePutLatency` alerts point nowhere).
- `tf/gitops/dns-records/main.tf` — `kube_api_ips` (the `api.allegedly.works` A records must be
  the CPs' public IPs).
- `cluster/README.md` — the "Node Types" table (human-facing CP/worker roster).

## Stage 3 — third SSD node (optional, future)

Buy 1 NVMe OVH box; add as control-plane (learner → voter), then remove `103656` (the last HDD
etcd seat) → all-NVMe 3-member quorum; bump the SeaweedFS `ssd` group to `replicas: 3`. Same
one-member-at-a-time **G-etcd** discipline. Apply the CP-membership checklist above for both the
add and the removal. Doing this **before** the SSD-node rename removes the single-copy window
entirely.
