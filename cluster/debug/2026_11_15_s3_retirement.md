# S3 migration retirement gates

Observed on 2026-11-15 UTC against `api.allegedly.works`, SeaweedFS `4.53`
and operator `1.0.39`. The initial inventory used read-only operations; the later
approved application probes and obsolete-key revocations are recorded below. Follow the [retirement runbook](../docs/runbooks/seaweedfs_s3_retirement.md)
for each later stage; do not publish the next removal before its live gate.

## Session archive: stage reclaim policies

At 10:12:52–10:12:59, a complete inventory found 38 Buckets, 39 S3Identities,
39 S3Credentials, one S3Policy and one S3PolicyBinding. Resolving each identity's
`spec.name` (defaulting to its resource name) and same-namespace credential
references found exactly one identity and one credential for
`session-backup-db-backups`, both in `session-backup`. Both were Ready at
observed generation 1, with `Retain` and their protection finalizers present.
There was no matching Bucket CR or surviving Bucket access writer.

At 10:19:07–10:19:10, independent `s3.bucket.list` reads through
`seaweedfs-filer-0.seaweedfs-filer-peer.seaweedfs.svc:8888` and
`seaweedfs-filer-1.seaweedfs-filer-peer.seaweedfs.svc:8888` found the old backup
bucket absent. Earlier `collection.list` reads found no exact or related backup
collection. Both filers still reported its IAM identity with one key. The sole
policy binding, `seaweedfs/claude-reader-buckets`, targets only `claude-reader`.

The live `session-archive-db-hdd` had no backup, external clusters or recovery
bootstrap; WAL archiving was disabled. The web Deployment and all three importer
CronJobs referenced the database/Restic credentials, not the retired S3 Secret.
The actual session Restic repository and its credentials remain active.

The policy staging PR [#16658](https://github.com/agentydragon/ducktape/pull/16658)
merged before the next live check. At 10:39:26, Action
`e2c8d24e-6390-4574-9da8-218488efd66d` independently confirmed exactly one
identity and one credential claimant. Both retained session resources had
`reclaimPolicy: Delete`, generation 2 and observed generation 2, Ready, no deletion
timestamp, and their respective protection finalizers present.

The credential-only PR [#16664](https://github.com/agentydragon/ducktape/pull/16664)
merged at 10:48:45. At 10:49:41 its credential was still present, so credential
finalization is still a live gate. Keep the dedicated identity present until
all-key absence through both filers and owned Secret deletion are verified.
Remove the identity in a later change, then verify no user/key recreation after
reconciliation. The first Action stopped on an unrelated Mimir consumer preflight
before any probes or IAM mutations; its session policy observations passed
independently.

## Mimir, Tempo and Langfuse: obsolete keys revoked

The applications reference their tenant-local `*-seaweedfs-credentials`. The old
and current access-key fields were present and different for each tenant
(compared only in memory). Each current Secret is owned by its tenant-local
S3Credentials; each rollback Secret has no controller owner and is Flux-managed.
The complete IAM inventory found exactly one identity and one credential claimant
per application.

A broad consumer preflight initially stopped on the historical
`reloader.stakater.com/last-reloaded-from` annotation in Mimir's gateway Deployment
and Tempo's StatefulSet. A separate read projected the matching object names and
paths. The revised preflight excluded only that annotation within
`metadata.annotations`; every other specification and annotation remained in the
check. All Pods, Deployments, StatefulSets, CronJobs and HelmReleases in each
application namespace passed with zero legacy Secret consumer references.

At 10:49:41–10:50:25, approved Action
`e3dcf294-25a8-4443-9a45-82b6fdc9c0ba` verified replacement credentials against
`mimir-blocks`, `mimir-ruler`, `tempo` and `langfuse`. Each probe conditionally
created a unique tiny object, read back identical bytes, deleted precisely its
created object and confirmed absence with a 404. No existing objects were changed.

Only after every probe and the exact two-key IAM preconditions passed did the
Action revoke the three obsolete keys. Independent reads through both filers
confirmed exactly one active current key and no obsolete key for every tenant,
then repeated those checks after ten seconds. The Action exited 0 with no output
truncation. No credentials were printed or persisted. This permits removal of the
three legacy rollback Secret resources; preserve all existing data buckets,
current credentials, identities and grants.

Initial read-only Action receipts: `41063c2d-ea07-4b7e-b5c5-283bc9490508`
(inventory) and `6372eb74-3b4d-4f4e-9e96-ab8187612d52` (direct filer and policy
reads).
