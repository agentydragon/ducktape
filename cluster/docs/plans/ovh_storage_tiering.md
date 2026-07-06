# Plan: OVH data-disk mount rename (storage tiering — Stage 2 remainder)

**Status: active — the 3 HDD nodes are renamed; the 2 SSD control-plane nodes remain.** The
etcd-onto-NVMe control-plane reshuffle is done (control plane `{103656, 104952, 104963}`, etcd
on 2× KS-GAME NVMe + the KS-5 anchor `103656`; `102453`/`103711` are workers), and the
foundation is in place: media-scoped StorageClasses (`local-path-ovh-{hdd,ssd}`), the SeaweedFS
`volumeTopology` layer (`hdd` 3× KS-5, `ssd` 2× KS-GAME; operator 1.0.30), Forgejo git + both
SSD-destined DBs on SSD.

**Done (2026-07-05): all three KS-5 HDD nodes renamed** off `/var/mnt/seaweedfs-data` →
`/var/mnt/local-path-ovh-hdd` — `103711` and `102453` (workers) and `103656` (the CP anchor;
etcd on `/dev/sda` untouched, no reboot). Each was: SeaweedFS volume-server evacuated → FUSE
consumers refreshed → node cordoned+drained → `tofu apply -target` repartition → recovery (CNPG
re-clones, disposable STS recreated, SeaweedFS server rebuilt). The per-node mechanism
(`data_disk_mount_renamed_nodes` opt-in + `nodePathMap` flip) is built and merged.

**What's left:** rename the **2 SSD control-plane nodes** (`104952`, `104963`) off
`/var/mnt/seaweedfs-data` → `/var/mnt/local-path-ovh-ssd`. This is harder — see
"SSD-node rename" below (only 2 SSD SeaweedFS servers at repl `001`, so no evacuation buffer;
accepted approach is a gated single-copy window). Then an optional **Stage 3** (3rd NVMe box).

Reference material from the completed foundation work:
<../lessons_learned/2026_07_04_seaweedfs_volumetopology_and_operator.md>,
<../lessons_learned/2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md>,
<../runbooks/seaweedfs_pvc_storageclass_migration.md> (reusable PVC storage-class migration),
<../lessons_learned/2026_06_19_etcd_hdd_io_contention.md> (the etcd motivator, now resolved).

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

### The per-node rename mechanism (built)

Two per-node surfaces, both live:

1. Talos side: the UserVolume name is `contains(data_disk_mount_renamed_nodes, node) ?
"local-path-ovh-${storage_tier}" : "seaweedfs-data"` (<../../terraform/main/ovh-nodes.tf>) —
   an opt-in `toset()`; empty = no-op, add one hostname to roll that node.
2. local-path side: that node's `nodePathMap` entry in
   <../../k8s/local-path-provisioner/helmrelease.yaml> flipped to `/var/mnt/local-path-ovh-hdd/local-path`
   in the **same commit**.

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
3. **Warn before wiping any single-copy disk.** These local-path PVCs are not replicated, so
   the wipe destroys them. The per-node preflight (**G-losable**) must **list every single-copy
   PVC on the node and halt for explicit acknowledgment**, and must **halt on any single-copy
   disk in neither list below** so a newly-created one is never destroyed silently. Two classes:

   **Recreate-empty (disposable) — recreate blank, no backup:**
   - `agent-box/agent-box-root`
   - `gecko/gecko-root`
   - `codex-nix-pod/codex-home`
   - `codex-nix-pod/codex-nix-store`

   **Back up → restore (kept on `local-path` by decision, not migrated to `seaweedfs-ovh`):**
   these hold real state, so per node holding one: **down the app → back up the PVC → wipe →
   restore into the re-provisioned PVC → verify perms/mode → restart**.
   - `grocy-sf/grocy-config-ovh`, `grocy-vallejo/grocy-config-ovh` — grocy config/uploads;
     existing backups available.
   - `langfuse/data-langfuse-zookeeper-0` — langfuse ZooKeeper (ClickHouse ReplicatedMergeTree
     metadata; **not** safe to recreate empty). Down langfuse so the ZK data dir is quiescent
     before copying. These three currently sit on `ovh-ns103711`.

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

### HDD nodes — done (2026-07-05)

All three KS-5 HDD nodes are renamed (`103711`, `102453` workers; `103656` CP anchor). Per node:
evacuate its SeaweedFS volume server → refresh FUSE consumers → cordon+drain → `tofu apply
-target` repartition (`auto` mode, **no reboot** — verified, and etcd on the anchor's `/dev/sda`
was untouched) → recovery. None had backup-restore PVCs on it (the grocy/zk backups landed only
on `103711`). Operational lessons that generalize to the SSD nodes below:

- **`volumeServer.evacuate` aborts on the first transient gRPC error** (the lossy nebula overlay
  — see <2026_07_05_nebula_overlay_packet_loss_investigation.md>, issue #2917). Wrap it in a
  **retry loop** that re-runs until the server shows 0 volumes; a single blip no longer stalls it.
- **The last volume often won't evacuate** because it ended up over-replicated (3 copies) from
  interrupted moves — `evacuate` can't move it (all servers have it) and `fix.replication` may
  delete the wrong copy. It's **safe to wipe anyway** (the wipe drops the redundant copy → back
  to 2). Or force it off with `volume.delete -volumeId X -node <server>` (deletes one replica,
  not all — but a full/`ReadOnly:true` volume may ignore it).
