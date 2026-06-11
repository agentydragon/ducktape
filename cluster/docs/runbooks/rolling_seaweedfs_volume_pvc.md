# Runbook: rolling a SeaweedFS volume-server PVC

This procedure is the safe way to delete and recreate the `local-path-ovh`
PVC of one SeaweedFS volume-server pod — e.g. as part of renaming the OVH
node it lives on, swapping in new hardware, or recovering from a corrupt
local disk.

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

# Identify the StatefulSet ordinal pinned to that node:
kubectl get pods -n seaweedfs -o wide | grep seaweedfs-volume
# e.g. seaweedfs-volume-1 lives on ovh-ns103656

# Patch the Seaweed CR to scale only that ordinal out:
# (depends on operator; safest: scale the volume server StatefulSet to 0
# replicas via the Seaweed CR's volume.count or via direct kubectl scale)
kubectl scale statefulset -n seaweedfs seaweedfs-volume --replicas=<N-1>
```

(Alternative: just delete the volume-server pod and let the SS recreate it
once you've moved its PVC. The pod will go into ContainerCreating until
its PVC is back.)

### 3. **GATE**: wait for replication to converge

This is the step we skipped on 2026-06-02.

```bash
# Run until output is "no under-replicated volumes found" (no -n: actually fix).
# Re-run periodically; convergence is rate-limited by how fast volumes can be
# copied between DataNodes (~tens of MB/s per volume).
kubectl exec -n seaweedfs seaweedfs-filer-0 -- weed shell <<'EOF'
volume.fix.replication
EOF
```

For our ~700 MB working set this takes single-digit minutes. **Do not
proceed to step 4 until this returns clean.** If you're rolling multiple
volume-server nodes (e.g. node-rename project), repeat this gate **between
each one** — losing two DataNodes' worth of volumes in sequence without
re-replication is exactly the failure mode we hit.

### 4. Delete the PVC and let it rebind on the renamed/new node

```bash
kubectl delete pvc -n seaweedfs mount0-seaweedfs-volume-<ordinal>
# local-path-ovh is hostname-pinned in the HelmRelease nodePathMap; if
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

As of 2026-06-02 all three OVH volume servers advertise the same rack id
(`hil-ovh-h109b04`). `001` replication then falls back to "any other
DataNode" rather than "another rack" — i.e. we have single-node tolerance,
not rack tolerance. Until rack labels are fixed (see
`cluster/docs/plan.md` Next Actions), treat **any concurrent loss of two
volume-server pods or their PVCs as a data-loss event** regardless of what
the policy nominally says.

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
