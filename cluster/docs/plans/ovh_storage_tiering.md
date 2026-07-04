# Plan: OVH storage tiering (HDD bulk vs SSD hot) + etcd onto NVMe

**Status: active.** Foundation is live: the `storage.allegedly.works/tier` node labels are
applied to all five OVH nodes (KS-5 `hdd`, KS-GAME `ssd`), and the media-scoped
StorageClasses `local-path-ovh-{hdd,ssd}` plus the `local-path-ovh`→`hdd` repin are merged
and reconciled. Remaining, in order: **Stage 1** (SSD SeaweedFS tier + Forgejo git cutover —
the Haku-dashboard fix, no wipes/CP changes), then **Stage 2** (mount rename + etcd onto
NVMe, the rolling destructive part), then the optional **Stage 3**.

## Goal

Split OVH node-local storage into media-scoped StorageClasses so fsync/latency-critical
data (etcd, Forgejo git) sits on the KS-GAME NVMe, while bulk/append-heavy data (SeaweedFS
blobs, most small Postgres DBs, logs, caches, VM disks) stays on the KS-5 HDDs. Motivated
by slow Forgejo (SeaweedFS-over-FUSE on 7200rpm HDD) degrading the Haku dashboard, and by
etcd HDD I/O contention (<../lessons_learned/2026_06_19_etcd_hdd_io_contention.md>).

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
SeaweedFS bulk volume server + two VM root disks). Almost nothing on the NVMe today
actually needs NVMe.

**Reclaimable garbage:** several `Released`/orphaned local-path dirs still consume disk
(duplicate `seaweedfs-volume-*` dirs, stale CNPG instance ordinals such as
`authentik-db-ovh-2` alongside `-3`/`-4`, `grafana-db-ovh-1`+`-3`, `gatus-db-1`+`-3`,
several `study-casino`/`atuin` ordinals). Do a `Released`-PV GC pass before trusting any
per-node sizing — stale dirs inflate `used`.

## Tier-placement policy (post-switch)

SSD is scarce (2 × 419 GiB, and at replication `001` it only survives one KS-GAME node
down). **Default everything to HDD.** Promote to SSD only fsync/latency-critical data,
deliberately:

**SSD tier:**

- **etcd** — the whole reason for the control-plane reshuffle. It is **not a PVC**, so no
  StorageClass can place it: "etcd on SSD" means keeping the control plane on the KS-GAME
  NVMe boxes (Stage 2). This is a hard requirement.
- **Forgejo git** — the SeaweedFS `ssd` volume group + the `forgejo-git` RWX PVC. This is
  the change that fixes the Haku dashboard. Definite.
