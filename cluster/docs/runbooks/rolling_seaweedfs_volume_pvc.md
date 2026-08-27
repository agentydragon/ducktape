# Runbook: rolling a SeaweedFS volume-server PVC

This procedure is the safe way to delete and recreate the local-path PVC of
one SeaweedFS volume-server pod — e.g. as part of renaming the OVH node it
lives on, swapping in new hardware, or recovering from a corrupt local disk.

Since the 2026-07 storage-tiering migration the volume layer runs as two
`volumeTopology` groups, so the StatefulSet and PVC names depend on the
node's tier: KS-5 nodes host `seaweedfs-volume-hdd-*`
(`mount0-seaweedfs-volume-hdd-<ordinal>`, class `local-path-ovh-hdd`); KS-GAME
nodes host `seaweedfs-volume-ssd-*` (`…-ssd-…`, class `local-path-ovh-ssd`).
Substitute the right tier below.

**Why this runbook exists**: On 2026-06-02 we did three of these PVC deletes
back-to-back across pilots 2–4 of the OVH node rename project without
waiting for replication to converge between them. Result: every SeaweedFS
volume id ≤150 became unreachable (most of `mimir-blocks`, all pre-rename
`loki` indexes, the freshly-bootstrapped Forgejo `app.ini`, the `tempo`
seed, six `augur-assets` jpegs). See
<../lessons_learned/2026_06_02_seaweedfs_volume_loss_ovh_rename.md> for the
full RCA. **The gate in step 3 is what would have prevented that.**

## Preconditions

- SeaweedFS cluster is otherwise healthy (`weed shell volume.list` returns
  the expected layout).
- `defaultReplication` is `001` or stronger — losing one DataNode must be
  survivable. Verify in the Seaweed CR or `kubectl exec ... weed shell` →
  `volume.list`.
- You have the SeaweedFS [rack-labels caveat](#caveat-rack-labels) in mind
  (see below).

## Procedure

### 1. Confirm the cluster is fully replicated _before_ you start

```bash
kubectl exec -n seaweedfs seaweedfs-filer-0 -- weed shell <<'EOF'
volume.fix.replication -n
EOF
```

`-n` is dry-run. If output is anything other than "no under-replicated
volumes found", **stop**. There's pre-existing under-replication; rolling
another PVC now is one bug away from data loss. Resolve it first (often
just runs to completion by removing `-n` and waiting).

### 2. Drain + cordon the target node, scale the volume server to 0

```bash
kubectl cordon <node>
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data

# Identify the tier StatefulSet + ordinal pinned to that node:
kubectl get pods -n seaweedfs -o wide | grep seaweedfs-volume
# e.g. seaweedfs-volume-hdd-2 lives on ovh-ns103656 (KS-5 → hdd group)

# Scale that tier's StatefulSet down by one (seaweedfs-volume-hdd or
# seaweedfs-volume-ssd). The operator reconciles replicas from the Seaweed CR's
# volumeTopology.<tier>.replicas, so for a lasting change lower it there; a
# direct kubectl scale is fine for a transient roll (operator will restore it).
kubectl scale statefulset -n seaweedfs seaweedfs-volume-<tier> --replicas=<N-1>
```

(Alternative: just delete the volume-server pod and let the SS recreate it
once you've moved its PVC. The pod will go into ContainerCreating until
its PVC is back.)

### 3. **GATE**: wait for replication to converge

```bash
# Run until output is "no under-replicated volumes found" (no -n: actually fix).
# Re-run periodically; convergence is rate-limited by how fast volumes can be
# copied between DataNodes (~tens of MB/s per volume).
kubectl exec -n seaweedfs seaweedfs-filer-0 -- weed shell <<'EOF'
volume.fix.replication
EOF
```

For our ~700 MB working set this takes single-digit minutes. **Do not
proceed to step 4 until this returns clean.** Rolling multiple volume-server
nodes (e.g. the node-rename project)? Repeat this gate **between each one** —
skipping it between nodes is exactly the 2026-06-02 failure.

### 4. Delete the PVC and let it rebind on the renamed/new node

```bash
kubectl delete pvc -n seaweedfs mount0-seaweedfs-volume-<tier>-<ordinal>
# local-path-ovh-<tier> is hostname-pinned in the HelmRelease nodePathMap; if
# you're renaming the node, update nodePathMap first so the new PV lands
# in the right hostPath after rebind.
```

The SeaweedFS volume-server pod, once recreated by its StatefulSet, comes
up with an empty data directory and registers as a fresh DataNode. The
master starts assigning new (high-numbered) volume IDs to it. Replication
from the surviving DataNodes refills only volumes that have under-replicated
state after the join — which means **old volumes that lived only on the
wiped server are gone**. (Step 3's gate ensures this set was empty.)

### 5. Verify post-rebuild

```bash
kubectl exec -n seaweedfs seaweedfs-filer-0 -- weed shell <<'EOF'
volume.list
volume.fix.replication -n
EOF
```

`volume.list` should show the new DataNode picking up replicas. Dry-run
fix should be clean within a few minutes.

## Caveat: rack labels

Each topology group's servers share one rack id: the `hdd` group's three
KS-5 servers advertise `hil-ovh-h109b04`, the `ssd` group's two KS-GAME
servers advertise `hil-ovh-h108b01`. `001` means "2 copies, 2 DataNodes, 1
rack" — by design here, so it's **single-node tolerance within a tier, not
rack tolerance**. Treat **any concurrent loss of two volume-server pods or
their PVCs in the same tier as a data-loss event** regardless of what the
policy nominally says.

## Caveat: retiring a volume server (refresh FUSE clients before you delete it)

The steps above **roll** a PVC — the volume-server pod keeps its identity, so its
hostname keeps resolving. **Deleting a volume server outright** (removing its
StatefulSet, e.g. retiring the flat servers or reprovisioning a node in Stage 2 of
the tiering plan) is different and bit us on 2026-07-04: long-lived `weed mount`
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
