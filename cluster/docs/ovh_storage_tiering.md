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

1. **Foundation:** hard `storage-tier` labels + media-scoped SCs — a consumer of
   `-ssd` cannot provision on HDD.
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
   _both_ instances on the two KS-GAME nodes (`forgejo-db`, `airlock-db`,
   `atuin`, `langfuse`, …); wiping both at once loses them. Wipe one, wait for
   CNPG re-clone + SeaweedFS re-replication onto the survivor, then the next.
2. **Do NOT apply via a blanket `bazel run //cluster:bootstrap`** — it hits all
   OVH nodes together. Gate the rename per node with an opt-in node set (same
   idiom as `kimsufi_eno1_peer_route_enabled_nodes` in `ovh-nodes.tf`): the
   UserVolume is renamed only for nodes in the set, so an empty set is a no-op
   and you roll by adding one node at a time.
3. **Warn before wiping any single-copy disk.** These local-path PVCs are not
   replicated and would be lost. They are pre-accepted as **disposable** (fine to
   lose), but the per-node preflight (Stage 2) must **list them and halt for
   explicit acknowledgment** before the wipe — and must **halt on any single-copy
   disk _not_ on this list**, so a newly-created one is never destroyed silently:
   - `agent-box/agent-box-root`
   - `gecko/gecko-root`
   - `codex-nix-pod/codex-home`
   - `codex-nix-pod/codex-nix-store`

The per-node rename procedure and its health gates are folded into **Stage 2**
of the execution plan below (it pairs with the control-plane reshuffle, which
already drains/rebuilds each node — but the rename roll can also be run
standalone).

## Execution plan

### Starting state (today)

| Node           | Box     | Role                   | etcd disk          | Data disk → mount                                         | Holds                                                      |
| -------------- | ------- | ---------------------- | ------------------ | --------------------------------------------------------- | ---------------------------------------------------------- |
| `ovh-ns102453` | KS-5    | control-plane          | `/dev/sda` **HDD** | `/dev/sdb` → `/var/mnt/seaweedfs-data` (`seaweedfs-data`) | SeaweedFS bulk vol + local-path PVs                        |
| `ovh-ns103656` | KS-5    | control-plane (leader) | `/dev/sda` **HDD** | `/dev/sdb` → `/var/mnt/seaweedfs-data`                    | SeaweedFS bulk vol + local-path PVs                        |
| `ovh-ns103711` | KS-5    | control-plane          | `/dev/sda` **HDD** | `/dev/sdb` → `/var/mnt/seaweedfs-data`                    | SeaweedFS bulk vol + local-path PVs                        |
| `ovh-ns104952` | KS-GAME | worker                 | —                  | NVMe#2 → `/var/mnt/seaweedfs-data`                        | ~15 CNPG, `seaweedfs-volume-0`, Loki/Tempo/Mimir, VMs      |
| `ovh-ns104963` | KS-GAME | worker                 | —                  | NVMe#2 → `/var/mnt/seaweedfs-data`                        | ~15 CNPG, seaweedfs master/filer, Loki/Mimir, agent-box VM |

etcd on HDD (WAL fsync p99 124–240 ms). Forgejo git on `seaweedfs-ovh`
(FUSE → HDD volume servers). `local-path-ovh` media-blind; DBs scattered by luck.

### Final state (no new hardware — end of Stage 2)

| Node           | Box     | Role                     | etcd disk          | Data disk → mount                                                 | Holds                                                         |
| -------------- | ------- | ------------------------ | ------------------ | ----------------------------------------------------------------- | ------------------------------------------------------------- |
| `ovh-ns104952` | KS-GAME | **control-plane**        | NVMe#1 **SSD**     | NVMe#2 → `/var/mnt/local-path-ovh-ssd` (`local-path-ovh-ssd`)     | SSD tier: SeaweedFS `ssd` vol (git), `forgejo-db`, `filer-db` |
| `ovh-ns104963` | KS-GAME | **control-plane**        | NVMe#1 **SSD**     | NVMe#2 → `/var/mnt/local-path-ovh-ssd`                            | SSD tier (peer copy)                                          |
| `ovh-ns102453` | KS-5    | control-plane (3rd, HDD) | `/dev/sda` **HDD** | `/dev/sdb` → `/var/mnt/local-path-ovh-hdd` (`local-path-ovh-hdd`) | HDD bulk: SeaweedFS bulk vol + cold DBs/PVs                   |
| `ovh-ns103656` | KS-5    | **worker**               | —                  | `/dev/sdb` → `/var/mnt/local-path-ovh-hdd`                        | HDD bulk                                                      |
| `ovh-ns103711` | KS-5    | **worker**               | —                  | `/dev/sdb` → `/var/mnt/local-path-ovh-hdd`                        | HDD bulk                                                      |