- **`forgejo-db`** — likely (small; keeps hot Forgejo metadata next to its git).
- **`seaweedfs-filer-db`** — yes. It is the `postgres2` metadata backend on the critical
  path of _every_ SeaweedFS CSI/S3 op, including the Forgejo git POSIX mount the SSD tier
  exists to accelerate: git is metadata-heavy, so leaving this DB on HDD would leave git's
  small-op latency HDD-bound even with git blobs on SSD volume servers. Tiny (~2 GiB), a
  2-instance CNPG pair (one per KS-GAME node), on the data NVMe (separate from etcd on
  NVMe#1) — no downside. Speeds all SeaweedFS consumers, not just git.

**HDD tier (default — includes "most of the tiny Postgreses"):** all other CNPG DBs
(`authentik`, `langfuse`, `atuin`, `airlock`, `litellm`, `props`, `paperless`, `plaid`,
`gatus`, `tofu-state`, `attic`, `grafana`, `alertmanager`, `study-casino`, `haku-*`,
`wayback`, `gmail-labeling`, `postscanmail`, `manifold`, `tana`, `grocy`), the SeaweedFS
bulk volume servers, Loki/Mimir/Tempo WAL+cache, Valkey caches, and VM root disks
(`agent-box`, `gecko`, `codex`).

## Design: media as a node label + `allowedTopologies`

`storage.allegedly.works/tier` node label, set in <../../terraform/main/ovh-nodes.tf> and
keyed to the node's actual data-disk hardware (a per-node `storage_tier` field), **not** its
role — so it stays correct when a KS-GAME node is later re-designated control-plane (Stage 2).

Three StorageClasses (<../../k8s/local-path-provisioner/>) share the one provisioner +
nodePathMap; media differs only by `allowedTopologies`. Each ANDs two keys:
`topology.kubernetes.io/zone=hil-ovh` (keeps the class OVH-scoped, so it never lands on a
non-OVH SSD node such as wyrm2) **and** `storage.allegedly.works/tier`:

- `local-path-ovh-hdd` → zone `hil-ovh` + `tier=hdd` (KS-5)
- `local-path-ovh-ssd` → zone `hil-ovh` + `tier=ssd` (KS-GAME)
- `local-path-ovh` → deprecated alias, re-pinned to the same as `-hdd`

Under `WaitForFirstConsumer`, binding follows the pod: a PVC on the SSD class binds only
where an OVH SSD node is schedulable; if none is, it stays **Pending (loud)** instead of
silently landing on HDD. (`local-path` never checks free space, so PVC size is not a
placement lever, and a SeaweedFS `-disk=ssd` tag is unverified — media must be a hard
constraint, not a hint.)

### Why not "StorageClass + PVC size"

`local-path-ovh` is media-blind: the only thing deciding media is which node the pod
schedules on (`/var/mnt/seaweedfs-data` is HDD on KS-5 and NVMe on KS-GAME). Making media
an explicit hard constraint is the whole point.

### Robustness layers

1. **Foundation:** hard `storage.allegedly.works/tier` labels + media-scoped SCs — a
   consumer of `-ssd` cannot provision on HDD.
2. The SeaweedFS SSD volume-topology group (Stage 1) also carries a `required`
   nodeAffinity on `tier=ssd` — belt-and-suspenders so the pod, not just the volume, is
   media-locked.

The mount rename (below) is **naming/clarity, not** a third enforcement layer:
local-path's `nodePathMap` is keyed by node, not by StorageClass, so a mis-scheduled pod
would still provision at whatever path that node has.

**Guardrail:** since `-disk` tags are unverified, alert if a SeaweedFS `ssd`
volume-server pod is not on a `tier=ssd` node.

**Verified (rehearsal 2026-07-04) — `volumeTopology` is all-or-nothing, NOT additive.** The
operator (`controller_volume.go`, confirmed in 1.0.19 _and_ master) does
`if len(VolumeTopology) > 0 { return ...topology }` — so the moment `volumeTopology` is set,
the flat `spec.volume` StatefulSet is **no longer reconciled** (an operator upgrade won't
help). The SSD tier therefore can't be "added beside" the flat block; the whole volume layer
must move to `volumeTopology` (an `hdd` group + an `ssd` group), which is a data migration —
see the rewritten Stage 1 below. Two hard rehearsal findings drive the procedure:

- **`spec.volume` must stay in the CR.** Removing it while adding `volumeTopology` **panics
  the operator** (nil deref in `buildVolumeServerStartupScriptWithTopology`, which reads
  `m.Spec.Volume.*` for topology defaults). Keep `spec.volume` as a defaults stub — in
  topology mode the operator ignores it for server creation but still reads it for defaults.
- Each topology group **requires `dataCenter` and `rack`** (CRD-required fields).

The good news, also rehearsed: flipping to `volumeTopology` **leaves the existing flat
StatefulSet running** (same UID, orphaned but serving), and **`volume.move`** relocates each
volume copy flat→topology (copy → tail-for-in-flight → delete source) **without ever dropping
below 2 copies**, retagging disk type in flight. Data integrity confirmed end-to-end.

**Verified — no control-plane taint trap.** OVH control planes run with
`allowSchedulingOnControlPlanes = true` (ovh-nodes.tf), and live OVH nodes carry no
control-plane taint. So when the KS-GAME nodes are promoted (Stage 2) they stay
schedulable, and neither the SSD volume group nor the SSD-pinned CNPG pods need a
control-plane toleration. (The CP tolerations already on the flat SeaweedFS blocks are
harmless no-ops.)

## SeaweedFS re-replication is manual (Stage 1/2 depend on this)

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
wipe in Stage 1/2 is therefore gated on **G-swfs** before (a source copy exists) and after
(re-replication back to 2 completed via this command).

The **`SeaweedFSReplicaPlacementMismatch`** alert
(<../../k8s/seaweedfs/monitoring/prometheusrule.yaml>) now surfaces under-replication so it
doesn't lurk — it fires on `sum(SeaweedFS_master_replica_placement_mismatch) > 0`. Keep it
**alert-only**, not auto-remediation, so node failures surface rather than being masked; if a
scheduled `-apply` autoheal is ever added it must be **suspended during Stage 1/2** so
re-replication stays deliberate and gated at G-swfs checkpoints.

**Slot model** (why re-replication can stall): a volume server's capacity is a **volume-slot
count** (`-max=0` → disk size ÷ `volumeSizeLimitMB`, 16 GB here), not raw space — e.g. the
419 GB NVMe yields only ~24 slots. Re-replication needs a server with a **free slot**, so a
slot-full server (`volume-0` at 24/24) cannot receive replicas even with disk free.

## Data-disk mount rename (fixing the backwards `/var/mnt/seaweedfs-data` path)

Today the OVH data disk is a UserVolume named `seaweedfs-data`, and the general
local-path-provisioner is nested under it at `/var/mnt/seaweedfs-data/local-path` — so
every OVH local-path PVC (~70: CNPG DBs, Valkey, Loki/Mimir/Tempo, VM disks, and SeaweedFS
volume servers, which are themselves just `local-path` PVCs) lives inside a mount named
after one tenant, three levels down. It's a leftover from the SeaweedFS trial.

**Target:** name the mount for the disk/tier; SeaweedFS becomes one PVC of the class:

- KS-5: UserVolume `local-path-ovh-hdd` → `/var/mnt/local-path-ovh-hdd`
- KS-GAME: UserVolume `local-path-ovh-ssd` → `/var/mnt/local-path-ovh-ssd`
  (`diskSelector` on the NVMe serial)

### Two change surfaces move together per node (gotcha)

The rename touches **both** (a) the Talos `UserVolumeConfig` name in
<../../terraform/main/ovh-nodes.tf> (renaming a UserVolume **repartitions/wipes** the disk)
and (b) the local-path-provisioner `nodePathMap` in
<../../k8s/local-path-provisioner/helmrelease.yaml> (Flux). If (a) renames a node's mount
to `/var/mnt/local-path-ovh-hdd` but (b) still points that node at
`/var/mnt/seaweedfs-data/local-path`, new PVCs land on the **root filesystem**. The
`nodePathMap` is a single ConfigMap but has per-node entries, so per-node rollout is
possible — but the "opt-in node set" idiom (rule 2) covers only the Talos side; each node's
`nodePathMap` entry must be flipped in lockstep with its wipe.

### Why the wipe is OK

Nearly all data is replicated or rebuildable: CNPG re-clones a wiped instance from its
replica; SeaweedFS re-replicates (`001`); Loki/Mimir/Tempo local PVCs are WAL/cache backed
by SeaweedFS S3; Valkey is cache. So a **rolling, one-node-at-a-time wipe** is safe.

### Rules

1. **One node at a time — never both KS-GAME together.** Many CNPG clusters have _both_
   instances on the two KS-GAME nodes; wiping both loses them. Wipe one, wait for CNPG
   re-clone + SeaweedFS re-replication onto the survivor, then the next.
2. **Do NOT apply via a blanket `bazel run //cluster:bootstrap`** — it hits all OVH nodes
   together. Gate the rename per node with an opt-in node set (same idiom as
   `kimsufi_eno1_peer_route_enabled_nodes` in `ovh-nodes.tf`): an empty set is a no-op; you
   roll by adding one node at a time. Flip that node's `nodePathMap` entry (surface b) in
   the same step.
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

**Verified inventory (2026-07-03):** all 18 OVH CNPG clusters are on `local-path-ovh`.
Instance counts: 16 × 2-instance, `study-casino-db` × 3, `wayback-archive-db` × **1**.
Four clusters have _both_ instances on the two KS-GAME nodes (`airlock`, `atuin`, `forgejo`,
`langfuse`).

### Case A — HDD-destined (16 of 18): no class change, ride node-eviction

`local-path-ovh` re-pins to `= -hdd`, so these keep their existing PVC/SC name — **no
migration, no app repoint**. They only need to leave the KS-GAME nodes, which happens for
free when those nodes drain in Stage 2: CNPG deletes the unschedulable PVC + pod and
re-clones from the surviving instance via `pg_basebackup`, and the re-pinned SC's
`tier=hdd` `allowedTopologies` **forces** the new PVC onto a KS-5 node. Gate: **G-cnpg**
before (≥1 healthy instance elsewhere as the re-clone source) and after (re-cloned instance
`streaming`, caught up). Strictly one KS-GAME node at a time — the four both-on-KS-GAME
clusters pass through a single-healthy-instance window, so never drain the second KS-GAME
node until the first's re-clone is streaming.

### Case B — SSD-destined (`forgejo-db`, `filer-db`): clone-and-cutover

A real class change (`local-path-ovh` → `local-path-ovh-ssd`), so use the
**`cnpg_region_switch`** procedure: stand up a new Cluster on `local-path-ovh-ssd` as a
streaming replica (`bootstrap.pg_basebackup` + `replica.enabled`), confirm lag ≈ 0, promote
(`replica.enabled: false`), **repoint the app**, then delete the old cluster. App repoints:
`forgejo-db` → Forgejo's DB host; `filer-db` → the filer's `WEED_POSTGRES2_*` host (new
cluster name = new `-rw` service DNS). Non-destructive to the source until the final delete
— important for `filer-db`, the SeaweedFS metadata SSOT with no off-cluster backup: verify
the clone, then delete.

### Pre-execution prep

- **`wayback-archive-db`: scale 1 → 2 instances first** (make it HA like the others) so it
  gains a re-clone source and rides Case A during the Stage-2 KS-GAME drain, instead of
  being destroyed with its only node. Do this before Stage 2.
- Confirm every 2-instance cluster is `G-cnpg`-green before the roll; `study-casino-db` (3
  instances across 4 to-be-wiped nodes) always keeps ≥1 healthy instance if steps stay
  one-at-a-time.

See <../cnpg_conventions.md> and <../../skills/cnpg_region_switch/RUNBOOK.md>.

## Execution plan

### Application discipline (every Terraform-applying step)

**Never run a blind `bazel run //cluster:bootstrap`** for this plan — it applies against
all nodes at once. Each TF-applying step is: `tofu plan` against
`cluster/terraform/main/` → **read the full diff** → apply only the intended addresses with
**`tofu apply -target=<addr>`** (the same `-target` mechanism `bootstrap.py` uses) → re-plan
to confirm the residual diff is empty or only what's expected. One concern at a time.

- **Stage 2** per-node ops are targeted single-node applies, gated additionally by the
  opt-in node set (rule 2) so the plan surfaces only that node — reviewed before apply.

The Stage-0 label apply already followed this: a targeted
`talos_machine_configuration_apply.{kimsufi,kimsufi_cp}` plan whose diff was exactly one
`nodeLabels` line per node (anything else — image/disk/network — is a stop-and-investigate).

### Final state (no new hardware — end of Stage 2)

| Node           | Box     | Role                                     | etcd disk      | Data disk → mount                          | Holds                                                         |
| -------------- | ------- | ---------------------------------------- | -------------- | ------------------------------------------ | ------------------------------------------------------------- |
| `ovh-ns104952` | KS-GAME | **control-plane**                        | NVMe#1 **SSD** | NVMe#2 → `/var/mnt/local-path-ovh-ssd`     | SSD tier: SeaweedFS `ssd` vol (git), `forgejo-db`, `filer-db` |
| `ovh-ns104963` | KS-GAME | **control-plane**                        | NVMe#1 **SSD** | NVMe#2 → `/var/mnt/local-path-ovh-ssd`     | SSD tier (peer copy)                                          |
| `ovh-ns103656` | KS-5    | control-plane (3rd, HDD; primary/anchor) | `/dev/sda` HDD | `/dev/sdb` → `/var/mnt/local-path-ovh-hdd` | HDD bulk: SeaweedFS bulk vol + cold DBs/PVs                   |
| `ovh-ns102453` | KS-5    | **worker** (3.6 TB HDD)                  | —              | `/dev/sdb` → `/var/mnt/local-path-ovh-hdd` | HDD bulk — biggest disk → most local-path/bulk pods           |
| `ovh-ns103711` | KS-5    | **worker**                               | —              | `/dev/sdb` → `/var/mnt/local-path-ovh-hdd` | HDD bulk                                                      |

etcd quorum = 2 NVMe + 1 HDD (commits gated by the two-NVMe majority; the HDD member lags
harmlessly and occasionally leads — accepted). Forgejo git on `seaweedfs-ovh-ssd` (NVMe
volume servers). Mounts self-describing; bulk on HDD.

**The 3rd (HDD) control-plane is `103656`, not the big-disk `102453`.** etcd rides each
CP's **system disk** (`/dev/sda`), not the big data disk, so an HDD control-plane is
equivalent whichever KS-5 node holds it — while the largest data disk (`102453`, 3.6 TB)
is left as a **worker** so it can attract the most local-path/bulk pods without
control-plane resource/I-O contention. `103656` is chosen because it is the current etcd
leader, so it doubles as the migration anchor (below) with no opening `move-leader` needed.
This means the TF **primary/bootstrap control-plane must move `102453` → `103656`**:
`primary_controlplane_ip` and the `talos_machine_bootstrap`/kubeconfig endpoints
(`infrastructure.tf`) point at `102453` today, and `102453` sits in its own
`kimsufi_cp_servers` map — reassign both to `103656` and demote `102453` into the worker
set. (Bootstrap already ran; guard the endpoint change so it does not retrigger.)

**etcd's neighbors on the KS-GAME NVMe#1 (accepted co-location).** On Talos the install
disk's EPHEMERAL partition (`/var`) is one XFS filesystem, so etcd (`/var/lib/etcd`) shares
that disk _and_ filesystem with containerd's image + snapshot store (`/var/lib/containerd`),
disk-backed emptyDirs (`/var/lib/kubelet/...`; memory-medium emptyDirs are tmpfs, so they
don't count), and pod/system logs (`/var/log`). etcd therefore contends with containerd and
emptyDir I/O — but this is accepted:

- On NVMe the contention is a different order of magnitude from the HDD problem we're
  fixing (seek-bound p99 124–240 ms → sub-ms NVMe fsync even under concurrent I/O), so the
  move off HDD already captures ~all the win.
- A 2-NVMe box cannot fully isolate etcd's device queue — etcd always shares a physical
  device with something (OS/containerd on NVMe#1, or the SSD blob store + DBs on NVMe#2).
  Keeping etcd on **NVMe#1 is the better side**: NVMe#2's heaviest tenant is the SeaweedFS
  ssd volume server (sustained blob writes), so NVMe#1 keeps etcd away from that, leaving
  only containerd's bursty pulls + emptyDirs as neighbors, which NVMe absorbs.
- Residual risk: these CPs are schedulable (`allowSchedulingOnControlPlanes=true`), so a
  scratch-/image-heavy pod could land on a KS-GAME CP and pound NVMe#1. Mitigate by steering
  heavy/emptyDir-heavy workloads onto the **HDD workers** — which is already the reason the
  biggest-disk node (`102453`) is a worker. A dedicated `/var/lib/etcd` UserVolume carved
  from NVMe#2 (filesystem isolation, not device isolation) is available if a benchmark ever
  shows fsync contention, but it trades blob-store contention for containerd contention and
  is not worth it up front.

**Stage 3 (optional, +1 NVMe OVH box):** control plane becomes 3× NVMe → all-NVMe etcd
(leadership never on HDD) + SSD replication headroom (repl `001` across 3 SSD nodes
tolerates 1 down). All 3 KS-5 then workers/bulk.

### Health gates (each destructive step is fenced by these)

**G-all** = all of the below green; must hold before starting a stage and after each node op.

- **G-etcd** — `talosctl … etcd status` / `service etcd`: every member `HEALTH OK`, same DB
  revision/raft index, no alarms, no leader churn; `ControlPlaneLeasePutLatency*` not
  firing. Change **one** etcd member at a time, only when the rest are green.
- **G-nodes** — `kubectl get nodes`: all `Ready` except the one intentionally cordoned.
- **G-swfs** — from a master pod (commands on stdin — no `-c` flag), a dry-run
  `printf 'volume.fix.replication\n' | weed shell` returns **empty** (nothing
  under-replicated) and `volume.list` / `cluster.check` are clean; filer up. Gate every
  volume-server wipe on this **before** (a source copy exists elsewhere) and **after**
  (re-replication back to 2 completed — see "SeaweedFS re-replication is manual" above).
- **G-cnpg** — for each affected cluster, `kubectl cnpg status <cluster>`: healthy, all
  instances `streaming`, lag ≈ 0. Gate every node wipe on: every CNPG cluster with an
  instance on that node has **≥1 healthy instance elsewhere** (re-clone source); after: the
  re-cloned instance is `streaming` and caught up.
- **G-flux** — `flux get kustomizations` / `helmreleases` all `Ready`.
- **G-public** — Gatus green + blackbox: `git.allegedly.works`, `auth.allegedly.works`
  reachable; a test `git clone` succeeds.

### Stage 1 — SSD SeaweedFS tier for Forgejo git (fixes the Haku dashboard)

No control-plane change, no new hardware — but (per the rehearsal above) it is a **volume-layer
topology migration**, not a bolt-on: the whole SeaweedFS volume layer moves from the flat
`spec.volume` to two `volumeTopology` groups (`hdd` on KS-5, `ssd` on KS-GAME), and the
existing ~237 GiB of bulk is `volume.move`-relocated onto the new `hdd` servers. `spec.volume`
stays in the CR as a defaults stub (removing it panics the operator).

**Phase 0 — `seaweedfs-ovh-ssd` StorageClass** (<../../k8s/seaweedfs-csi/sc-seaweedfs-ovh-ssd.yaml>):
CSI, `parameters: {diskType: ssd, replication: "001"}` (v1.4.14 maps `diskType`→`weed mount
-disk`, passes `replication` through). Additive; nothing binds until the `ssd` servers exist
(Phase 1), so a real post-check waits for Phase 1.

**Phase 1 — add the `hdd` + `ssd` `volumeTopology` groups, keep `spec.volume`.** Commit →
Flux. The operator brings up empty `seaweedfs-volume-{hdd,ssd}-*` StatefulSets; the flat
`seaweedfs-volume` StatefulSet keeps running (orphaned, same UID). **Post-check:** master
`cluster.check` lists all servers (flat + hdd + ssd), **G-swfs** green (bulk still 2-copy on
the flat 3). Sketch (both groups need `dataCenter` + `rack`; `spec.volume` unchanged, still
present):

```yaml
# cluster/k8s/seaweedfs/cluster/seaweed.yaml — spec.volume stays (defaults stub); ADD:
volumeTopology:
  hdd: # 3 replicas on KS-5; holds the migrated bulk
    replicas: 3
    dataCenter: hil
    rack: hil-ovh-h109b04
    storageClassName: local-path-ovh-hdd
    requests: { storage: 1800Gi, cpu: 100m, memory: 512Mi }
    limits: { memory: 1536Mi }
    extraArgs: ["-disk=hdd"] # matches the flat servers' default ("" == hdd, verified)
    priorityClassName: stateful-infra
    nodeSelector: { storage.allegedly.works/tier: hdd }
    affinity: { podAntiAffinity: <one per host, labelSelector seaweedfs/topology=hdd> }
  ssd: # 2 replicas on KS-GAME; serves Forgejo git
    replicas: 2
    dataCenter: hil
    rack: hil-ovh-h108b01 # real OVH rack of both KS-GAME nodes
    storageClassName: local-path-ovh-ssd
    requests: { storage: 250Gi, cpu: 100m, memory: 512Mi }
    limits: { memory: 1536Mi }
    minFreeSpacePercent: 10 # NVMe#2 is one shared XFS pool (ssd server + forgejo-db +
    #   filer-db); go read-only before the disk fills so the fragile DBs keep their reserve.
    maxVolumeCounts: 50 # overcommit slots: -max=0 gives only ~24 on 419 GB, and SeaweedFS
    #   pre-grabs ~2–6 (mostly-empty) volumes per collection; thin volumes make this ~free.
    extraArgs: ["-disk=ssd"]
    priorityClassName: stateful-infra
    metricsPort: 9328
    nodeSelector: { storage.allegedly.works/tier: ssd }
    affinity: { podAntiAffinity: <one per host, labelSelector seaweedfs/topology=ssd> }
```

**Phase-1 gotcha — the flat servers' anti-affinity blocks the topology pods (fix or they
stay Pending).** The topology pods carry the same `app.kubernetes.io/component=volume` +
`name=seaweedfs` labels as the flat servers, so the flat servers' original one-per-host
`podAntiAffinity` (`{component: volume, name: seaweedfs}`) treats the topology pods as
anti-affinity targets — each flat server blocks its node against topology co-location. With
flat servers on 3 of the 5 OVH nodes, only the 2 flat-free nodes accept a topology pod; the
other 3 stay **Pending**. Fix: narrow the flat anti-affinity to `component=volume` **AND**
`seaweedfs/topology DoesNotExist` (keeps flat-flat separation, ignores topology). The operator
won't apply a `spec.volume` change in topology mode (it stops reconciling the flat
StatefulSet), so patch the **orphaned** flat StatefulSet directly — it's transient (deleted in
Phase 2), so no committed-config change is warranted:

