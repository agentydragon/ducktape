# OVH storage tiering: HDD vs SSD local-path classes

Splits OVH node-local storage into media-scoped StorageClasses so fsync-sensitive
data (git, etcd-adjacent DBs) can be placed on the KS-GAME NVMe while bulk blob
data stays on the KS-5 HDDs. Motivated by slow Forgejo (SeaweedFS-over-FUSE on
7200rpm HDD) degrading the Haku dashboard, and by etcd HDD I/O contention
(<lessons_learned/2026_06_19_etcd_hdd_io_contention.md>).

## Hardware recap

| Nodes                                          | Box                  | Data disk (`/var/mnt/seaweedfs-data`) | `storage-tier` |
| ---------------------------------------------- | -------------------- | ------------------------------------- | -------------- |
| `ovh-ns102453`, `ovh-ns103656`, `ovh-ns103711` | KS-5 (control plane) | `/dev/sdb`, 2 TB 7200rpm **HDD**      | `hdd`          |
| `ovh-ns104952`, `ovh-ns104963`                 | KS-GAME (worker)     | 2nd Intel NVMe ~450 GB **SSD**        | `ssd`          |

Only a few hundred GB of fsync-sensitive data needs SSD (etcd DB ~76 MB, Forgejo
git PVC 50 GB, hot CNPG DBs). The ~1.8 TB SeaweedFS bulk (LFS, registry, Mimir,
Loki, backups) is append-heavy and stays on HDD.

## Why not "StorageClass + PVC size" for placement

`local-path-ovh` was media-blind: `WaitForFirstConsumer` + `allowedTopologies:
zone=hil-ovh` + a `nodePathMap` pointing every OVH node at
`/var/mnt/seaweedfs-data/local-path`. That path is HDD on KS-5 and NVMe on
KS-GAME, so the **only** thing deciding media was which node the pod scheduled
on. Size is not a placement lever (local-path never checks free space). And a
SeaweedFS `-disk=ssd` tag is **unverified** — a volume server that drifts onto a
KS-5 would advertise `ssd` to the master while writing to rust. So media must be
a hard, explicit constraint.

## Design: media as a node label + allowedTopologies

`storage-tier` node label, set in <../terraform/main/ovh-nodes.tf> and keyed to
the node's actual data-disk hardware (a per-node `storage_tier` field), **not**
its role — so it stays correct when a KS-GAME node is later re-designated
control-plane (Stage 2).

Three StorageClasses (<../k8s/local-path-provisioner/>), all sharing the one
provisioner + nodePathMap; media differs only by `allowedTopologies`. Each class
ANDs two keys in the same topology term — `topology.kubernetes.io/zone=hil-ovh`
(keeps the class OVH-scoped, so it never places on a non-OVH SSD node such as
wyrm2) **and** `storage-tier`:

- `local-path-ovh-hdd` → zone `hil-ovh` + `storage-tier=hdd` (KS-5)
- `local-path-ovh-ssd` → zone `hil-ovh` + `storage-tier=ssd` (KS-GAME)
- `local-path-ovh` → deprecated alias, re-pinned to the same as `-hdd`

Because binding follows the pod under `WaitForFirstConsumer`, a PVC on the SSD
class can only bind where an OVH SSD node is schedulable; if none is, the PVC
stays **Pending (loud)** instead of silently landing on HDD.

### Robustness layers

1. **Now (this change):** hard `storage-tier` labels + media-scoped SCs. A
   consumer of `-ssd` cannot provision on HDD.
2. **Now:** the SeaweedFS SSD volume-topology group (below) also carries a hard
   `required` nodeAffinity on `storage-tier=ssd` — belt-and-suspenders so the
   pod, not just the volume, is media-locked.

The **hard media gate is layers 1–2** (allowedTopologies + the hardware-keyed
`storage-tier` label). The data-disk mount rename below is **naming/clarity, not
a third enforcement layer**: local-path-provisioner's `nodePathMap` is keyed by
node, not by StorageClass, so a mis-scheduled pod would still provision at
whatever path that node has — it does not make the SSD tier "physically
impossible" on a KS-5.

**Guardrail:** since `-disk` tags are unverified, alert if a SeaweedFS `ssd`
volume-server pod is not on a `storage-tier=ssd` node.