- **CNPG re-clone** = `kubectl delete pvc <inst> --wait=false; kubectl delete pod <inst> --force`
  → the operator re-clones onto the fresh disk. All instances came back 2/2 fast.
- **Post-wipe recovery**, per node: reconcile `local-path-provisioner` (nodePathMap flip via
  Flux) → uncordon → CNPG re-clones → delete-and-recreate the disposable STS PVCs (valkey caches,
  `loki-write`/`mimir-ingester`/`alertmanager`, SeaweedFS volume-server PVC) → GC the Released
  old-path PVs (strip `pvc-protection` finalizer; the local-path cleaner can't reach the wiped
  dir). The ConfigMap is `local-path-storage/local-path-config` (not `-provisioner-config`).

### SSD-node rename (`104952`, `104963`) — remaining, harder

The two KS-GAME NVMe nodes still mount NVMe#2 at `/var/mnt/seaweedfs-data`; target
`/var/mnt/local-path-ovh-ssd`. Same mechanism, but **no evacuation buffer**: the SSD SeaweedFS
tier has only **2** volume servers at repl `001`, so a volume's two copies are on those two
servers — you can't evacuate one and stay 2-copy (no third server to hold the moved replica).

**Accepted approach (decided 2026-07-05): ride a bounded single-copy window, one node at a
time.** The user OK'd intermittent 1-copy for the SSD data.

- **Back up first** (insurance for the one fatal case — the _surviving_ SSD node dying during the
  window): snapshot the SSD SeaweedFS data (Forgejo git) to **off the SSD tier** (HDD or
  external). Accepting 1-copy is fine; a double-failure with no backup is not.
- **One SSD node at a time.** Wipe ssd-0 → its volumes ride **1 copy on ssd-1** → node returns →
  `volume.fix.replication -apply` back to 2 → **verify 2-copy (G-swfs) before touching ssd-1.**
  Never wipe the second SSD node until the first's re-replication is complete.
- **SSD-pinned CNPG** (`forgejo-db-ssd`, `seaweedfs-filer-db-ssd`) likewise ride a
  single-healthy-instance window — the re-clone can't land until the drained node returns (no
  third SSD node). Same one-at-a-time gating.
- These are **control planes**: drain is more sensitive (respect G-etcd; the data-disk repartition
  is `/dev/sdb`-equivalent NVMe#2, no reboot, etcd on NVMe#1 untouched — as on the `103656`
  anchor). **No Forgejo downtime without approval** — the SSD rename touches Forgejo git's hot
  tier, so confirm before the cutover.
- **Alternative:** do **Stage 3 first** (3rd NVMe box → bump `ssd` group to `replicas: 3`), then
  the SSD nodes evacuate exactly like the HDD ones — zero single-copy window. Cheaper on risk,
  costs hardware. The rename is cosmetic, so deferring is also fine.

**Finalize (after SSD nodes):** re-check Nebula lighthouse placement (`102453`/`103711` are now
workers; lighthouse role is per-node, not tied to k8s role). **Exit:** every OVH data disk at
`/var/mnt/local-path-ovh-{hdd,ssd}`, `nodePathMap` pointing there, `seaweedfs-data` name retired.

### Control-plane membership checklist (any CP add/remove)

Changing which nodes are control-plane is **not** just the Terraform `role` field. The Stage-2
reshuffle flipped the TF roles but left three downstream rosters on the old CP set, which broke
devel (`test_nebula_mesh`/`test_dns_records`) and, worse, silently mis-pointed live etcd metrics
and the `api.allegedly.works` record at nodes that no longer served them. Any CP add/remove
(Stage 3's `103656` removal + new-box addition, or a future reshuffle) must update **all** of
these in the same change — the two validation tests enforce the last three:

- `cluster/terraform/main/ovh-nodes.tf` — the node `role` field (the actual CP membership).
- `cluster/terraform/main/infrastructure.tf` — `primary_controlplane_ip` + the
  `talos_machine_bootstrap`/`talos_cluster_kubeconfig` `ignore_changes` guards, if the anchor
  CP moves.
- `nebula-mesh.json` — the per-host `role` (leave `lighthouse`/`relay`/`cert_groups` alone;
  they're per-node, not role-tied).
- `cluster/k8s/monitoring/etcd/endpoints.yaml` — the etcd metrics scrape EndpointSlice (must
  list exactly the nodes running etcd, or `ControlPlaneLeasePutLatency` alerts point nowhere).
- `tf/gitops/dns-records/main.tf` — `kube_api_ips` (the `api.allegedly.works` A records must be
  the CPs' public IPs; a demoted node has no apiserver on `:6443`).

### Stage 3 — third SSD node (optional, future)

Buy 1 NVMe OVH box; add as control-plane (learner → voter), then remove `103656` (the last HDD
etcd seat) → all-NVMe 3-member quorum; bump the SeaweedFS `ssd` group to `replicas: 3`. Same
one-member-at-a-time **G-etcd** discipline. Apply the control-plane membership checklist above
for both the add and the removal.