```bash
kubectl -n seaweedfs patch sts seaweedfs-volume --type=merge -p '{"spec":{"template":{"spec":{"affinity":{"podAntiAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":[{"labelSelector":{"matchExpressions":[{"key":"app.kubernetes.io/component","operator":"In","values":["volume"]},{"key":"app.kubernetes.io/name","operator":"In","values":["seaweedfs"]},{"key":"seaweedfs/topology","operator":"DoesNotExist"}]},"topologyKey":"kubernetes.io/hostname"}]}}}}}}'
```

The flat servers roll one-at-a-time (each sticky to its local-path node); **G-swfs** stays
green. A topology pod left Pending during the roll can keep a stale scheduler backoff — `kubectl
-n seaweedfs delete pod <pending-topology-pod>` forces a fresh scheduling attempt once the last
flat server has rolled. (Done 2026-07-04: all 8 servers up, G-swfs green, no data moved.)

**Phase 2 — evacuate the flat servers → `hdd` group, then retire them (hands-on,
G-swfs-gated).** Moves the ~473 GB physical (~237 GiB logical) of bulk off the 3 flat servers
onto the 3 `hdd` servers with `volume.move` (copy → tail-for-in-flight → delete source — never
drops below 2 copies). All from a master pod; mutating commands need the admin `lock`.

