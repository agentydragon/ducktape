# Runbook: SeaweedFS Bucket CR adoption

**Status (2026-05-27)**: Resolved. SeaweedFS 4.x upgrade fixed 5 of 6
Bucket CRs (attic, augur-assets, mimir-blocks, mimir-ruler, tempo) by
registering the IAM gRPC service the operator needs. The remaining
`loki` CR was fixed by patching its `status.bucketName` (see below).

## Root cause

SeaweedFS 3.93 (the version that shipped via `chrislusf/seaweedfs:3.93`
in our previous Seaweed CR) does NOT register the
`iam_pb.SeaweedIdentityAccessManagement` gRPC service on any daemon.
The symbol is in `weed/pb/iam_pb/iam_grpc.pb.go` but never registered.

The seaweedfs-operator's bucket controller (`NewSwadminBucketAdmin`)
issues `weed shell` commands (`s3.bucket.access`, `s3.bucket.owner`),
which internally call the filer's `iam_pb` gRPC service. In 3.93 those
calls hit `Unimplemented` → reconcile fails with `AccessFailed` → the
operator never completes its post-create steps → every Bucket CR loops
in `Failed/BucketAlreadyExists`.

Commit `f41925b60` ("Embed IAM API into S3 server") landed in
SeaweedFS **4.03**. From that release on, the filer unconditionally
registers `iam_pb` on its existing gRPC port (18888 by default)
whenever `credentialManager` initializes successfully — which it
always does, even without `filer.s3.enabled` and without `-iam` flag.
Auth is opt-in via `jwt.filer_signing.key`; unauthenticated by default,
matching the rest of the filer's gRPC surface.

## Fix

Bump the Seaweed CR's image to a 4.x version. Currently pinned at
`chrislusf/seaweedfs:4.29` in
`cluster/k8s/seaweedfs/cluster/seaweed.yaml`.

The 3.93 → 4.29 upgrade was safe: no on-disk format changes for the
leveldb2 filer store, volume `.dat`/`.idx`, or `s3-config.json`. The
upgrade rolls the seaweed-operator-managed StatefulSets and Deployment
on next Flux reconcile — see `git log cluster/k8s/seaweedfs/cluster/seaweed.yaml`
for the version-bump commit.

After the rollout, existing Bucket CRs that aren't `Ready` get
re-reconciled automatically on the operator's standard interval. Most
adopt within ~30s.

## Adopting a pre-existing bucket (e.g. `loki`)

If `/buckets/<name>/` already exists on the filer but the Bucket CR
was never marked as having created it, the operator refuses adoption.
The check in `bucket_controller.go:174` is:

```go
if exists && bucket.Status.BucketName == "" {
    // BucketAlreadyExists / refusing to adopt
}
```

So the issue is not the filer directory mode — it's that the CR's
`status.bucketName` was never populated. Fix by patching the status
subresource:

```sh
kubectl -n seaweedfs patch bucket.seaweed.seaweedfs.com <name> \
  --subresource=status --type=merge \
  -p '{"status":{"bucketName":"<name>"}}'
```

The operator's next reconcile (≤30 s) skips the adoption check and
applies the rest of the spec (versioning, object lock, access). The
CR moves to `Ready`. No filer-side changes needed.

## How to verify iam_pb is registered

```sh
kubectl -n seaweedfs port-forward sts/seaweedfs-filer 18888:18888 &
grpcurl -plaintext localhost:18888 list
# Expected output:
#   filer_pb.SeaweedFiler
#   grpc.reflection.v1.ServerReflection
#   grpc.reflection.v1alpha.ServerReflection
#   iam_pb.SeaweedIdentityAccessManagement
```

The fourth entry is what was missing in 3.93.

## Filer startup log to look for

```text
filer.go:451 Registered IAM gRPC service on filer (unauthenticated;
  set jwt.filer_signing.key in security.toml to require admin Bearer
  token)
```

This line appears in the filer's stdout on 4.x startup when
`credentialManager` initialized successfully (which is always).

## Notes that turned out to be wrong (kept for the record)

**Misdiagnosis 1 — "operator bug stamping owner=''":** an earlier
version of this runbook attributed the failure to an operator bug
that "creates buckets with `owner: ""` and can't recognize its own
creates." The actual issue was simpler: the filer just wasn't running
the IAM gRPC service at all, so every operator call returned
`Unimplemented`. The "BucketAlreadyExists" was a downstream symptom,
not an adoption-logic bug.

**Misdiagnosis 2 — "loki failed because of 0o471 mode":** the next
revision claimed the operator refused adoption because of a
non-default filer dir mode. The operator code makes no such check;
`bucket_controller.go:174` only looks at `bucket.Status.BucketName`.
The 0o471 vs 0o777 mode difference was a red herring — the real cause
was the empty `status.bucketName`.

Both were resolved by reading the seaweedfs-operator 1.0.19 source
(`internal/controller/swadmin/`, `internal/controller/bucket_admin.go`,
`internal/controller/bucket_controller.go`) and the seaweedfs 3.93 vs
4.29 source (`weed/command/filer.go`, `weed/pb/iam_pb/iam_grpc.pb.go`)
locally cloned at `~/code/seaweedfs-operator` and `~/code/seaweedfs`.
