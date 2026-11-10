# Platform Sessions PVC backup and restore drill

This records the 2026-11-10 backup-coverage audit and prepares a synthetic restore
drill. It is not a production backup configuration. The repository and live
Kubernetes audit found no configured backup path for these claims; it does not rule
out an independently operated backup outside those scopes. A synthetic-only drill
can use its own disposable repository without selecting a production destination or
deciding the policy for real captures. That policy is required before backing up
real capture data.

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

The repository and live API audit found no VolSync `ReplicationSource` or
`ReplicationDestination` in `platform-sessions`, no `VolumeSnapshotClass`, and no
`VolumeSnapshot` or `VolumeSnapshotContent` objects. No configured volume-level
recovery path was found for either claim type within those scopes. This does not
establish that no external or separately managed backup exists. The old, unsuffixed
SeaweedFS provider PVCs remain Bound, but the active Pods mount the new `*-piraeus`
and `*-captures` claims; the old claims are migration leftovers, not configured
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

## Disposable synthetic restore drill

This drill needs no production destination or production capture-retention
decision. It uses a test-only Restic REST repository backed by a new, dedicated
PVC in the drill namespace, with fresh test-only HTTP and Restic passwords. Do not
reuse the `session-backup` service/PVC, a production bucket or repository, a
production identity, or a production password. Keep this repository writable only
for the drill; never configure production snapshots or retention there. This
in-cluster repository makes the mechanics testable without a separate service
selection, but it cannot prove recovery after loss of the cluster. Production
recovery goals that include cluster loss need a separate off-cluster destination.

The repository already uses VolSync's Restic mover with `copyMethod: Direct` for
other apps. For this isolated drill, create a temporary `restic-test-server`
Service on port 8000 and `ReplicationSource` objects in the test namespace. Back
the server with its own 1 GiB `piraeus-hdd-local` PVC and run it with
`--private-repos --append-only`. Give it a test-only `.htpasswd` Secret and the
existing pinned `restic/rest-server` image. Use repository URLs of the form
`rest:http://restic-test-server:8000/<test-user>/state` and
`rest:http://restic-test-server:8000/<test-user>/captures`. Each VolSync
repository Secret contains its URL as `RESTIC_REPOSITORY`, its independent
test-only encryption password as `RESTIC_PASSWORD`, and the test-only HTTP
credentials as `RESTIC_REST_USERNAME` and `RESTIC_REST_PASSWORD`. Restore with a
temporary Restic client Pod that reads only the matching test repository Secret
and mounts a fresh destination PVC. Use no production Secret or endpoint.
The repository pins VolSync chart 0.16.0. Its [Restic mover documentation]
describes repository Secret keys as Restic environment variables, and its
[release notes] record support for `RESTIC_REST_USERNAME` and
`RESTIC_REST_PASSWORD`. The pinned `restic/rest-server` v0.14.0 [README] documents
private per-user repositories, nested paths such as `/foo/bar`, and append-only
backup creation. These interfaces support the planned credential and path shape.
This exact VolSync/server setup has not been run, so initialize and check both
disposable repository paths before backing up the source PVCs; if path
initialization fails, use one test user per repository at the user-root path.
The repository has no Restic `ReplicationDestination` construct today, so this
drill tests VolSync backup plus direct Restic restoration; it does not test
VolSync-managed restore orchestration.

1. In a new namespace named `platform-sessions-restore-drill`, create the
   test-only REST server and repository PVC, plus `state-source` (1 GiB,
   `piraeus-ssd-2-autodiskful`) and `captures-source` (1 GiB,
   `piraeus-hdd-local`). Give every test object the
   `app.kubernetes.io/part-of=platform-sessions-restore-drill` label. Generate
   fresh random HTTP Basic and Restic encryption passwords for this test and
   expose them only as namespace-scoped Secrets. Use separate repository paths
   for state and captures. Do not reference any production PVC, Secret, bucket,
   or provider Pod.
2. Write only synthetic fixtures: a JSON session-state file, a small SQLite
   database with one synthetic row, and a `.mitm`-named opaque fixture file. Put
   `SYNTHETIC_ONLY_NOT_A_CREDENTIAL` in each fixture. Close/checkpoint SQLite
   before backing it up. Record a SHA-256 manifest and fixture file modes.
3. Run one VolSync Restic backup for each source PVC to its matching test-only
   repository. Require a successful, timestamped completed backup for each
   claim. Verify repository integrity using the same test-only credentials;
   never enumerate or prune a production repository during this drill.
4. Restore each repository's latest snapshot into new empty `state-restored`
   and `captures-restored` PVCs in the test namespace using a temporary Restic
   client Pod. The source claims remain untouched. Verify the restored SHA-256
   manifest and modes, run SQLite `PRAGMA integrity_check`, confirm its expected
   synthetic row and the JSON fixture marker, and compare the opaque `.mitm`
   fixture byte-for-byte with its recorded digest. Record the source and restored
   PVC/PV names, backup timestamp, and pass/fail results, but do not retain fixture
   contents or credentials in the report.
5. Before cleanup, prove all PVCs and Pods carrying the drill label are in the
   test namespace and that each PV's claim reference and LINSTOR volume handle
   belong to the drill. These classes use `Retain`, so deleting the namespace
   does not necessarily remove the PV or LINSTOR volume. Delete only the named
   drill PVCs and then clean up only their verified drill PVs/volume handles
   using the Piraeus cleanup procedure. Do not bulk-delete Released PVs. Remove
   the test-only repositories only after verifying they contain no other data.
   If ownership is ambiguous, leave the resource and report it for review.

The drill passes only when both independent restores complete into fresh claims,
the repository integrity checks pass, and all restored data matches the synthetic
manifest. The local Restic round trip below does not prove VolSync scheduling,
Piraeus provisioning, repository reachability, credential recovery, VolSync restore
orchestration, application startup, or recovery after cluster loss. No cluster
resources were applied as part of this audit.

[Restic mover documentation]: https://volsync.readthedocs.io/en/stable/usage/restic/index.html
[release notes]: https://github.com/backube/volsync/blob/v0.16.0/CHANGELOG.md
[README]: https://github.com/restic/rest-server/blob/v0.14.0/README.md

## Safe synthetic check already run

A local Restic 0.18.1 repository was created under the task scratch directory with
a randomly generated test-only password file, the three synthetic fixtures above,
and no cluster or network endpoint. `restic backup`, `restic check --read-data`,
and `restic restore` completed; `diff -r` matched both trees and SQLite
`PRAGMA integrity_check` returned `ok`. The check proved only local Restic file
round-trip behavior. It wrote no production backup and did not exercise Kubernetes,
VolSync, Piraeus, or the live provider PVCs.