**Pre:** **G-swfs** green; all 8 servers up (`cluster.check`); snapshot `volume.list`.

1. **Evacuate one flat server at a time** (`volume-2`, then `-1`, then `-0`). For each volume
   on flat server `F`, `volume.move` its copy to an `hdd` server that does **not** already hold
   that volume, so the volume's 2 copies land on 2 different `hdd` hosts (repl 001, rack
   `hil-ovh-h109b04`). Batch per lock session:

   ```bash
   printf 'lock\nvolume.move -source <F>:8444 -target <hdd-X>:8444 -volumeId <id>\n…\nunlock\n' \
     | kubectl -n seaweedfs exec -i seaweedfs-master-0 -- weed shell
   ```

   After each server empties: **G-swfs** green (every volume still 2 copies, now on `hdd`
   servers) and `volume.list` shows `F` holding 0 volumes.

2. **Verify the bulk is fully on the `hdd` group.** `volume.list`: every bulk volume's 2 copies
   on `seaweedfs-volume-hdd-*`, none on the flat servers; `volume.fix.replication` dry-run
   empty; `cluster.check` clean.

3. **Retire the flat StatefulSet** — only once step 2 is green:
   - `kubectl -n seaweedfs delete statefulset seaweedfs-volume`
   - Delete PVCs `mount0-seaweedfs-volume-{0,1,2}` (reclaimPolicy `Delete` frees the local-path
     dirs). **Irreversible** — the old copies are gone; do it only after G-swfs confirms the
     data is on the `hdd` group.
   - `spec.volume` stays in the CR (removing it panics the operator) — now a pure stub that
     creates no servers.