## Data-disk mount rename (fixing the backwards `/var/mnt/seaweedfs-data` path)

Today the OVH data disk is a Talos UserVolume named `seaweedfs-data`, and the
**general** local-path-provisioner is nested under it at
`/var/mnt/seaweedfs-data/local-path` — so every OVH local-path PVC (~40: CNPG
DBs, Valkey, Loki/Mimir/Tempo, VM disks…) lives inside a mount named after one
tenant. SeaweedFS's own volume server is itself just a `local-path` PVC
(`/var/mnt/seaweedfs-data/local-path/pvc-<id>/`), so the disk is named after a
tenant that is three levels down. It's a leftover from the SeaweedFS trial.

**Target:** name the mount for the disk/tier, SeaweedFS is one PVC of the class:

- KS-5: UserVolume `local-path-ovh-hdd` → `/var/mnt/local-path-ovh-hdd`
- KS-GAME: UserVolume `local-path-ovh-ssd` → `/var/mnt/local-path-ovh-ssd`
  (`diskSelector` on the NVMe serial)

Each node's `nodePathMap` points at its own mount; SeaweedFS volume servers and
everything else become plain `pvc-*` dirs under it.

### Why it's a wipe, and why that's OK

Renaming a Talos UserVolume repartitions (wipes) the disk. Both OVH data disks
are **full of live local-path data** — the KS-GAME NVMe holds ~15 CNPG instances
(incl. `forgejo-db`, `authentik-db`, `langfuse-db`), `seaweedfs-volume-0`, Loki
backends, Tempo, Mimir compactor/store-gateway, Valkey, and a couple of VM disks.
But nearly all of it is **replicated or rebuildable**: CNPG re-clones a wiped
instance from its replica; SeaweedFS re-replicates (`001`); Loki/Mimir/Tempo
local PVCs are WAL/cache backed by SeaweedFS S3; Valkey is cache. So a **rolling,
one-node-at-a-time wipe** is safe and not painful.

### Rules

1. **One node at a time — never both KS-GAME together.** Many CNPG clusters have
   *both* instances on the two KS-GAME nodes (`forgejo-db`, `airlock-db`,
   `atuin`, `langfuse`, …); wiping both at once loses them. Wipe one, wait for
   CNPG re-clone + SeaweedFS re-replication onto the survivor, then the next.
2. **Do NOT apply via a blanket `bazel run //cluster:bootstrap`** — it hits all
   OVH nodes together. Gate the rename per node with an opt-in node set (same
   idiom as `kimsufi_eno1_peer_route_enabled_nodes` in `ovh-nodes.tf`): the
   UserVolume is renamed only for nodes in the set, so an empty set is a no-op
   and you roll by adding one node at a time.
3. **Migrate or accept the single-copy disks first.** Not replicated, would be
   lost: `agent-box/agent-box-root`, `gecko/gecko-root`,
   `codex-nix-pod/codex-home`, `codex-nix-pod/codex-nix-store` (experimental
   agent/VM sandboxes). Confirm each is disposable or move it before wiping its
   node.

### Per-node procedure

1. `kubectl cordon <node>` and drain the losable/single-copy pods; for CNPG,
   `cnpg.io` will re-clone the instance elsewhere once its PVC is gone.
2. Add `<node>` to the rename opt-in set; apply Talos config to **that node
   only** (`talosctl -n <node> apply-config`), which wipes + recreates the data
   disk under the new UserVolume name.
3. Update the node's `nodePathMap` entry to the new mount in lockstep.
4. `kubectl uncordon <node>`; verify CNPG instances rejoin healthy and SeaweedFS
   re-replicates before moving to the next node.

This pairs naturally with the Stage-2 control-plane reshuffle (those nodes are
already being drained/rebuilt), but does not depend on it — the roll can be done
standalone whenever.

## Apply ordering (important — SC allowedTopologies is immutable)

1. Apply the `storage-tier` node labels: `bazel run //cluster:bootstrap` (or
   `talosctl apply-config`). Verify: `kubectl get nodes -L storage-tier`.
