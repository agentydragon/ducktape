# Plan: OVH data-disk mount rename (storage tiering — Stage 2 remainder)

**Status: active — the data-disk mount rename is the only remaining Stage 2 work.** The
etcd-onto-NVMe control-plane reshuffle is **done**: the control plane is now
`{103656, 104952, 104963}` (etcd on 2× KS-GAME NVMe + the KS-5 anchor `103656`), the leader
sits on the anchor `103656`, and `102453`/`103711` are demoted to workers. The rest of the
tiering foundation was already in place: media-scoped StorageClasses
(`local-path-ovh-{hdd,ssd}`, keyed to a `storage.allegedly.works/tier` node label), the
SeaweedFS volume layer on `volumeTopology` (`hdd` 3× KS-5, `ssd` 2× KS-GAME; operator 1.0.30),
**Forgejo git** on SSD, and both SSD-destined DBs — **`seaweedfs-filer-db`** and **`forgejo-db`**
— on the SSD tier.

What's left is the destructive piece the reshuffle deliberately deferred: **renaming the OVH
data-disk mounts off the legacy `/var/mnt/seaweedfs-data` path** to tier-named mounts, via a
rolling one-node-at-a-time disk repartition. Then an optional **Stage 3** (3rd NVMe box).

Reference material from the completed foundation work:
<../lessons_learned/2026_07_04_seaweedfs_volumetopology_and_operator.md>,
<../lessons_learned/2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md>,
<../runbooks/seaweedfs_pvc_storageclass_migration.md> (reusable PVC storage-class migration),
<../lessons_learned/2026_06_19_etcd_hdd_io_contention.md> (the etcd motivator, now resolved).

## Small independent cleanups

- Delete the old `forgejo-git-rwx` PVC + the three VolSync objects (`ReplicationSource`/
  `ReplicationDestination` + the intermediate PVC) from the git cutover, once satisfied it
  won't be rolled back (rollback until then = revert the `claimName` commit).

## Goal

Split OVH node-local storage into media-scoped StorageClasses so fsync/latency-critical data
sits on the KS-GAME NVMe, while bulk/append-heavy data (SeaweedFS blobs, most small Postgres
DBs, logs, caches, VM disks) stays on the KS-5 HDDs. The two original motivators are already
addressed: slow Forgejo (SeaweedFS-over-FUSE on 7200rpm HDD) — Forgejo git is on SSD; and etcd
HDD I/O contention — etcd is now on NVMe (control-plane reshuffle complete). The remaining
rename is **naming/clarity**: the data disk is still a UserVolume named `seaweedfs-data` (a
leftover from the SeaweedFS trial), with the general local-path-provisioner nested three levels
down inside a mount named after one tenant. This pass renames the mount for the disk/tier.

## Verified hardware + current state (roles as of the completed reshuffle)

Each OVH node has one XFS Talos UserVolume (`seaweedfs-data`) on its data disk, mounted at
`/var/mnt/seaweedfs-data`, with the general local-path-provisioner nested at
`.../local-path`. Whole-disk `df` (usage figures from 2026-07-03):

| Node           | Box     | Role today                | etcd disk      | Data disk      | Size    | Used    | Free    |
| -------------- | ------- | ------------------------- | -------------- | -------------- | ------- | ------- | ------- |
| `ovh-ns103656` | KS-5    | control-plane (anchor)    | `/dev/sda` HDD | `/dev/sdb` HDD | 1.8 TiB | 192 GiB | 1.6 TiB |
| `ovh-ns104952` | KS-GAME | control-plane             | NVMe#1 SSD     | NVMe#2 SSD     | 419 GiB | 68 GiB  | 351 GiB |
| `ovh-ns104963` | KS-GAME | control-plane             | NVMe#1 SSD     | NVMe#2 SSD     | 419 GiB | 59 GiB  | 360 GiB |
| `ovh-ns102453` | KS-5    | worker (3.6 TB HDD, bulk) | —              | `/dev/sdb` HDD | 3.6 TiB | 205 GiB | 3.4 TiB |
| `ovh-ns103711` | KS-5    | worker                    | —              | `/dev/sdb` HDD | 1.9 TiB | 77 GiB  | 1.8 TiB |