**Post:** `cluster.check` shows 5 volume servers (3 `hdd` + 2 `ssd`); **G-swfs** green; bulk on
`hdd`, `ssd` servers empty (until Phase 3). ~hours over nebula.

**Phase 3 — migrate Forgejo git → SSD** (copy-cutover runbook below). Its `seaweedfs-ovh-ssd`
PVC (`diskType: ssd`) places git volumes on the `ssd` group. **Post-checks:** **G-public** (a
`git clone`/push works), the new PVC bound on `seaweedfs-ovh-ssd`, and the Haku dashboard load
benchmarked vs. the old latency.

### Stage 2 — mount rename + etcd onto NVMe (rolling, node-by-node)

Two rolls interleaved per node: **(E)** move the etcd quorum from
{`102453`, `103656`, `103711`} to {`103656`, `104952`, `104963`}, and **(R)** wipe+rename
each data disk to `local-path-ovh-{hdd,ssd}`. Rules: **one etcd membership change at a
time** with **G-etcd** green between each; **never wipe both KS-GAME at once** (**G-cnpg**);
the data-disk wipe (R) is independent of etcd (which lives on the install disk).

Anchor = `103656` (the KS-5 node that stays control-plane throughout and holds the final
HDD etcd seat). It is the current leader, so no opening `move-leader` is needed — just
confirm leadership is on `103656` and keep it there; no membership step ever touches the
leader. Do the TF primary/bootstrap reassignment (`102453` → `103656`, see Final state)
before promoting any KS-GAME node.

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
S3-backed and Valkey is cache. Anything left that is **not** on the disposable list
**halts the roll**. Otherwise print the disposable disks about to be destroyed and get an
explicit ack. HDD-destined DBs (Case A above) need no pre-step — draining forces the
re-clone, and the re-pinned `local-path-ovh` (`tier=hdd`) `allowedTopologies` lands it on a
KS-5 node. The two SSD-destined DBs (Case B) are migrated separately by clone-and-cutover,
not by these node drains. Ensure `wayback-archive-db` is already 2-instance before draining
its KS-GAME node.

