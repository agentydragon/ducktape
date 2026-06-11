# ns103711: `u-seaweedfs-data` UserVolume claimed but not mounted at `/var/mnt/seaweedfs-data`

**Date**: 2026-06-03
**Status**: Resolved — reboot fixed the mount; grocy-vallejo data recovered
from orphan local-path dir; grocy-sf and 3 MCP valkeys abandoned (caches,
recovery not worth it).

## Symptom

Workloads scheduled to `ovh-ns103711` whose PVC uses `local-path-ovh` fail
mount with:

    MountVolume.SetUp failed for volume "pvc-…":
    mkdir /var/mnt/seaweedfs-data/local-path: read-only file system

10+ PVCs in this state today (`grocy-{sf,vallejo}/grocy-config-ovh`,
`tana-mcp/{mcp-valkey-ovh-0,tana-mcp-config-ovh}`, `monitoring/{db-alertmanager-monitoring-1,grafana-db-ovh-4,storage-mimir-ingester-1}`,
`loki/data-loki-write-1`, `seaweedfs/seaweedfs-filer-db-4`, `manifold-mcp/manifold-valkey-kimsufi-0`,
`plaid-mcp/plaid-valkey-kimsufi-1`).

## What's actually going on

`/dev/sdb` on ns103711 IS claimed by the Talos `UserVolumeConfig`
named `seaweedfs-data` (volume id `u-seaweedfs-data`) — verified by
`talosctl wipe disk sdb` refusing with:

    rpc error: code = FailedPrecondition desc =
    blockdevice "sdb" is in use by volume "u-seaweedfs-data"

But the volume isn't _mounted_ in Talos's root mount namespace.
Side-by-side comparison of `/proc/self/mountinfo`:

**Working node ns103656:**

    85   65  8:16  /                          /var/mnt/seaweedfs-data
                                                                     rw,relatime shared:40 - xfs /dev/sdb
    3207 65  8:16  /local-path/pvc-…/alertmanager-db
                   /var/lib/kubelet/.../pvc-…/alertmanager/3 rw,relatime shared:39 - xfs /dev/sdb

`u-seaweedfs-data → /var/mnt/seaweedfs-data` shows up in
`talosctl get volumemountstatus`.

**Broken node ns103711:**

    1317 66  8:16  /local-path/pvc-3bd966f9-…/alertmanager-db
                   /var/lib/kubelet/.../pvc-3bd966f9-…/alertmanager/3
                                                                     rw,relatime shared:39 - xfs /dev/sdb

Only the kubelet bind-mount. No top-level `/var/mnt/seaweedfs-data` entry,
no `u-seaweedfs-data` in `talosctl get volumemountstatus`.

`/var/mnt/seaweedfs-data` exists as a _directory_ in the read-only Talos
root, so `mkdir /var/mnt/seaweedfs-data/local-path` from
local-path-provisioner returns `EROFS`. Pods whose subpath got
bind-mounted _before_ the top-level mount disappeared (e.g.
`alertmanager-monitoring-1` PVC `pvc-3bd966f9`) keep working — the
kernel maintains the bind-mount even after its source mount went away.
Anything new gets EROFS.

## What it isn't

- Not a config-not-applied problem: `talosctl get mc v1alpha1 -o yaml` on
  ns103711 contains the `UserVolumeConfig name=seaweedfs-data
diskSelector="disk.dev_path == '/dev/sdb'" xfs` — same as the working
  node.
- Not a wrong-disk problem: `talosctl get disks` shows `/dev/sdb` exists
  on ns103711 (2.0 TB HGST), same as ns103656.
- Not a filesystem-corruption problem: `talosctl get discoveredvolume sdb`
  reports it as xfs cleanly.
- Not a "Talos doesn't know about the volume" problem: the wipe refusal
  proves Talos's volume controller has bound `u-seaweedfs-data` to sdb.

## Hypothesis — confirmed via Talos resource diff

