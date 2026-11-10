# Platform Sessions PVC backup and restore drill

This records the 2026-11-10 backup-coverage audit and prepares a synthetic restore
drill. It is not a production backup configuration: no backup owner or repository
currently covers these claims, so the in-cluster drill must wait for a reviewed
backup destination and capture-retention decision.

## Coverage found

The remote provider code declares six 1 GiB state PVCs on
`piraeus-ssd-2-autodiskful` and six 20 GiB capture PVCs on `piraeus-hdd-local`
([PVC declarations](../../cdk8s/platform_sessions/remote.py)). The six active
provider Pods each mount one claim of each type. Live metadata showed every
PVC and its PV Bound with those classes; all twelve PVs use `Retain`. The two-copy
SSD class keeps synchronous replicas on
distinct OVH nodes; that supports availability and node recovery, but it does not
provide an independent, point-in-time backup. The capture class is strict-local
with one copy ([Piraeus class contract](../piraeus.md#storage-classes)).

The capture sidecar writes daily `.mitm` files. Recording is enabled through at
least January 2027; the files can contain session credentials and private
financial, medical, and conversation data. The current runbook says there is no
automatic capture retention or cleanup and that a single HDD copy is not a backup
([recording policy](platform_sessions_remote.md#upstream-traffic-recording)).
Whether to retain these files in a second system, for how long, and who may restore
them needs an explicit owner decision before backing up real captures.

No VolSync `ReplicationSource` or `ReplicationDestination` exists in
`platform-sessions`; the cluster has no `VolumeSnapshotClass` and no
`VolumeSnapshot` or `VolumeSnapshotContent` objects. There is no configured
volume-level recovery path for either claim type. The old, unsuffixed SeaweedFS
provider PVCs remain Bound, but the active Pods mount the new `*-piraeus` and
`*-captures` claims; the old claims are migration leftovers, not configured
backups for the new claims.

The separate `session-backup` service stores workstation Restic repositories on
its own `session-backup-data-hdd` claim. Its documented inputs are workstation
Claude and Codex directories, not Platform Sessions provider PVCs. The archive's
CNPG database is a rebuildable projection of those workstation repositories; its
PVC is not a provider-state backup. Neither service covers the twelve claims above
([session-backup scope and recovery notes](../../k8s/session-backup/README.md)).

## Read-only audit commands

These metadata-only queries reproduce the live check without reading Secrets or
volume contents:

```sh
kubectl get pvc -n platform-sessions \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,CLASS:.spec.storageClassName,CAPACITY:.status.capacity.storage'
kubectl get pods -n platform-sessions \
  -o=jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.volumes[*]}{.persistentVolumeClaim.claimName}{" "}{end}{"\n"}{end}'
kubectl get pv -o json | jq -r '
  .items[] |
  select(.spec.claimRef.namespace == "platform-sessions" and
    ((.spec.claimRef.name | endswith("-piraeus")) or
     (.spec.claimRef.name | endswith("-captures")))) |
  [.spec.claimRef.name, .status.phase, .spec.storageClassName,
   .spec.persistentVolumeReclaimPolicy, .spec.capacity.storage] | @tsv'
kubectl get replicationsources.volsync.backube,replicationdestinations.volsync.backube \
  -n platform-sessions
kubectl get volumesnapshots.snapshot.storage.k8s.io,volumesnapshotcontents.snapshot.storage.k8s.io -A
kubectl get volumesnapshotclass.snapshot.storage.k8s.io
kubectl get pvc -n session-backup \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,CLASS:.spec.storageClassName,CAPACITY:.status.capacity.storage'
```

A missing result is evidence only for the resource/API queried. Do not infer backup
coverage from a Bound PVC, `Retain` reclaim policy, two Piraeus replicas, or the
presence of a backup controller elsewhere in the cluster.

## Disposable restore drill to run after backup ownership is chosen

Use a dedicated encrypted test repository or bucket/prefix with test-only
credentials. It must not share the production repository, identity, encryption password, or
retention/prune policy. Prefer a recovery destination outside the Piraeus cluster
failure domain if the intended guarantee includes loss of the cluster. The repo
already uses VolSync's Restic mover with `copyMethod: Direct` for other apps, and
no CSI `VolumeSnapshotClass` is installed; any new implementation still needs its
own reviewed owner, security policy, and network access.

1. Get approval for the backup destination and an explicit capture policy first:
   whether raw captures are backed up, the retention period, authorized restore
   principals, and the deletion process. Keep state and capture repositories or
   prefixes separate if they need different access or retention. A synthetic
   capture fixture tests the restore mechanics without authorizing backup of live
   captures. If live captures stay excluded, record that as an explicit policy.
2. In a new namespace named `platform-sessions-restore-drill`, create synthetic
   source PVCs: `state-source` (1 GiB, `piraeus-ssd-2-autodiskful`) and
   `captures-source` (1 GiB, `piraeus-hdd-local`). Give every test object the
   `app.kubernetes.io/part-of=platform-sessions-restore-drill` label. Do not
   reference any production PVC, Secret, or provider Pod.
3. Write only synthetic fixtures: a JSON session-state file, a small SQLite
   database with one synthetic row, and a `.mitm`-named opaque fixture file. Put
   `SYNTHETIC_ONLY_NOT_A_CREDENTIAL` in each fixture. Close/checkpoint SQLite
   before backing it up. Record a SHA-256 manifest and fixture file modes.
4. Back up the two source claims with the selected mechanism to the test-only
   repository. Require a successful, timestamped completed backup for each
   claim. Verify repository integrity from the same test-only credentials; never
   enumerate or prune a production repository during this drill.
5. Restore each backup into new empty `state-restored` and `captures-restored`
   PVCs in the test namespace. The source claims remain untouched. Verify the
   restored SHA-256 manifest and modes, run SQLite `PRAGMA integrity_check`, and
   verify that every restored file contains only the synthetic marker. Record
   the source and restored PVC/PV names, backup timestamp, and pass/fail results,
   but do not retain fixture contents or credentials in the report.
6. Before cleanup, prove all PVCs and Pods carrying the drill label are in the
   test namespace and that each PV's claim reference and LINSTOR volume handle
   belong to the drill. These classes use `Retain`, so deleting the namespace
   does not necessarily remove the PV or LINSTOR volume. Delete only the named
   drill PVCs and then clean up only their verified drill PVs/volume handles
   using the Piraeus cleanup procedure. Do not bulk-delete Released PVs. Remove
   the test-only repository/prefix only after verifying it contains no other
   data. If ownership is ambiguous, leave the resource and report it for review.

The drill passes only when both independent restores complete into fresh claims,
the repository integrity check passes, and all restored data matches the synthetic
manifest. A successful local Restic round trip does not prove VolSync scheduling,
Piraeus provisioning, repository reachability, credential recovery, or application
startup.

## Safe synthetic check already run

A local Restic 0.18.1 repository was created under the task scratch directory with
a randomly generated test-only password file, the three synthetic fixtures above,
and no cluster or network endpoint. `restic backup`, `restic check --read-data`,
and `restic restore` completed; `diff -r` matched both trees and SQLite
`PRAGMA integrity_check` returned `ok`. The check proved only local Restic file
round-trip behavior. It wrote no production backup and did not exercise Kubernetes,
VolSync, Piraeus, or the live provider PVCs.