Per-node order — each fenced by **G-all** + **G-losable** before and after:

1. **`ovh-ns104952` → SSD control-plane.** Verify re-clone sources (**G-cnpg**),
   cordon+drain. Wipe+rename NVMe#2 → `local-path-ovh-ssd` (R). Promote to control-plane →
   joins etcd (learner → voter). **Post:** **G-etcd** shows 4 healthy members incl.
   `104952`; **G-swfs**/**G-cnpg** re-replicated.
2. **`ovh-ns103711` → worker.** One membership change: remove from etcd, reprovision as
   worker; wipe+rename `/dev/sdb` → `local-path-ovh-hdd` (R). **Post:** **G-etcd** back to 3
   {`102453`, `103656`, `104952`}.
3. **`ovh-ns104963` → SSD control-plane.** As step 1. **Post:** **G-etcd** 4 members incl.
   `104963`.
4. **`ovh-ns102453` → worker (big 3.6 TB HDD).** Membership change: remove from etcd,
   reprovision as worker; drain its `/dev/sdb` local-path PVs, wipe+rename →
   `local-path-ovh-hdd` (R). **Post:** **G-etcd** = target {`103656`, `104952`, `104963`}.
5. **`ovh-ns103656` — data disk only.** Stays control-plane (anchor); do **not** touch its
   etcd / `/dev/sda`. Drain its `/dev/sdb` local-path PVs, wipe+rename → `local-path-ovh-hdd`
   (R). **Post:** **G-swfs**/**G-cnpg** green.
