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
3. **Stage 2 (Talos hardening):** give the KS-GAME NVMe its own
   `UserVolumeConfig` (e.g. `seaweedfs-ssd-data`, `diskSelector` on the NVMe
   serial, mounted at `/var/mnt/seaweedfs-ssd-data`) and point `-ssd`'s
   nodePathMap there. Then the SSD tier physically cannot exist on a KS-5 (the
   mount is absent), removing trust in the human-set label.

**Guardrail:** since `-disk` tags are unverified, alert if a SeaweedFS `ssd`
volume-server pod is not on a `storage-tier=ssd` node.

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
in the Talos-layer L3 hardening above. Pin `forgejo-db` / `seaweedfs-filer-db`
to `local-path-ovh-ssd`.

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