2. The two new SCs apply cleanly via Flux (additive). The **re-pinned
   `local-path-ovh`** changes an immutable field, so SSA can't mutate it in
   place — do a one-time `kubectl delete storageclass local-path-ovh` and let
   Flux recreate it. Bound PVCs survive the SC delete (the SC is only consulted
   at provisioning time).

Do steps 1 → 2 in order: if the re-pinned SC reconciles before the labels exist,
new `local-path-ovh` provisioning stalls Pending (existing bound volumes are
fine).

## Staging

**Stage 1 — SSD SeaweedFS tier for Forgejo git (fixes Haku dashboard). No
control-plane change, no new hardware.**

- [x] `storage-tier` labels + `local-path-ovh-{hdd,ssd}` SCs (this change).
- [ ] Add a SeaweedFS SSD volume-topology group (all-operator, single cluster —
      the operator's `spec.volume.volumeTopology` map is present in the installed
      v1.0.19 CRD; each group is its own StatefulSet on the shared master/filer,
      tagged via `extraArgs`). Sketch:

  ```yaml
  # cluster/k8s/seaweedfs/cluster/seaweed.yaml, under spec.volume
  # KEEP the existing flat `volume:` block unchanged (it is the untagged/default
  # = HDD tier on KS-5; renaming it into a topology group would rename the
  # StatefulSet and orphan the 1.8 TB of data). ADD only the ssd group:
  volumeTopology:
    ssd:
      replicas: 2 # the 2 KS-GAME nodes; replication 001 = 2 copies, survives 1 node
      storageClassName: local-path-ovh-ssd
      requests: { storage: 250Gi, cpu: 100m, memory: 512Mi }
      limits: { memory: 1536Mi }
      extraArgs: ["-disk=ssd"] # operator appends this to `weed volume`; no first-class diskType field
      priorityClassName: stateful-infra
      metricsPort: 9328
      nodeSelector: { storage-tier: ssd }
      affinity:
        nodeAffinity: # hard: never land on non-SSD
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - { key: storage-tier, operator: In, values: ["ssd"] }
        podAntiAffinity: # one per host
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels: { app.kubernetes.io/component: volume, app.kubernetes.io/name: seaweedfs }
              topologyKey: kubernetes.io/hostname
  ```

  **Verify on reconcile** that the operator _adds_ the ssd StatefulSet and does
  not touch the existing flat volume pods (flat + topology are documented to
  coexist, but confirm before trusting it).

- [ ] Add StorageClass `seaweedfs-ovh-ssd` (CSI driver, params `diskType: ssd`,
      `replication: "001"`) — confirm the `diskType` param name against the
      pinned CSI driver `v1.4.14`.
- [ ] Migrate the Forgejo git repo onto SSD (runbook below).

**Stage 2 — etcd onto NVMe.** Promote the 2 KS-GAME to control-plane (2 NVMe +
1 KS-5 HDD quorum; leadership not pinned — accept occasional HDD-leader
latency), demote 2 KS-5 to workers, re-check Nebula lighthouse placement. Fold
the data-disk mount rename (above) into these per-node rebuilds. Pin
`forgejo-db` / `seaweedfs-filer-db` to `local-path-ovh-ssd`.

**Stage 3 — third SSD node.** All-NVMe 3-member etcd + SSD placement headroom
(replication 001 across 3 SSD nodes tolerates 1 down without losing redundancy).

## Forgejo git → SSD migration runbook (Stage 1)

`storageClassName` is immutable on a PVC, so this is a copy-cutover (same shape
as the 2026-06-28 RWO→RWX migration), not a manifest flip.

1. Create `forgejo-git-rwx-ssd` (RWX, `storageClassName: seaweedfs-ovh-ssd`,
   50Gi).
2. Scale Forgejo to 0 (maintenance window).
3. Copy `forgejo-git-rwx` → `forgejo-git-rwx-ssd` (helper pod mounting both,
   `cp -a` / `rsync -a`). Verify repo counts + a `git fsck` sample.
4. Point the chart's `persistence.claimName` at `forgejo-git-rwx-ssd`
   (<../k8s/forgejo/app/helmrelease.yaml>, <../k8s/forgejo/app/git-storage-pvc.yaml>).
5. Scale up; verify `git.allegedly.works` and a clone/push. Bench the Haku
   dashboard load.
6. Retain the old PVC briefly, then delete once satisfied.