Mount step of the Talos VolumeConfigController failed silently on ns103711
at some point (likely during the OVH talos-kimsufi-worker-1 → ovh-ns103711
rename window), leaving the volume in a half-attached state: the
`block.VolumeConfigController` claim succeeded, but the mount into
`/var/mnt/seaweedfs-data` never happened or got torn down without
re-attempting. There's no automatic re-mount loop.

Side-by-side resource diff confirms:

| Resource                             | ns103656 (working)                                                         | ns103711 (broken)       |
| ------------------------------------ | -------------------------------------------------------------------------- | ----------------------- |
| `VolumeConfig u-seaweedfs-data`      | present                                                                    | present                 |
| `VolumeStatus u-seaweedfs-data`      | `ready /dev/sdb 2.0 TB`                                                    | `ready /dev/sdb 2.0 TB` |
| `MountStatus u-seaweedfs-data`       | `/dev/sdb → /var/mnt/seaweedfs-data xfs`                                   | **MISSING**             |
| `VolumeMountStatus u-seaweedfs-data` | requester=`block.VolumeConfigController`, target=`/var/mnt/seaweedfs-data` | **MISSING**             |

Provisioning ran fine; mounting didn't. `tofu plan` shows "No changes" —
the static config is identical to source-of-truth. The break is at
controller-runtime state, not config drift.

## Recovery path

In escalating order:

1. **Diff TF-rendered config against running config** — `tofu plan`
   reports "No changes". No source-of-truth drift. _Done._
