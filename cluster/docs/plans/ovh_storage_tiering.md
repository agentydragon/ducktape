# Plan: etcd onto NVMe + data-disk mount rename (OVH storage tiering — Stage 2)

**Status: active — Stage 2 is the remaining work.** The tiering foundation is already in
place: media-scoped StorageClasses (`local-path-ovh-{hdd,ssd}`, keyed to a
`storage.allegedly.works/tier` node label), the SeaweedFS volume layer on `volumeTopology`
(`hdd` 3× KS-5, `ssd` 2× KS-GAME; operator 1.0.30), and both **Forgejo git** and the
**`seaweedfs-filer-db` metadata DB** cut over to the SSD tier. What's left is the one
destructive piece the rest was sequenced around: moving **etcd onto NVMe** via a rolling
Talos control-plane reshuffle, and renaming the OVH data-disk mounts off the legacy
`/var/mnt/seaweedfs-data` path. Then an optional **Stage 3** (3rd NVMe box).

Reference material from the completed foundation work:
<../lessons_learned/2026_07_04_seaweedfs_volumetopology_and_operator.md>,
<../lessons_learned/2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md>,
<../runbooks/seaweedfs_pvc_storageclass_migration.md> (reusable PVC storage-class migration).

## Small independent cleanups

- Delete the old `forgejo-git-rwx` PVC + the three VolSync objects (`ReplicationSource`/
  `ReplicationDestination` + the intermediate PVC) from the git cutover, once satisfied it
  won't be rolled back (rollback until then = revert the `claimName` commit).

## Goal

Split OVH node-local storage into media-scoped StorageClasses so fsync/latency-critical data
(etcd, Forgejo git) sits on the KS-GAME NVMe, while bulk/append-heavy data (SeaweedFS blobs,
most small Postgres DBs, logs, caches, VM disks) stays on the KS-5 HDDs. Motivated by slow
Forgejo (SeaweedFS-over-FUSE on 7200rpm HDD) degrading the Haku dashboard, and by etcd HDD
I/O contention (<../lessons_learned/2026_06_19_etcd_hdd_io_contention.md>). The storage side
of this is done; **etcd on HDD is the last unaddressed motivator** — hence Stage 2.

## Verified hardware + current usage (2026-07-03)

Each OVH node has one XFS Talos UserVolume (`seaweedfs-data`) on its data disk, mounted at
`/var/mnt/seaweedfs-data`, with the general local-path-provisioner nested at
`.../local-path`. Whole-disk `df`:

| Node           | Box     | Role today    | Data disk      | Size    | Used    | Free    |
| -------------- | ------- | ------------- | -------------- | ------- | ------- | ------- |
| `ovh-ns102453` | KS-5    | control-plane | `/dev/sdb` HDD | 3.6 TiB | 205 GiB | 3.4 TiB |
| `ovh-ns103656` | KS-5    | control-plane | `/dev/sdb` HDD | 1.8 TiB | 192 GiB | 1.6 TiB |
| `ovh-ns103711` | KS-5    | control-plane | `/dev/sdb` HDD | 1.9 TiB | 77 GiB  | 1.8 TiB |
| `ovh-ns104952` | KS-GAME | worker        | NVMe#2 SSD     | 419 GiB | 68 GiB  | 351 GiB |
| `ovh-ns104963` | KS-GAME | worker        | NVMe#2 SSD     | 419 GiB | 59 GiB  | 360 GiB |

**Gotchas the earlier draft got wrong:**