etcd quorum = 2 NVMe + 1 HDD (commits gated by the two NVMe majority; the HDD
member lags harmlessly and occasionally leads — accepted). Forgejo git on
`seaweedfs-ovh-ssd` (NVMe volume servers). Mounts self-describing; bulk on HDD.

**Stage 3 (optional, +1 NVMe OVH box):** control plane becomes 3× NVMe →
all-NVMe etcd (leadership never on HDD) + SSD replication headroom (repl `001`
across 3 SSD nodes tolerates 1 down without losing redundancy). All 3 KS-5 then
workers/bulk.

### Health gates (the pre/post checks each destructive step is fenced by)

Define these once; every stage references them. **G-all** must be green before
starting a stage and after each node op; the others gate specific step types.

- **G-etcd** — `talosctl -n <cp>… etcd status` / `service etcd`: every member
  `HEALTH OK`, same DB revision/raft index, no alarms (`etcd alarm list` empty),
  no leader churn; the `ControlPlaneLeasePutLatency*` alerts not firing. Only
  ever change **one** etcd member at a time, and only when the rest are green.
- **G-nodes** — `kubectl get nodes`: all `Ready` except the one intentionally
  cordoned; no unexpected `NotReady`.
- **G-swfs** — SeaweedFS fully replicated: via a master pod,
  `weed shell -c "volume.list"` shows **every** volume at 2 copies (repl `001`),
  zero under-replicated/`0-copy` volumes; filer up; `weed shell -c "cluster.check"`
  clean. Gate every volume-server wipe on this being green **before** (a source
  copy exists elsewhere) and **after** (re-replication back to 2 completed).
- **G-cnpg** — for each affected cluster, `kubectl cnpg status <cluster>`:
  `Cluster in healthy state`, all instances `streaming`, replication lag ≈ 0, a
  healthy primary. Gate every node wipe on: every CNPG cluster with an instance
  on that node has **≥1 healthy instance on another node** (re-clone source),
  and after: the re-cloned instance is `streaming` and caught up.
- **G-flux** — `flux get kustomizations` / `helmreleases` all `Ready`.
- **G-public** — Gatus green + blackbox: `git.allegedly.works`,
  `auth.allegedly.works` reachable; a test `git clone` succeeds.

### Stage 0 — foundation: `storage-tier` labels (this PR) + media-scoped SCs (follow-up)

Split into two PRs so nothing activates on merge unexpectedly:

- **This PR** ships only the `storage-tier` labels (Terraform/Talos in
  `ovh-nodes.tf`) + this plan. Merging it changes nothing on its own — the labels
  are inert until applied via Terraform, and the SCs that would consume them
  aren't here.
- **Follow-up PR** ships the StorageClass manifests (`local-path-ovh-{hdd,ssd}` +
  the `local-path-ovh` repin). These are Flux-reconciled, so they take effect on
  merge — hence they land _after_ the labels are live.

Apply order (declarative only; nothing moves):

1. Apply the labels: `bazel run //cluster:bootstrap` (or `talosctl
apply-config`). Verify: `kubectl get nodes -L storage-tier`.
2. Merge the follow-up SC PR. Flux creates `local-path-ovh-{hdd,ssd}` and re-pins
   the `local-path-ovh` alias; `allowedTopologies` is immutable, so the repin
   needs a one-time `kubectl delete storageclass local-path-ovh` for Flux to
   recreate it (bound PVCs survive — the SC is only read at provision time).

Do 1 → 2 in order: the repin references `storage-tier=hdd`, which no node has
until the labels are applied.

### Stage 0 post-checks

**G-flux** green (the local-path-provisioner Kustomization `Ready`) and
`kubectl get sc local-path-ovh{,-hdd,-ssd}` shows each with the expected
`allowedTopologies`. No workload moved — nothing to drain.

## Stage 1 — SSD SeaweedFS tier for Forgejo git (fixes the Haku dashboard)

No control-plane change, no new hardware — this is the change that fixes
Forgejo/Haku.

**Pre-check:** the KS-GAME NVMe (~450 GB) must have room for the `ssd` pool — it
already holds ~15 DBs + `seaweedfs-volume-0` + VM disks. Check headroom first
(`node_filesystem_avail_bytes{mountpoint="/var/mnt/seaweedfs-data"}`); if tight,
shed HDD-appropriate tenants to KS-5 (evict `seaweedfs-volume-0` here and let it
re-replicate onto a KS-5) or size the pool below 250 Gi. Confirm **G-swfs** green
before evicting any volume server, and again after it re-replicates.

1. **Add the SeaweedFS `ssd` volume-topology group** (keep the flat `volume:`
   block untouched). Commit → Flux reconciles. **Post-check:** the operator
   _adds_ a new `seaweedfs-volume-ssd-*` StatefulSet on the 2 KS-GAME nodes and
   does **not** disturb the existing flat volume pods; **G-swfs** green; the
   master lists the new `ssd`-disk volumes. Sketch:

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

