# SeaweedFS stale FUSE mount cache after volume-server evacuation (2026-07-04)

## Summary

During OVH storage tiering Stage 1 Phase 2 we evacuated the three flat SeaweedFS
volume servers (`volume.move` onto the new `hdd` topology group), verified every
volume was 2-copy on the `hdd` servers, then **deleted the flat `seaweedfs-volume`
StatefulSet + its PVCs**. Shortly after, git operations against Forgejo started
failing intermittently:

- `git clone`/`ls-remote` over SSH: ~1 in 5 attempts returned `Forgejo: Failed to
execute git command`.
- `git push` (both Forgejo and, reported by the user, `gaffer-private`):
  `remote: unpack-objects died of signal 7` → `remote unpack failed` → `remote
rejected (unpacker error)`. **Signal 7 is SIGBUS.**

## Root cause

The Forgejo git repos live on a SeaweedFS RWX PVC served through a `weed mount`
FUSE client (the `seaweedfs-csi-driver-mount` DaemonSet). That client caches each
volume's location (a `vidMap`: volume id → volume-server address).

`volume.move` relocated the chunks and the master's location index updated
correctly (`volume.list` showed every volume on `seaweedfs-volume-hdd-*`). But the
long-lived FUSE clients — running since **before** the migration — kept serving
their **cached** locations, still pointing moved chunks at
`seaweedfs-volume-{0,1,2}`. Once we deleted the flat StatefulSet, those pod/service
DNS names stopped resolving, so a read of any chunk whose cached location was a flat
server failed with:

```text
read http://seaweedfs-volume-1.seaweedfs-volume-peer.seaweedfs:8444/305,... failed:
  dial tcp: lookup seaweedfs-volume-1... no such host
fetching chunk ...ducktape.git/objects/pack/pack-219a...idx: ... no such host
```

git reads pack indexes via `mmap`; an I/O error on a memory-mapped page is delivered
as **SIGBUS**, which killed `upload-pack`/`unpack-objects`.

**Why it did not self-heal:** the client retried the _same_ dead hosts with
exponential backoff and never re-resolved from the master. A SeaweedFS client only
re-looks-up a volume's location gracefully when the old server is **alive** and
answers "volume not found" (HTTP 404). A **deleted** server (DNS `no such host`)
never triggers the re-lookup — so the stale cache is permanent until the mount is
refreshed.

## Blast radius

3 of 5 `seaweedfs-csi-driver-mount` pods held stale caches (707 / 416 / 55 errors).
Both Forgejo replicas were affected (hence the intermittency — SSH load-balances
across them). Other `seaweedfs-ovh` consumers on the same mounts (paperless, a
grocy backup Job, litellm's auth PVC, a codex workspace) were latent risks.

## The mistake (ordering, not the refresh itself)

The evacuation procedure was **move → verify → delete**. The missing step was
refreshing (or letting self-heal) the FUSE clients **while the emptied servers were
still alive**. We deleted the servers before any client had re-resolved, and the
alive-but-empty server — the graceful fallback that turns a stale read into a clean
re-lookup — no longer existed.

## Correct sequence (retire a volume server with no window)

Server deletion must be the **last, quiescence-gated** step:

1. `volume.move` all data off the server — it is now empty but **stays running**.
2. Verify 2-copy on the destination group (**G-swfs**).
3. **Refresh clients while the emptied server is still alive.** Either passively
   (clients self-heal on the next read: the alive-but-empty server returns 404 →
   client re-looks-up → reads from the destination group) optionally nudged by a
   grace period, or deterministically by **rolling the consumer pods**. Because the
   old server still resolves, there is no window — every read resolves to either the
   alive-empty old server (→ re-lookup) or the new copy.
4. Confirm the emptied server sees **no more volume-data requests** in its logs
   (all clients have re-resolved).
5. **Only then** delete the StatefulSet + PVCs.

## Recovery (from the already-broken state)

Once the servers are gone there is no zero-window fix — the window already exists.
Refresh the stale clients:

- **Rolling the consumer pod** refreshes its mount: NodeUnpublish tears down the
  stale `weed mount` subprocess, NodePublish respawns a fresh one that rebuilds the
  `vidMap` from the master. Verified: after `kubectl -n forgejo rollout restart
deploy/forgejo`, stale errors dropped to 0, `ls-remote` went 8/8, and a real
  push (`unpack-objects`) succeeded. Repeated for paperless + codex-pod; all mount
  pods reached 0 stale errors.
- Deleting the `seaweedfs-csi-driver-mount` pods also works but is more disruptive
  (it breaks every mount on the node). The DaemonSet is `updateStrategy: OnDelete`
  precisely because restarting a mount is disruptive — prefer rolling consumers.

## Prevention

- Volume-server retirement runbook updated with the "keep the emptied server alive,
  refresh clients, then delete" sequence: <../runbooks/rolling_seaweedfs_volume_pvc.md>.
- The OVH tiering plan's Stage 2 (which retires/moves volume servers again) carries
  the same gotcha: <../plans/ovh_storage_tiering.md>.
