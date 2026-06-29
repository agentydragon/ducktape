# SeaweedFS bulk volume loss during OVH node rename

**Date**: 2026-06-02
**Status**: Data loss confirmed. Recovery is partial (no backups for the affected buckets).
**Supersedes**: <2026_06_02_loki_compactor_seaweedfs_replication_race.md> (which had the wrong RCA — replication race for a single object).

## TL;DR

While renaming the three OVH worker/CP nodes that host SeaweedFS volume servers
(pilots 2, 3, 4 of the rename project), each pilot included deleting the
SeaweedFS volume server's `local-path-ovh` PVC. We did three of these PVC
deletes in close succession without verifying replication had converged
between them. The result: every SeaweedFS volume id in the range **0–150**
is gone from the cluster (master reports `volume id N not found`). Only
volumes 151–210 — created **after** the third volume server came back online —
survived. Filer metadata still references the dead volume ids, so any S3
object stored before the rename window is now an unreadable pointer.

```text
$ kubectl exec -n seaweedfs seaweedfs-master-0 -- \
    curl -s "http://localhost:9333/dir/lookup?volumeId=113"
{"volumeOrFileId":"113","error":"volume id 113 not found"}
```

This matches the broader cluster status:

| volume server                                | Volumes | VolumeIds | data PVC age |
| -------------------------------------------- | ------- | --------- | ------------ |
| `seaweedfs-volume-0` (ovh-ns104952, pilot 4) | 0       | _empty_   | 11h          |
| `seaweedfs-volume-1` (ovh-ns103656, pilot 3) | 40      | 171–210   | 13h          |
| `seaweedfs-volume-2` (ovh-ns102453, pilot 2) | 60      | 151–210   | 13h          |

The three data PVC ages match the three pilot windows in which we wiped them.
Volumes 151–170 only have one replica (volume-2); 171–210 have two
(volume-1 + volume-2); volume-0 hasn't been picked back up for any volumes yet.

## Symptom that surfaced first

`loki-backend-{0,1}` crash-looping with:

```text
init compactor: failed to init delete store: unexpected EOF
error initialising module: compactor
```

Loki's compactor reads `loki/index/delete_requests/delete_requests.gz`
unconditionally on startup. That object was a single chunk pointing at a
dead volume; the filer reported size 135 B (its metadata row), but reading
the chunk gave `unexpected EOF` because the volume hosting the bytes no
longer exists. We "fixed" it by deleting the object, which let Loki start —
but that was treating the symptom, not the disease.

The disease was much wider. Forgejo's `init-app-ini` then crash-looped on
`input/output error` reading its own `app.ini` (chunk on dead volume 113),
which is what made us realize this was systemic.

## Blast radius

Scanned every bucket by walking filer entries and tallying `volume_id`
of each chunk. `≤150` = dead, `≥151` = alive.

| bucket                                 | files                                                | dead chunks                                | alive chunks         | notes                                                                              |
| -------------------------------------- | ---------------------------------------------------- | ------------------------------------------ | -------------------- | ---------------------------------------------------------------------------------- |
| `forgejo` (S3)                         | 1                                                    | 1 (v131)                                   | 0                    | a single orphan avatar                                                             |
| `pvc-7c70e9bf…` (gitea-shared-storage) | 14                                                   | 18 (v113–v118)                             | 0                    | Forgejo bootstrap scaffolding only — see below                                     |
| `augur-assets`                         | 6                                                    | 6 (v61, v63, v65, v66)                     | 0                    | the six landing-page jpegs                                                         |
| `loki`                                 | 15                                                   | 11 (v31, v33, v35, v67, v68, v71–v74, v76) | 4 (v191, v192, v194) | indexes from before/during the rename window are unreadable; new ones start in 19x |
| `mimir-blocks`                         | 84                                                   | **1098** (v13–v18, v101, v102)             | 96 (v183, v184)      | most metric history is gone                                                        |
| `mimir-ruler`                          | 0                                                    | 0                                          | 0                    | empty bucket post-incident                                                         |
| `tempo`                                | 1                                                    | 1 (v27)                                    | 0                    | only the cluster seed file existed; no real traces                                 |
| `vm-images`                            | 0 chunked                                            | 0                                          | 0                    | bucket exists but no data chunks                                                   |
| `drivefs-artifacts`                    | 0 chunked                                            | 0                                          | 0                    | bucket exists but no data chunks                                                   |
| `listing-monitor-captures`             | 0 chunked                                            | 0                                          | 0                    | bucket exists but no data chunks                                                   |
| `attic`                                | 0                                                    | 0                                          | 0                    | (also no `attic` collection in master layouts — bucket was empty)                  |
| `pvc-6938027d…`                        | 35                                                   | 0                                          | 48 (v207, v208)      | intact (created post-rename)                                                       |
| `pvc-a107c6d9…`                        | 7                                                    | 0                                          | 1 (v199)             | intact                                                                             |
| `pvc-e899889b…`                        | 1                                                    | 0                                          | 0                    | empty                                                                              |
| `pvc-a4af7238…`                        | (no top-level dir, collection has writables 209/210) | 0                                          | 0                    | created post-rename, empty                                                         |