**Gotchas the earlier draft got wrong:**

- **KS-5 disks are not uniform.** `102453` is a ~4 TB disk; the other two are ~2 TB.
- **The etcd disk is separate** from the data disk above: etcd rides each control-plane's
  **install disk** (`/dev/sda` on KS-5, NVMe#1 on KS-GAME), not `/var/mnt/seaweedfs-data`. This
  is why the mount rename (a data-disk repartition) is independent of etcd.
- **SSD headroom is generous, not tight** (~350 GiB free on each KS-GAME node). What fills
  the NVMe is **movable bulk**, not hot DBs — per-PVC `du`:
  - `104952`: `seaweedfs-volume-0` 40 GiB (SeaweedFS bulk, re-replicates), `gecko/gecko-root`
    1.9 GiB (VM, disposable); every actual Postgres DB there is <5 GiB and mostly <1 GiB.
  - `104963`: `agent-box/agent-box-root` 47 GiB (VM, disposable); everything else <0.7 GiB.
  - The combined small-CNPG footprint across both SSD nodes is only a few GiB.

**Reclaimable garbage:** several `Released`/orphaned local-path dirs still consume disk
(duplicate `seaweedfs-volume-*` dirs, stale CNPG instance ordinals such as
`authentik-db-ovh-2` alongside `-3`/`-4`, `grafana-db-ovh-1`+`-3`, `gatus-db-1`+`-3`,
several `study-casino`/`atuin` ordinals). Do a `Released`-PV GC pass before trusting any
per-node sizing — stale dirs inflate `used`.

## Tier-placement policy

SSD is scarce (2 × 419 GiB, and at replication `001` it only survives one KS-GAME node
down). **Default everything to HDD.** Promote to SSD only fsync/latency-critical data,
deliberately:

**SSD tier:**

- **etcd** — the whole reason for the control-plane reshuffle. It is **not a PVC**, so no
  StorageClass places it: "etcd on SSD" means the control plane runs on the KS-GAME NVMe boxes,
  which is now the case.
- **Forgejo git** (SeaweedFS `ssd` volume group + `forgejo-git` RWX PVC), **`seaweedfs-filer-db`**,
  and **`forgejo-db`** — all on SSD.

**HDD tier (default — includes "most of the tiny Postgreses"):** all other CNPG DBs
(`authentik`, `langfuse`, `atuin`, `airlock`, `litellm`, `props`, `paperless`, `plaid`,
`gatus`, `tofu-state`, `attic`, `grafana`, `alertmanager`, `study-casino`, `haku-*`,
`wayback`, `gmail-labeling`, `postscanmail`, `manifold`, `tana`, `grocy`), the SeaweedFS bulk
volume servers, Loki/Mimir/Tempo WAL+cache, Valkey caches, and VM root disks (`agent-box`,
`gecko`, `codex`).

## Tier enforcement (built — the rename relies on it)

The `storage.allegedly.works/tier` node label (<../../terraform/main/ovh-nodes.tf>, a
per-node `storage_tier` field) is keyed to the node's **actual data-disk hardware, not its
role** — so it stayed correct when the KS-GAME nodes were promoted to control-plane. Three
StorageClasses (<../../k8s/local-path-provisioner/>) share one provisioner + nodePathMap and
differ only by `allowedTopologies`, which ANDs `topology.kubernetes.io/zone=hil-ovh` (never
lands on a non-OVH SSD node like wyrm2) with the tier label: `-hdd` → `tier=hdd`, `-ssd` →
`tier=ssd`, and `local-path-ovh` → deprecated alias re-pinned to `-hdd`. Under
`WaitForFirstConsumer` a mis-scheduled `-ssd` PVC stays **Pending (loud)** rather than silently
landing on HDD.

- **No control-plane taint trap.** OVH control planes run `allowSchedulingOnControlPlanes =
true` and carry no CP taint, so the promoted KS-GAME nodes stayed schedulable; neither the SSD
  volume group nor SSD-pinned CNPG pods need a CP toleration.
- **Guardrail:** `-disk` tags are unverified, so an alert fires if a SeaweedFS `ssd`
  volume-server pod is not on a `tier=ssd` node.

The mount rename is naming/clarity, **not** an enforcement layer: local-path's `nodePathMap` is
keyed by node, not StorageClass, so a mis-scheduled pod would still provision at whatever path
that node has.

## SeaweedFS re-replication is manual (the rename depends on this)

SeaweedFS does **not** auto-re-replicate: neither the operator (the `Seaweed` CRD exposes no
repair/heal field) nor the master self-heals under-replicated volumes. Restoring replica
count is a manual `weed shell` command from a master pod — and mutating commands require an
admin **`lock`** first (without it, `-apply` errors `need to run "lock" first`):

```bash
# weed shell reads commands on stdin (this build has no `-c` flag)
printf 'volume.fix.replication\n' | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell   # dry-run (default)
printf 'lock\nvolume.fix.replication -apply\nunlock\n' | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell
```

`volume.fix.replication -apply` fixes **one** missing replica per volume per run and needs a
target server with a **free volume slot** (see the slot model below). So an _unplanned_
volume-server/node loss leaves volumes at 1 copy until a human runs this. Every volume-server
wipe in the rename is therefore gated on **G-swfs** before (a source copy exists) and after
(re-replication back to 2 completed via this command).

The **`SeaweedFSReplicaPlacementMismatch`** alert
(<../../k8s/seaweedfs/monitoring/prometheusrule.yaml>) surfaces under-replication so it
doesn't lurk — it fires on `sum(SeaweedFS_master_replica_placement_mismatch) > 0`. Keep it
**alert-only**, not auto-remediation, so node failures surface rather than being masked; if a
scheduled `-apply` autoheal is ever added it must be **suspended during the rename** so
re-replication stays deliberate and gated at G-swfs checkpoints.

**Slot model** (why re-replication can stall): a volume server's capacity is a **volume-slot
count** (`-max=0` → disk size ÷ `volumeSizeLimitMB`, 16 GB here), not raw space — e.g. the
419 GB NVMe yields only ~24 slots. Re-replication needs a server with a **free slot**, so a
slot-full server (`volume-0` at 24/24) cannot receive replicas even with disk free.

**GOTCHA — refresh FUSE clients before deleting a volume server (the rename retires and
re-provisions volume-server nodes, so it must follow this).** `weed mount` clients cache
volume locations and only re-resolve off an **alive** server's "volume not found"; a
**deleted** server (DNS `no such host`) leaves the cache stale → I/O errors / SIGBUS on
consumers. So make server deletion the **last, quiescence-gated** step: move data off (server
stays running, empty) → verify 2-copy (**G-swfs**) → refresh clients while the emptied server
is still alive (roll consumers, or let them self-heal via its 404→re-lookup) → confirm it's
idle → **only then** delete. Full RCA:
<../lessons_learned/2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md>.

## Data-disk mount rename (fixing the backwards `/var/mnt/seaweedfs-data` path)

Today the OVH data disk is a UserVolume named `seaweedfs-data`, and the general
local-path-provisioner is nested under it at `/var/mnt/seaweedfs-data/local-path` — so every
OVH local-path PVC (~70: CNPG DBs, Valkey, Loki/Mimir/Tempo, VM disks, and SeaweedFS volume
servers, which are themselves just `local-path` PVCs) lives inside a mount named after one
tenant, three levels down. It's a leftover from the SeaweedFS trial.

**Target:** name the mount for the disk/tier; SeaweedFS becomes one PVC of the class:

- KS-5: UserVolume `local-path-ovh-hdd` → `/var/mnt/local-path-ovh-hdd`
- KS-GAME: UserVolume `local-path-ovh-ssd` → `/var/mnt/local-path-ovh-ssd`
  (`diskSelector` on the NVMe serial)

### Prerequisite — the per-node rename mechanism is not coded yet

The current TF gives every node the **same** UserVolume `name = "seaweedfs-data"`
(<../../terraform/main/ovh-nodes.tf>), and the `nodePathMap`
(<../../k8s/local-path-provisioner/helmrelease.yaml>) points **all five** OVH nodes at
`/var/mnt/seaweedfs-data/local-path`. Before any node can be renamed, both surfaces must be made
**per-node**:

1. Talos side: derive the UserVolume name from the node's `storage_tier`
   (`local-path-ovh-${tier}`), gated by an **opt-in node set** (the `kimsufi_*_enabled_nodes`
   idiom already in `ovh-nodes.tf`) so an empty set is a no-op and nodes flip one at a time.
2. local-path side: change that node's `nodePathMap` entry to the new path in the same commit.

### Two change surfaces move together per node (gotcha)

The rename touches **both** (a) the Talos `UserVolumeConfig` name in `ovh-nodes.tf` (renaming a
UserVolume **repartitions/wipes** the disk) and (b) the local-path-provisioner `nodePathMap` in
`helmrelease.yaml` (Flux). If (a) renames a node's mount to `/var/mnt/local-path-ovh-hdd` but
(b) still points that node at `/var/mnt/seaweedfs-data/local-path`, new PVCs land on the **root
filesystem**. The `nodePathMap` is a single ConfigMap but has per-node entries, so per-node
rollout is possible — each node's `nodePathMap` entry must be flipped in lockstep with its wipe.
The non-atomicity between the TF apply and the Flux reconcile is covered by the node being
**cordoned+drained** during its wipe: nothing provisions a PVC on it until both surfaces are on
the new path and it is uncordoned.

### Why the wipe is OK

Nearly all data is replicated or rebuildable: CNPG re-clones a wiped instance from its
replica; SeaweedFS re-replicates (`001`); Loki/Mimir/Tempo local PVCs are WAL/cache backed by
SeaweedFS S3; Valkey is cache. So a **rolling, one-node-at-a-time wipe** is safe.

### Rules

1. **One node at a time — never both KS-GAME together.** Many CNPG clusters have _both_
   instances on the two KS-GAME nodes; wiping both loses them. Wipe one, wait for CNPG
   re-clone + SeaweedFS re-replication onto the survivor, then the next.
2. **Do NOT apply via a blanket `bazel run //cluster:bootstrap`** — it hits all OVH nodes
   together. Gate the rename per node with the opt-in node set (rule 1 of the mechanism above):
   an empty set is a no-op; you roll by adding one node at a time. Flip that node's
   `nodePathMap` entry (surface b) in the same step, and apply the Talos side with
   `tofu apply -target=` for just that node.
3. **Warn before wiping any single-copy disk.** These local-path PVCs are not replicated.
   They are pre-accepted as **disposable**, but the per-node preflight (**G-losable**) must
   **list them and halt for explicit acknowledgment**, and must **halt on any single-copy
   disk _not_ on this list** so a newly-created one is never destroyed silently:
   - `agent-box/agent-box-root`
   - `gecko/gecko-root`
   - `codex-nix-pod/codex-home`
   - `codex-nix-pod/codex-nix-store`

## CNPG tier migration without disruption

**CNPG `storage.storageClass` is effectively immutable** — the operator will not migrate
existing PVCs, and the proven path (the repo's `cnpg_region_switch` skill / RUNBOOK) is a
**clone-and-cutover**, not an in-place edit. The two cases are handled differently.

**Verified inventory (2026-07-03):** all HDD-destined OVH CNPG clusters are on
`local-path-ovh` (the two SSD-migrated DBs, `seaweedfs-filer-db-ssd` and `forgejo-db-ssd`, are
on `local-path-ovh-ssd` and stay put). Instance counts: mostly 2-instance, `study-casino-db` ×
3, `wayback-archive-db` × **1**. Three HDD-destined clusters have _both_ instances on the two
KS-GAME nodes (`airlock`, `atuin`, `langfuse`) — these ride Case A eviction when their KS-GAME
node is drained for the data-disk rename.

### Case A — HDD-destined (nearly all): no class change, ride node-eviction

`local-path-ovh` re-pins to `= -hdd`, so these keep their existing PVC/SC name — **no
migration, no app repoint**. When a node holding an instance is cordoned+drained for its
data-disk wipe, CNPG deletes the unschedulable PVC + pod and re-clones from the surviving
instance via `pg_basebackup`, and the re-pinned SC's `tier=hdd` `allowedTopologies` **forces**
the new PVC onto a KS-5 node. Gate: **G-cnpg** before (≥1 healthy instance elsewhere as the
re-clone source) and after (re-cloned instance `streaming`, caught up). Strictly one KS-GAME
node at a time — the three both-on-KS-GAME clusters pass through a single-healthy-instance
window, so never drain the second KS-GAME node until the first's re-clone is streaming.

### Case B — SSD-destined (`seaweedfs-filer-db`, `forgejo-db`): done

Both SSD-destined DBs already sit on `local-path-ovh-ssd` (migrated via the
**`cnpg_region_switch`** clone-and-cutover), so the data-disk rename leaves them in place. The
procedure lives in the runbook if another DB ever needs it.

### Pre-execution prep

- **`wayback-archive-db`: scale 1 → 2 instances first** (make it HA like the others) so it
  gains a re-clone source and rides Case A during its node's data-disk wipe, instead of being
  destroyed with its only node. Do this before wiping its node.
- Confirm every 2-instance cluster is `G-cnpg`-green before the roll; `study-casino-db` (3
  instances) always keeps ≥1 healthy instance if steps stay one-at-a-time.

See <../cnpg_conventions.md> and <../../skills/cnpg_region_switch/RUNBOOK.md>.

## Execution plan — data-disk mount rename (rolling, node-by-node)

### Application discipline (every Terraform-applying step)

**Never run a blind `bazel run //cluster:bootstrap`** for this plan — it applies against all
nodes at once. Each TF-applying step is: `tofu plan` against `cluster/terraform/main/` →
**read the full diff** → apply only the intended addresses with **`tofu apply
-target=<addr>`** (the same `-target` mechanism `bootstrap.py` uses) → re-plan to confirm the
residual diff is empty or only what's expected. One node at a time, gated additionally by the
opt-in node set (rule 2) so the plan surfaces only that node — reviewed before apply.

### Health gates (each destructive step is fenced by these)

**G-all** = all of the below green; must hold before wiping a node and after each node op.

- **G-etcd** — `talosctl … etcd status` / `service etcd`: every member `HEALTH OK`, same DB
  revision/raft index, no alarms, no leader churn; `ControlPlaneLeasePutLatency*` not firing.
  The rename touches only the data disk, not etcd, but keep etcd green as a tripwire: the two
  control-plane data-disk wipes (`103656`, `104952`, `104963`) drain a CP node, and a wobbly
  quorum during a drain is a stop signal.
- **G-nodes** — `kubectl get nodes`: all `Ready` except the one intentionally cordoned.
- **G-swfs** — from a master pod (commands on stdin — no `-c` flag), a dry-run
  `printf 'volume.fix.replication\n' | weed shell` returns **empty** (nothing
  under-replicated) and `volume.list` / `cluster.check` are clean; filer up. Gate every
  volume-server wipe on this **before** (a source copy exists elsewhere) and **after**
  (re-replication back to 2 completed — see "SeaweedFS re-replication is manual" above).
- **G-cnpg** — for each affected cluster, `kubectl cnpg status <cluster>`: healthy, all
  instances `streaming`, lag ≈ 0. Gate every node wipe on: every CNPG cluster with an instance
  on that node has **≥1 healthy instance elsewhere** (re-clone source); after: the re-cloned
  instance is `streaming` and caught up.
- **G-flux** — `flux get kustomizations` / `helmreleases` all `Ready` (allowing for the
  known-suspended services).
- **G-public** — Gatus green + blackbox: `git.allegedly.works`, `auth.allegedly.works`
  reachable; a test `git clone` succeeds.

**G-losable** (before wiping each node `N`): list the local-path volumes pinned to `N` and
confirm the only non-replicated ones are the pre-accepted disposable disks (rename rule 3):

```bash
node=<N>
kubectl get pv -o json | jq -r --arg n "$node" '
  .items[]
  | select((.spec.storageClassName // "") | test("local-path"))
  | select([ .spec.nodeAffinity.required.nodeSelectorTerms[]?.matchExpressions[]?
             | select(.key == "kubernetes.io/hostname") | .values[] ] | index($n))
  | "\(.spec.claimRef.namespace)/\(.spec.claimRef.name)\t\(.spec.storageClassName)"'
```

Everything CNPG re-clones and every SeaweedFS volume re-replicates; Loki/Mimir/Tempo are
S3-backed and Valkey is cache. Anything left that is **not** on the disposable list **halts the
roll**. Otherwise print the disposable disks about to be destroyed and get an explicit ack.
HDD-destined DBs (Case A) need no pre-step — draining forces the re-clone, and the re-pinned
`local-path-ovh` (`tier=hdd`) `allowedTopologies` lands it on a KS-5 node. Ensure
`wayback-archive-db` is already 2-instance before draining its node.

### Per-node roll

Each node's data disk is wiped+renamed to `local-path-ovh-{hdd,ssd}` (surface a) with its
`nodePathMap` entry flipped in lockstep (surface b), fenced by **G-all** + **G-losable** before
and after. etcd is untouched (it lives on the install disk), so there is no membership change —
but draining a control-plane node still moves its pods, so keep G-etcd green throughout.

Suggested order (least-risk first):

1. **`ovh-ns103711` (KS-5 worker, HDD).** Cleanest first node: pure worker, no etcd, HDD tier.
   Verify re-clone sources (**G-cnpg**) + SeaweedFS 2-copy (**G-swfs**), cordon+drain,
   wipe+rename `/dev/sdb` → `local-path-ovh-hdd`. Proves the mechanism end-to-end on the
   lowest-stakes node.
2. **`ovh-ns102453` (KS-5 worker, 3.6 TB HDD, most bulk).** As step 1; more to drain +
   re-replicate, so budget time for **G-swfs** to return to 2 copies.
3. **`ovh-ns104952` (KS-GAME control-plane, NVMe#2 → SSD).** Never together with `104963`
   (both hold KS-GAME CNPG instances). Wipe+rename NVMe#2 → `local-path-ovh-ssd`. etcd stays on
   NVMe#1, untouched. **Post:** **G-etcd** still 3 healthy members; **G-swfs**/**G-cnpg**
   re-replicated.
4. **`ovh-ns104963` (KS-GAME control-plane, NVMe#2 → SSD).** Only after step 3's re-clones are
   `streaming`. As step 3.
5. **`ovh-ns103656` (KS-5 control-plane anchor, HDD).** Do **not** touch its etcd / `/dev/sda`.
   Drain its `/dev/sdb` local-path PVs, wipe+rename → `local-path-ovh-hdd`.
6. **Finalize.** All SSD-destined DBs are already on `local-path-ovh-ssd` — every other DB stays
   on HDD, no DB moves here. Re-check Nebula lighthouse placement now that `102453`/`103711`
   are workers (lighthouse role is per-node, not tied to k8s control-plane role).

**Exit:** every OVH data disk mounted at `/var/mnt/local-path-ovh-{hdd,ssd}`, `nodePathMap`
pointing there, and the `seaweedfs-data` UserVolume name retired.

### Stage 3 — third SSD node (optional, future)

Buy 1 NVMe OVH box; add as control-plane (learner → voter), then remove `103656` (the last HDD
etcd seat) → all-NVMe 3-member quorum; bump the SeaweedFS `ssd` group to `replicas: 3`. Same
one-member-at-a-time **G-etcd** discipline.