(flat + topology are documented to coexist — confirm before trusting it.)

2. **Add StorageClass `seaweedfs-ovh-ssd`** (CSI, `diskType: ssd`, `replication:
"001"`; confirm the param name against CSI `v1.4.14`). **Post-check:** a
   throwaway PVC on it binds on an `ssd`-tagged volume (master `volume.list`).
3. **Migrate Forgejo git → SSD** (copy-cutover runbook below). **Post-checks:**
   **G-public** (a `git clone`/push works), the new PVC is bound on
   `seaweedfs-ovh-ssd`, and the Haku dashboard load is benchmarked vs. the old
   latency.

## Stage 2 — mount rename + etcd onto NVMe (rolling, node-by-node)

Two rolls interleaved per node: **(E)** move the etcd quorum from
{`102453`, `103656`, `103711`} to {`102453`, `104952`, `104963`}, and **(R)**
wipe+rename each data disk to `local-path-ovh-{hdd,ssd}`. Rules: **one etcd
membership change at a time** with **G-etcd** green between each; **never wipe
both KS-GAME at once** (**G-cnpg**); the data-disk wipe (R) is independent of
etcd (which lives on the system disk), so R and the CP-promote are separate ops.

Before starting: `etcdctl move-leader` off `103656` onto `102453` (the anchor
member that stays control-plane throughout), so no membership step ever touches
the leader.

**G-losable — single-copy-disk warning (run before wiping each node `N`).** List
the local-path volumes pinned to `N` and confirm the only non-replicated ones are
the pre-accepted disposable disks (rename rule 3):

```bash
node=<N>
kubectl get pv -o json | jq -r --arg n "$node" '
  .items[]
  | select((.spec.storageClassName // "") | test("local-path"))
  | select([ .spec.nodeAffinity.required.nodeSelectorTerms[]?.matchExpressions[]?
             | select(.key == "kubernetes.io/hostname") | .values[] ] | index($n))
  | "\(.spec.claimRef.namespace)/\(.spec.claimRef.name)\t\(.spec.storageClassName)"'
```

Everything CNPG (`cnpg.io/cluster`) re-clones and every SeaweedFS volume
re-replicates; Loki/Mimir/Tempo are S3-backed and Valkey is cache. Anything left
that is **not** on the accepted-disposable list **halts the roll** — investigate
or migrate it first. Otherwise print the disposable disks about to be destroyed
and get an explicit ack before wiping.

Per-node order — each fenced by **G-all** (all gates green) + **G-losable**
before and after:

1. **`ovh-ns104952` → SSD control-plane.** Verify re-clone sources exist
   (**G-cnpg**), cordon+drain (CNPG re-clones its instances onto survivors).
   Wipe+rename NVMe#2 → `local-path-ovh-ssd` (R). Promote to control-plane →
   joins etcd (learner → voter). **Post:** **G-etcd** shows 4 healthy members
   incl. `104952`; **G-swfs**/**G-cnpg** re-replicated.
2. **`ovh-ns103711` → worker (leave etcd).** One membership change: remove it
   from etcd, reprovision as worker; wipe+rename `/dev/sdb` →
   `local-path-ovh-hdd` (R). **Post:** **G-etcd** back to 3 members
   {`102453`, `103656`, `104952`}.
3. **`ovh-ns104963` → SSD control-plane.** As step 1 (drain, wipe+rename →
   `local-path-ovh-ssd`, promote/join). **Post:** **G-etcd** 4 members incl.
   `104963`.
4. **`ovh-ns103656` → worker (leave etcd).** Membership change; wipe+rename
   `/dev/sdb` → `local-path-ovh-hdd`. **Post:** **G-etcd** = target
   {`102453`, `104952`, `104963`}, tolerating 1 down.
5. **`ovh-ns102453` — data disk only.** Stays control-plane; do **not** touch its
   etcd / `/dev/sda`. Drain its `/dev/sdb` local-path PVs, wipe+rename →
   `local-path-ovh-hdd` (R). **Post:** **G-swfs**/**G-cnpg** green.
6. **Pin hot DBs to SSD.** Set `forgejo-db` + `seaweedfs-filer-db`
   `storageClass: local-path-ovh-ssd` + nodeSelector `storage-tier=ssd`; roll
   each CNPG instance one at a time (**G-cnpg** between). Re-check Nebula
   lighthouse placement now that `103656`/`103711` are workers.

**Exit:** matches the Final-state table; watch `ControlPlaneLeasePutLatency*`
drop as etcd fsync moves onto NVMe.

## Stage 3 — third SSD node (optional, future)

Buy 1 NVMe OVH box; add as control-plane (learner → voter), then remove `102453`
from etcd → all-NVMe 3-member quorum; bump the SeaweedFS `ssd` group to
`replicas: 3`. Gives leadership-never-on-HDD + SSD replication headroom. Same
one-member-at-a-time **G-etcd** discipline.

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