Workload-level impact:

- **Forgejo**: was a fresh install bootstrapped 2026-06-01 23:07Z (commit
  68bd9926e, "switch git forge from Gitea to Forgejo"). The 14 unreadable
  files are all init-time scaffolding — `app.ini`, JWT/SSH host keys,
  search-index meta, queue cursors. The CNPG DB `forgejo-db` has zero
  user-defined tables in either `forgejo` or `postgres` databases — Forgejo
  never finished its first schema migration because `init-app-ini` has been
  failing on the corrupt `app.ini` for 10+ hours. **No user repos / users /
  issues existed.** Recovery is "delete the PVC, re-init."
- **augur-assets**: 6 landing-page jpegs gone. Regeneratable from source.
- **Loki**: deleted the corrupt cursor object earlier today; old index
  chunks now point at dead volumes so old log windows are gone. New
  ingest (post-recovery) is fine.
- **mimir-blocks**: this is the worst one. ~1100 chunks of historical
  metric blocks are unreadable. Anything in the retention window before
  the rename is effectively gone.
- **Tempo / vm-images / drivefs-artifacts / mimir-ruler /
  listing-monitor-captures**: bucket scaffolding only, no real
  user-significant data lost.

## Root cause

### What we did

OVH rename pilots 2, 3, 4 each renamed one of the three nodes hosting a
SeaweedFS volume server (`seaweedfs-volume-{2,1,0}` on `ovh-ns102453`,
`ovh-ns103656`, `ovh-ns104952`). Each pilot's runbook included:

1. drain + cordon the node
2. delete the SeaweedFS volume server's `local-path-ovh` PVC (because
   `local-path` PVs are pinned to the old hostname and need to be
   recreated under the new hostname)
3. apply the rename, reboot, uncordon
4. let the new volume server come up with an empty disk and rejoin the
   cluster

`defaultReplication: "001"` is supposed to make this safe — one extra
copy on a different rack/node — so any given pilot losing one volume
server's disk should be survivable.

### Why "001" wasn't enough

`001` means "one extra copy on a different rack, fall back to a different
DataNode if no other rack is available." All three of our volume servers
are labelled with the **same** rack (`hil-ovh-h109b04`), so SeaweedFS
falls back to "a different DataNode" — i.e. one of the other two volume
servers.

With three volume servers and replication factor 2, a volume can survive
losing any one server. It **cannot** survive losing two servers without
re-replication in between.

We deleted three volume-server PVCs in the span of a few hours without
ever verifying that the surviving servers had finished re-replicating to
the (newly empty) replacement server before moving on to the next pilot.
By pilot 4, every original volume id had been on a PVC we'd deleted at
some point, and re-replication had not converged in time. The cumulative
result is total loss of volumes 0–150.

### Why we didn't notice immediately

SeaweedFS does not aggressively GC filer metadata when a volume
disappears. The filer's row for `app.ini` still says "size 1534, chunk
fid 113,098c359de28a3d" — perfectly valid-looking metadata. The lookup
only fails when something actually tries to fetch the chunk bytes.
For long-lived workloads, that often only happens on restart (Loki
compactor, Forgejo init container) — so the corruption stayed invisible
until pods rolled.

