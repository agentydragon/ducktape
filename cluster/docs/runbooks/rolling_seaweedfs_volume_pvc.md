# SeaweedFS volume-server retirement and FUSE clients

The former local-path PVC roll procedure is obsolete. Volume servers now use
per-node `volumeTopology` groups on Piraeus local storage. For a node evacuation,
follow the [Seaweed volume-server procedure](piraeus_node_roll.md#seaweed-volume-servers),
including its replication and client-quiescence gates. Do not delete a PVC or scale a
StatefulSet until its exact consumers, replacement placement and replication safety
have been verified and the disruption has been approved.

The 2026-06-02 node-rename incident lost SeaweedFS volumes after several PVCs were
rolled without waiting for replication to converge between them. Its evidence and
recovery are in <../lessons_learned/2026_06_02_seaweedfs_volume_loss_ovh_rename.md>.

## Caveat: retiring a volume server (refresh FUSE clients before you delete it)

The steps above **roll** a PVC — the volume-server pod keeps its identity, so its
hostname keeps resolving. **Deleting a volume server outright** (removing its
StatefulSet, e.g. retiring the flat servers or reprovisioning a node) is
different and bit us on 2026-07-04: long-lived `weed mount`
FUSE clients cache each volume's location and only re-resolve gracefully when the
old server is **alive** and returns "volume not found". A **deleted** server (DNS
`no such host`) leaves the cache permanently stale → reads I/O-error → git `mmap`
→ **SIGBUS** (`unpack-objects died of signal 7`).

So make server deletion the **last, quiescence-gated** step: move the data off
(server stays running, empty) → verify 2-copy → **refresh the FUSE clients while
the emptied server is still alive** (roll the consumer pods, or let them self-heal
on the alive-empty server's 404→re-lookup) → confirm the emptied server logs no
more volume-data requests → _only then_ delete the StatefulSet + PVCs. Recovery if
you already deleted too early: `kubectl rollout restart` the consumer workloads
(NodeUnpublish→NodePublish respawns each mount with a fresh vidMap). Full RCA:
<../lessons_learned/2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md>.

## Quick sanity checks

```bash
# Are any volume ids unreachable? (master vs filer disagreement)
kubectl exec -n seaweedfs seaweedfs-master-0 -- \
  curl -s "http://localhost:9333/dir/lookup?volumeId=<id>"
# "volume id N not found" = that id is lost; filer entries pointing at
# it are unreadable.

# Bucket-level chunk audit (which chunks live on dead vs alive vol ids):
# see the recovery scripts referenced in the lessons-learned doc.
```