2. **`talosctl apply-config --mode=no-reboot`** with identical content —
   _Done; no effect._ Talos accepted ("Applied configuration without a
   reboot") but no controller action because the resource version didn't
   bump. `VolumeMountStatus` for `u-seaweedfs-data` still missing.
3. **`talosctl patch mc --mode=no-reboot`** trying to add a real
   optional field to the `UserVolumeConfig` (`mount.permissions`,
   `provisioning.label`) — _Done; both rejected._ Talos 1.12.3's
   `UserVolumeConfig` schema accepts only `name`, `volumeType`,
   `provisioning.diskSelector.match`, `filesystem.type` — no room to add
   a benign field. Observation during these attempts: the
   `block.MountController` IS active (it bounced `STATE` on `/dev/sda2`
   during the apply window), so the controller isn't dead — it's just
   skipping `u-seaweedfs-data` as already-handled.
4. **`talosctl wipe disk sdb`** — _Tried; refused._ `FailedPrecondition`
   because Talos's volume controller still claims `sdb` for
   `u-seaweedfs-data`.
5. **Reboot ns103711** — _Done at 2026-06-03T05:21:38Z._ Fresh boot
   triggered `VolumeConfigController` to re-evaluate from scratch and the
   `u-seaweedfs-data` mount came back at `/var/mnt/seaweedfs-data`. All
   local-path PVCs on ns103711 stopped EROFS-failing immediately.

## Data recovery (post-reboot)

The reboot fixed _new_ mounts, but the PVCs that were re-provisioned
_during_ the broken window (because local-path-provisioner had no
choice but to fail and the Deployment-managed pods got a fresh PVC bind
the next time scheduling succeeded) ended up pointing at empty
freshly-created `pvc-<NEW>_*` directories under
`/var/mnt/seaweedfs-data/local-path/`. The _original_ `pvc-<OLD>_*`
directories from before the rename were still on disk — local-path-
provisioner can't delete what it can't see, and the XFS itself was
intact the whole time. The pre-rename data was sitting in plain sight
once the mount was restored.

Affected `*_*_grocy-config-ovh` / `*_*_*-config-ovh` / valkey PVCs on
`/var/mnt/seaweedfs-data/local-path/` of ns103711:

| PVC                                        | Old (orphan, pre-rename) dir               | New (live, empty post-rename) dir |
| ------------------------------------------ | ------------------------------------------ | --------------------------------- |
| `grocy-vallejo/grocy-config-ovh`           | `pvc-0f9e70aa-…` (1.1 MB grocy.db, May 28) | `pvc-dc9393a2-…` (empty init)     |
| `grocy-sf/grocy-config-ovh`                | `pvc-572285c3-…` (27.8 MB)                 | `pvc-95037cb1-…`                  |
| `tana-mcp/tana-mcp-config-ovh`             | `pvc-51e79822-…`                           | `pvc-cd86430d-…`                  |
| `grocy-vallejo/grocy-vallejo-valkey-ovh-0` | `pvc-2b2a718d-…`                           | `pvc-02215dc2-…`                  |
| `grocy-sf/grocy-sf-valkey-ovh-0`           | `pvc-34f3ee92-…` (12 KB)                   | `pvc-96b25ac0-…`                  |
| `tana-mcp/mcp-valkey-ovh-0`                | `pvc-28ef594a-…` (1.4 MB)                  | `pvc-e84f509c-…`                  |
| `manifold-mcp/manifold-valkey-kimsufi-0`   | `pvc-63f745b9-…` (1.3 MB)                  | `pvc-0577d98a-…`                  |
| `plaid-mcp/plaid-valkey-kimsufi-1`         | `pvc-e7dec31d-…` (12 KB)                   | `pvc-88a99395-…`                  |

The home-dir snapshots taken as a precaution _before_ the reboot were
taken _after_ the underlying PVC had already been silently
re-provisioned (the pod kept Running on a stale bind-mount that
local-path had handed it pointing at a brand-new empty dir). So they
captured the empty state too, not the real data:

- `~/grocy-vallejo-backup-20260603T044718Z` (1.2 M) — **empty/fresh
  Grocy install, identical schema to a 0-row DB.** Useless for recovery.
- `~/tana-mcp-backup-20260603T045527Z` (58 M) — unverified post-incident;
  may or may not be the real Tana profile.

### What got recovered (2026-06-03)

- **grocy-vallejo**: recovered. Paused volsync RS, scaled grocy Deployment
  to 0, moved empty dir aside (`.empty-post-rename` suffix), `cp -a` old
  orphan dir over the new path via a hostPath pod in `local-path-storage`
  (sandbox/grocy-vallejo PSA = baseline blocks hostPath; local-path-storage
  is enforce=privileged). md5 verified end-to-end. Re-scaled to 1, resumed
  volsync. Recovered DB: 310 products, 278 stock, 1042 stock_log, 5
  shopping_list, 3 users, 15 locations.
- **grocy-sf**, **tana-mcp/mcp-valkey-ovh-0**, **manifold-mcp/
  manifold-valkey-kimsufi-0**, **plaid-mcp/plaid-valkey-kimsufi-1**,
  **grocy-sf/grocy-sf-valkey-ovh-0**: abandoned. Orphan dirs deleted.
  Valkeys are caches; grocy-sf was deemed not worth restoring.
- **tana-mcp/tana-mcp-config-ovh** (`pvc-51e79822-…`) and
  **grocy-vallejo-valkey-ovh-0** (`pvc-2b2a718d-…`): orphans left on
  disk for now (not in either recovery or abandon scope).
- **grocy-vallejo `.empty-post-rename` suffix dir** (`pvc-dc9393a2-…
_grocy-vallejo_grocy-config-ovh.empty-post-rename`): left on disk
  until grocy-vallejo is confirmed healthy for a few days, then delete.

## Backup-design lesson

The volsync `copyMethod: Direct` rsync pattern overwrites the only
backup in place. When the source PVC went empty post-rename, the
06:29 UTC scheduled run almost overwrote the seaweedfs-backed
`grocy-config-ovh-backup` PVC with the empty source — destroying the
only off-PVC copy of real data. Recovery only worked because
local-path's orphan dir on `/var/mnt/seaweedfs-data` was untouched.

Follow-up: switch volsync configs to snapshot-with-retention or to
Restic-to-object-storage so a source going empty (rename, wipe,
accidental delete-and-recreate) adds a snapshot rather than destroying
the prior one. Tracked in `cluster/docs/plan.md` (Next Actions).

## Related

- `2026_06_02_seaweedfs_volume_loss_ovh_rename.md` (bulk volume loss from
  same rename window — different mechanism, same root operation).
- `2025_11_17_zombie_kubelet_dual_ip.md` (Talos service half-state
  pattern).