- **KS-5 disks are not uniform.** `102453` is a ~4 TB disk; the other two are ~2 TB.
- **The etcd disk is separate** from the data disk above: etcd rides each control-plane's
  **install disk** (`/dev/sda` on KS-5, NVMe#1 on KS-GAME), not `/var/mnt/seaweedfs-data`.
- **SSD headroom is generous, not tight** (~350 GiB free on each KS-GAME node). What fills
  the NVMe today is **movable bulk**, not hot DBs — per-PVC `du`:
  - `104952`: `seaweedfs-volume-0` 40 GiB (SeaweedFS bulk, re-replicates), `gecko/gecko-root`
    1.9 GiB (VM, disposable); every actual Postgres DB there is <5 GiB and mostly <1 GiB.
  - `104963`: `agent-box/agent-box-root` 47 GiB (VM, disposable); everything else <0.7 GiB.
  - The combined small-CNPG footprint across both SSD nodes is only a few GiB.

So the SSD is currently occupied by exactly the things this plan moves **off** it (one
SeaweedFS bulk volume server + two VM root disks). Almost nothing on the NVMe today actually
needs NVMe.

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
  StorageClass can place it: "etcd on SSD" means keeping the control plane on the KS-GAME
  NVMe boxes (Stage 2). This is a hard requirement.
- **Forgejo git** (SeaweedFS `ssd` volume group + `forgejo-git` RWX PVC) and
  **`seaweedfs-filer-db`** — already on SSD.
- **`forgejo-db`** — optional, still pending (small; keeps hot Forgejo metadata next to its
  git).

**HDD tier (default — includes "most of the tiny Postgreses"):** all other CNPG DBs
(`authentik`, `langfuse`, `atuin`, `airlock`, `litellm`, `props`, `paperless`, `plaid`,
`gatus`, `tofu-state`, `attic`, `grafana`, `alertmanager`, `study-casino`, `haku-*`,
`wayback`, `gmail-labeling`, `postscanmail`, `manifold`, `tana`, `grocy`), the SeaweedFS bulk
volume servers, Loki/Mimir/Tempo WAL+cache, Valkey caches, and VM root disks (`agent-box`,
`gecko`, `codex`).

## Tier enforcement (built — Stage 2 relies on it)

The `storage.allegedly.works/tier` node label (<../../terraform/main/ovh-nodes.tf>, a
per-node `storage_tier` field) is keyed to the node's **actual data-disk hardware, not its
role** — so it stays correct when a KS-GAME node is re-designated control-plane in Stage 2.
Three StorageClasses (<../../k8s/local-path-provisioner/>) share one provisioner +
nodePathMap and differ only by `allowedTopologies`, which ANDs
`topology.kubernetes.io/zone=hil-ovh` (never lands on a non-OVH SSD node like wyrm2) with the
tier label: `-hdd` → `tier=hdd`, `-ssd` → `tier=ssd`, and `local-path-ovh` → deprecated alias
re-pinned to `-hdd`. Under `WaitForFirstConsumer` a mis-scheduled `-ssd` PVC stays **Pending
(loud)** rather than silently landing on HDD.

Two facts that matter for the Stage 2 reshuffle:

- **No control-plane taint trap.** OVH control planes run `allowSchedulingOnControlPlanes =
true` and carry no CP taint, so when the KS-GAME nodes are promoted they stay schedulable;
  neither the SSD volume group nor SSD-pinned CNPG pods need a CP toleration.
- **Guardrail:** `-disk` tags are unverified, so an alert fires if a SeaweedFS `ssd`
  volume-server pod is not on a `tier=ssd` node.

The mount rename (below) is naming/clarity, **not** an enforcement layer: local-path's
`nodePathMap` is keyed by node, not StorageClass, so a mis-scheduled pod would still provision
at whatever path that node has.

## SeaweedFS re-replication is manual (Stage 2 depends on this)

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
wipe in Stage 2 is therefore gated on **G-swfs** before (a source copy exists) and after
(re-replication back to 2 completed via this command).

The **`SeaweedFSReplicaPlacementMismatch`** alert
(<../../k8s/seaweedfs/monitoring/prometheusrule.yaml>) surfaces under-replication so it
doesn't lurk — it fires on `sum(SeaweedFS_master_replica_placement_mismatch) > 0`. Keep it
**alert-only**, not auto-remediation, so node failures surface rather than being masked; if a
scheduled `-apply` autoheal is ever added it must be **suspended during Stage 2** so
re-replication stays deliberate and gated at G-swfs checkpoints.

**Slot model** (why re-replication can stall): a volume server's capacity is a **volume-slot
count** (`-max=0` → disk size ÷ `volumeSizeLimitMB`, 16 GB here), not raw space — e.g. the
419 GB NVMe yields only ~24 slots. Re-replication needs a server with a **free slot**, so a
slot-full server (`volume-0` at 24/24) cannot receive replicas even with disk free.

**GOTCHA — refresh FUSE clients before deleting a volume server (Stage 2 retires and
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

### Two change surfaces move together per node (gotcha)

The rename touches **both** (a) the Talos `UserVolumeConfig` name in
<../../terraform/main/ovh-nodes.tf> (renaming a UserVolume **repartitions/wipes** the disk)
and (b) the local-path-provisioner `nodePathMap` in
<../../k8s/local-path-provisioner/helmrelease.yaml> (Flux). If (a) renames a node's mount to
`/var/mnt/local-path-ovh-hdd` but (b) still points that node at
`/var/mnt/seaweedfs-data/local-path`, new PVCs land on the **root filesystem**. The
`nodePathMap` is a single ConfigMap but has per-node entries, so per-node rollout is possible
— but the "opt-in node set" idiom (rule 2) covers only the Talos side; each node's
`nodePathMap` entry must be flipped in lockstep with its wipe.

### Why the wipe is OK

Nearly all data is replicated or rebuildable: CNPG re-clones a wiped instance from its
replica; SeaweedFS re-replicates (`001`); Loki/Mimir/Tempo local PVCs are WAL/cache backed by
SeaweedFS S3; Valkey is cache. So a **rolling, one-node-at-a-time wipe** is safe.

### Rules

1. **One node at a time — never both KS-GAME together.** Many CNPG clusters have _both_
   instances on the two KS-GAME nodes; wiping both loses them. Wipe one, wait for CNPG
   re-clone + SeaweedFS re-replication onto the survivor, then the next.
2. **Do NOT apply via a blanket `bazel run //cluster:bootstrap`** — it hits all OVH nodes
   together. Gate the rename per node with an opt-in node set (same idiom as
   `kimsufi_eno1_peer_route_enabled_nodes` in `ovh-nodes.tf`): an empty set is a no-op; you
   roll by adding one node at a time. Flip that node's `nodePathMap` entry (surface b) in the
   same step.
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
**clone-and-cutover**, not an in-place edit. So the two cases are handled very differently.

**Verified inventory (2026-07-03):** all OVH CNPG clusters are on `local-path-ovh`. Instance
counts: mostly 2-instance, `study-casino-db` × 3, `wayback-archive-db` × **1**. Four clusters
have _both_ instances on the two KS-GAME nodes (`airlock`, `atuin`, `forgejo`, `langfuse`).

### Case A — HDD-destined (nearly all): no class change, ride node-eviction

`local-path-ovh` re-pins to `= -hdd`, so these keep their existing PVC/SC name — **no
migration, no app repoint**. They only need to leave the KS-GAME nodes, which happens for free
when those nodes drain in Stage 2: CNPG deletes the unschedulable PVC + pod and re-clones from
the surviving instance via `pg_basebackup`, and the re-pinned SC's `tier=hdd`
`allowedTopologies` **forces** the new PVC onto a KS-5 node. Gate: **G-cnpg** before (≥1
healthy instance elsewhere as the re-clone source) and after (re-cloned instance `streaming`,
caught up). Strictly one KS-GAME node at a time — the four both-on-KS-GAME clusters pass
through a single-healthy-instance window, so never drain the second KS-GAME node until the
first's re-clone is streaming.

### Case B — SSD-destined (`forgejo-db` only, remaining): clone-and-cutover

A real class change (`local-path-ovh` → `local-path-ovh-ssd`), so use the
**`cnpg_region_switch`** procedure: stand up a new Cluster on `local-path-ovh-ssd` as a
streaming replica (`bootstrap.pg_basebackup` + `replica.enabled`), confirm lag ≈ 0, promote
(`replica.enabled: false`), **repoint the app** (Forgejo's DB host → the new cluster's `-rw`
service), then delete the old cluster. `seaweedfs-filer-db` already went through this path;
`forgejo-db` is optional and the only Case B left.

### Pre-execution prep

- **`wayback-archive-db`: scale 1 → 2 instances first** (make it HA like the others) so it
  gains a re-clone source and rides Case A during the Stage-2 KS-GAME drain, instead of being
  destroyed with its only node. Do this before Stage 2.
- Confirm every 2-instance cluster is `G-cnpg`-green before the roll; `study-casino-db` (3
  instances across 4 to-be-wiped nodes) always keeps ≥1 healthy instance if steps stay
  one-at-a-time.

See <../cnpg_conventions.md> and <../../skills/cnpg_region_switch/RUNBOOK.md>.

## Execution plan

### Application discipline (every Terraform-applying step)

**Never run a blind `bazel run //cluster:bootstrap`** for this plan — it applies against all
nodes at once. Each TF-applying step is: `tofu plan` against `cluster/terraform/main/` →
**read the full diff** → apply only the intended addresses with **`tofu apply
-target=<addr>`** (the same `-target` mechanism `bootstrap.py` uses) → re-plan to confirm the
residual diff is empty or only what's expected. One concern at a time. Stage 2 per-node ops
are targeted single-node applies, gated additionally by the opt-in node set (rule 2) so the
plan surfaces only that node — reviewed before apply.

### Final state (no new hardware — end of Stage 2)

| Node           | Box     | Role                                     | etcd disk      | Data disk → mount                          | Holds                                                         |
| -------------- | ------- | ---------------------------------------- | -------------- | ------------------------------------------ | ------------------------------------------------------------- |
| `ovh-ns104952` | KS-GAME | **control-plane**                        | NVMe#1 **SSD** | NVMe#2 → `/var/mnt/local-path-ovh-ssd`     | SSD tier: SeaweedFS `ssd` vol (git), `forgejo-db`, `filer-db` |
| `ovh-ns104963` | KS-GAME | **control-plane**                        | NVMe#1 **SSD** | NVMe#2 → `/var/mnt/local-path-ovh-ssd`     | SSD tier (peer copy)                                          |
| `ovh-ns103656` | KS-5    | control-plane (3rd, HDD; primary/anchor) | `/dev/sda` HDD | `/dev/sdb` → `/var/mnt/local-path-ovh-hdd` | HDD bulk: SeaweedFS bulk vol + cold DBs/PVs                   |
| `ovh-ns102453` | KS-5    | **worker** (3.6 TB HDD)                  | —              | `/dev/sdb` → `/var/mnt/local-path-ovh-hdd` | HDD bulk — biggest disk → most local-path/bulk pods           |
| `ovh-ns103711` | KS-5    | **worker**                               | —              | `/dev/sdb` → `/var/mnt/local-path-ovh-hdd` | HDD bulk                                                      |

etcd quorum = 2 NVMe + 1 HDD (commits gated by the two-NVMe majority; the HDD member lags
harmlessly and occasionally leads — accepted). Forgejo git on `seaweedfs-ovh-ssd` (NVMe volume
servers). Mounts self-describing; bulk on HDD.

**The 3rd (HDD) control-plane is `103656`, not the big-disk `102453`.** etcd rides each CP's
**system disk** (`/dev/sda`), not the big data disk, so an HDD control-plane is equivalent
whichever KS-5 node holds it — while the largest data disk (`102453`, 3.6 TB) is left as a
**worker** so it can attract the most local-path/bulk pods without control-plane resource/I-O
contention. `103656` is chosen because it is the current etcd leader, so it doubles as the
migration anchor (below) with no opening `move-leader` needed. This means the TF
primary/bootstrap control-plane must move `102453` → `103656`: `primary_controlplane_ip` and
the `talos_machine_bootstrap`/kubeconfig endpoints (`infrastructure.tf`) point at `102453`
today, and `102453` sits in its own `kimsufi_cp_servers` map — reassign both to `103656` and
demote `102453` into the worker set. (Bootstrap already ran; guard the endpoint change so it
does not retrigger.)

**etcd's neighbors on the KS-GAME NVMe#1 (accepted co-location).** On Talos the install disk's
EPHEMERAL partition (`/var`) is one XFS filesystem, so etcd (`/var/lib/etcd`) shares that disk
_and_ filesystem with containerd's image + snapshot store (`/var/lib/containerd`), disk-backed
emptyDirs (`/var/lib/kubelet/...`; memory-medium emptyDirs are tmpfs, so they don't count),
and pod/system logs (`/var/log`). etcd therefore contends with containerd and emptyDir I/O —
but this is accepted:

- On NVMe the contention is a different order of magnitude from the HDD problem we're fixing
  (seek-bound p99 124–240 ms → sub-ms NVMe fsync even under concurrent I/O), so the move off
  HDD already captures ~all the win.
- A 2-NVMe box cannot fully isolate etcd's device queue — etcd always shares a physical device
  with something (OS/containerd on NVMe#1, or the SSD blob store + DBs on NVMe#2). Keeping
  etcd on **NVMe#1 is the better side**: NVMe#2's heaviest tenant is the SeaweedFS ssd volume
  server (sustained blob writes), so NVMe#1 keeps etcd away from that, leaving only
  containerd's bursty pulls + emptyDirs as neighbors, which NVMe absorbs.
- Residual risk: these CPs are schedulable (`allowSchedulingOnControlPlanes=true`), so a
  scratch-/image-heavy pod could land on a KS-GAME CP and pound NVMe#1. Mitigate by steering
  heavy/emptyDir-heavy workloads onto the **HDD workers** — which is already the reason the
  biggest-disk node (`102453`) is a worker. A dedicated `/var/lib/etcd` UserVolume carved from
  NVMe#2 (filesystem isolation, not device isolation) is available if a benchmark ever shows
  fsync contention, but it trades blob-store contention for containerd contention and is not
  worth it up front.

### Health gates (each destructive step is fenced by these)

**G-all** = all of the below green; must hold before starting a stage and after each node op.

- **G-etcd** — `talosctl … etcd status` / `service etcd`: every member `HEALTH OK`, same DB
  revision/raft index, no alarms, no leader churn; `ControlPlaneLeasePutLatency*` not firing.
  Change **one** etcd member at a time, only when the rest are green.
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
- **G-flux** — `flux get kustomizations` / `helmreleases` all `Ready`.
- **G-public** — Gatus green + blackbox: `git.allegedly.works`, `auth.allegedly.works`
  reachable; a test `git clone` succeeds.

### Stage 2 — mount rename + etcd onto NVMe (rolling, node-by-node)

Two rolls interleaved per node: **(E)** move the etcd quorum from
{`102453`, `103656`, `103711`} to {`103656`, `104952`, `104963`}, and **(R)** wipe+rename each
data disk to `local-path-ovh-{hdd,ssd}`. Rules: **one etcd membership change at a time** with
**G-etcd** green between each; **never wipe both KS-GAME at once** (**G-cnpg**); the data-disk
wipe (R) is independent of etcd (which lives on the install disk).

Anchor = `103656` (the KS-5 node that stays control-plane throughout and holds the final HDD
etcd seat). It is the current leader, so no opening `move-leader` is needed — just confirm
leadership is on `103656` and keep it there; no membership step ever touches the leader. Do the
TF primary/bootstrap reassignment (`102453` → `103656`, see Final state) before promoting any
KS-GAME node.

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
HDD-destined DBs (Case A above) need no pre-step — draining forces the re-clone, and the
re-pinned `local-path-ovh` (`tier=hdd`) `allowedTopologies` lands it on a KS-5 node. Ensure
`wayback-archive-db` is already 2-instance before draining its KS-GAME node.

Per-node order — each fenced by **G-all** + **G-losable** before and after:

1. **`ovh-ns104952` → SSD control-plane.** Verify re-clone sources (**G-cnpg**), cordon+drain.
   Wipe+rename NVMe#2 → `local-path-ovh-ssd` (R). Promote to control-plane → joins etcd
   (learner → voter). **Post:** **G-etcd** shows 4 healthy members incl. `104952`;
   **G-swfs**/**G-cnpg** re-replicated.
2. **`ovh-ns103711` → worker.** One membership change: remove from etcd, reprovision as worker;
   wipe+rename `/dev/sdb` → `local-path-ovh-hdd` (R). **Post:** **G-etcd** back to 3 {`102453`,
   `103656`, `104952`}.
3. **`ovh-ns104963` → SSD control-plane.** As step 1. **Post:** **G-etcd** 4 members incl.
   `104963`.
4. **`ovh-ns102453` → worker (big 3.6 TB HDD).** Membership change: remove from etcd,
   reprovision as worker; drain its `/dev/sdb` local-path PVs, wipe+rename → `local-path-ovh-hdd`
   (R). **Post:** **G-etcd** = target {`103656`, `104952`, `104963`}.
5. **`ovh-ns103656` — data disk only.** Stays control-plane (anchor); do **not** touch its etcd
   / `/dev/sda`. Drain its `/dev/sdb` local-path PVs, wipe+rename → `local-path-ovh-hdd` (R).
   **Post:** **G-swfs**/**G-cnpg** green.
6. **(Optional) migrate `forgejo-db` to SSD (Case B).** Clone-and-cutover via
   `cnpg_region_switch` (new cluster → stream → promote → repoint Forgejo → delete old). Leave
   every other DB on HDD. Re-check Nebula lighthouse placement now that `102453`/`103711` are
   workers (lighthouse role is per-node, not tied to k8s control-plane role).

**Exit:** matches the Final-state table; watch `ControlPlaneLeasePutLatency*` drop as etcd
fsync moves onto NVMe.

### Stage 3 — third SSD node (optional, future)

Buy 1 NVMe OVH box; add as control-plane (learner → voter), then remove `103656` (the last HDD
etcd seat) → all-NVMe 3-member quorum; bump the SeaweedFS `ssd` group to `replicas: 3`. Same
one-member-at-a-time **G-etcd** discipline.
