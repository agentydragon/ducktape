# S3 migration retirement gates

Observed on 2026-11-15 UTC against `api.allegedly.works`, SeaweedFS `4.53`
and operator `1.0.39`. No IAM, bucket, object or GitOps mutations were used to
collect this evidence. Follow the [retirement runbook](../docs/runbooks/seaweedfs_s3_retirement.md)
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

The next GitOps change stages `Delete` on the retained IAM resources while
keeping both present. After merge, verify applied policies, observed generations
and finalizers. Only then remove the obsolete credential, verify its recorded key
is absent through both filers and check its owned Secret's deletion. Remove the
dedicated identity last, then verify no user/key recreation after reconciliation.

## Mimir, Tempo and Langfuse: retained keys still require retirement

All observed live Pods, Deployments, StatefulSets and HelmReleases in `monitoring`,
`langfuse`, `session-backup` and `seaweedfs` had no references to the three legacy
rollback Secrets. The applications reference their tenant-local
`*-seaweedfs-credentials`; all three HelmReleases were Ready at their observed
spec generations (Mimir 17, Tempo 5, Langfuse 31).

The old and current access-key fields were present and different for each tenant
(compared only in memory). Each filer reported two IAM keys per tenant. The
current Secret is owned by the tenant-local S3Credentials; each rollback Secret
has no controller owner and remains Flux-managed. These facts prove configured
cutover, not actual replacement reads/writes or obsolete-key revocation. Verify
those paths, identify and revoke only the obsolete key, then confirm absence
through each filer before removing its rollback Secret. Preserve all data buckets.

Read-only Action receipts: `41063c2d-ea07-4b7e-b5c5-283bc9490508` (inventory)
and `6372eb74-3b4d-4f4e-9e96-ab8187612d52` (direct filer and policy reads).