## How we should have done it

### Per-pilot procedure for any SeaweedFS-volume-hosting node

Full procedure lives in <../runbooks/rolling_seaweedfs_volume_pvc.md>. Short
version follows.

Before deleting the volume server's PVC, confirm the cluster can tolerate
losing this server:

```bash
# Inside any filer / master / volume pod
weed shell <<EOF
volume.list
volume.fix.replication -collectionPattern=* -volumeIdPattern=* -n
EOF
```

`volume.list` should show every collection's volume ids replicated on
≥2 distinct DataNodes. `volume.fix.replication -n` (dry-run) will list
any under-replicated volumes.

After bringing the renamed node back up (new empty volume server registered),
**wait for re-replication to finish** before starting the next pilot:

```bash
# Run periodically until under-replicated count is 0
weed shell <<EOF
volume.fix.replication -collectionPattern=*
EOF
```

For our cluster, with 50 GB of SeaweedFS data, re-replication takes on the
order of tens of minutes to a couple of hours. Treat this as a hard gate
between pilots.

### Architectural improvements

1. **Volsync the things we care about.** `gitea-shared-storage` (Forgejo)
   had no `ReplicationSource`. Only `grocy-{sf,vallejo}` and `tana-mcp`
   are currently volsync'd. Anything that lives on SeaweedFS and isn't
   trivially regeneratable should have an off-SeaweedFS backup. (CNPG
   barman-backed Postgres clusters are separately fine; this is about
   the file PVCs.)
2. **Real rack diversity, or accept the constraint.** All three OVH
   volume servers are labelled `rack=hil-ovh-h109b04`. If we want
   `001` to mean what it says, label them with their actual physical
   placement (or just `rack=$nodeName` as a degenerate but correct
   value). Until then, treat the cluster as "no rack-failure
   tolerance, only single-node-failure tolerance."
3. **Don't roll volume-bearing nodes from the same project that's
   also stressing the control plane.** The CNPG operator was thrashing
   throughout the rename window;
   SeaweedFS rebalancing competes with that for the same
   already-stressed Talos nodes. Plan rolling-PVC operations as their
   own change window with the API server idle.
4. **Mimir/Loki/Tempo backends should use real S3 or replicated object
   storage.** Backing observability storage with the same SeaweedFS we
   put on home-grade hardware made all three useless at once. If those
   are intended to survive cluster-level failures, they should live on
   off-cluster object storage (B2 / R2 / OVH Object Storage / etc.).

## Recovery

- **forgejo**: delete `gitea-shared-storage` PVC, let Forgejo re-init.
  Nothing of value to preserve. (The `forgejo` S3 bucket has only one
  orphan avatar — also safe to drop.)
- **augur-assets**: regenerate the six jpegs and re-upload.
- **mimir-blocks**: accept the loss. Going forward, anything pre-rename
  is gone. New blocks (collection writables 183, 184) are fine.
- **loki**: already done (corrupt cursor object deleted; backends up;
  pre-rename log windows lost).
- **tempo / vm-images / drivefs-artifacts**: nothing meaningful was lost.

For each affected workload we should also `weed shell` walk the bucket
and delete the filer entries pointing at dead volumes — otherwise list
operations will keep returning these phantom paths and any future
reader will get `unexpected EOF` again.

## Open hardening followups

(Cross-reference these into `cluster/docs/plan.md` Next Actions.)

- Add `ReplicationSource` for `gitea-shared-storage` (Forgejo) and any
  other SeaweedFS-backed PVC that holds non-regeneratable state.
- Decide whether observability storage stays on SeaweedFS or moves to
  off-cluster object storage.
- Fix SeaweedFS rack labels so `001` policy gives the protection it
  promises.
- Carry a local patch / file upstream: Loki compactor's `init delete
store` should at least skip-on-corruption for a single-file cursor
  rather than blocking the whole backend.
- Document a "rolling SeaweedFS volume server PVC" runbook with the
  `volume.fix.replication` gate above; reference it from any future
  node-rename plan.