6. **Move the SSD-tier DBs (Case B).** Migrate `forgejo-db` and `seaweedfs-filer-db` to
   `local-path-ovh-ssd` via the `cnpg_region_switch` clone-and-cutover (new cluster → stream
   → promote → repoint app → delete old), one at a time (**G-cnpg** between); repoint Forgejo
   and the filer respectively. Verify the `filer-db` clone before deleting the source (SSOT,
   no external backup). Leave every other DB on HDD. Re-check Nebula lighthouse placement now
   that `102453`/`103711` are workers (lighthouse role is per-node, not tied to k8s
   control-plane role).

**Exit:** matches the Final-state table; watch `ControlPlaneLeasePutLatency*` drop as etcd
fsync moves onto NVMe.

### Stage 3 — third SSD node (optional, future)

Buy 1 NVMe OVH box; add as control-plane (learner → voter), then remove `103656` (the last
HDD etcd seat) → all-NVMe 3-member quorum; bump the SeaweedFS `ssd` group to `replicas: 3`. Same
one-member-at-a-time **G-etcd** discipline.

## Forgejo git → SSD migration runbook (Stage 1)

`storageClassName` is immutable on a PVC, so this is a copy-cutover (same shape as the
2026-06-28 RWO→RWX migration), not a manifest flip.

1. Create `forgejo-git-rwx-ssd` (RWX, `storageClassName: seaweedfs-ovh-ssd`, 50Gi).
2. Scale Forgejo to 0 (maintenance window).
3. Copy `forgejo-git-rwx` → `forgejo-git-rwx-ssd` (helper pod mounting both, `cp -a` /
   `rsync -a`). Verify repo counts + a `git fsck` sample.
4. Point the chart's `persistence.claimName` at `forgejo-git-rwx-ssd`
   (<../../k8s/forgejo/app/helmrelease.yaml>, <../../k8s/forgejo/app/git-storage-pvc.yaml>).
5. Scale up; verify `git.allegedly.works` and a clone/push. Bench the Haku dashboard load.
6. Retain the old PVC briefly, then delete once satisfied.
