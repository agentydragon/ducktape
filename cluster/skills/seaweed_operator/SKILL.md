---
name: seaweed_operator
description: Safely inspect, change, migrate, or clean up SeaweedFS operator Bucket, S3Identity, S3Credentials, and related S3 objects in the cluster. Use before operating on these resources or their Flux ownership.
---

# SeaweedFS operator operations

These rules are verified against the deployed SeaweedFS operator chart `0.1.42`
(`appVersion: 1.0.39`, source tag `seaweedfs-operator-0.1.42`). Re-check the
upstream controllers when the chart changes:
<https://github.com/seaweedfs/seaweedfs-operator/tree/seaweedfs-operator-0.1.42>.

## Before changing anything

- Treat the SeaweedFS bucket and IAM key as external state, not disposable
  Kubernetes-only objects. Identify the physical bucket, current object count/size,
  IAM identity/key, target Secret, and owning Flux Kustomization first.
- Prefer the connected `haku-console` Kubernetes passthrough for reads. Use the
  `kubectl` escape hatch outside the sandbox only when the operator-linked RBAC
  cannot inspect the needed object. Never print Secret data or access-key values.
- Before removing or moving a stateful Flux owner, follow the parent Kubernetes
  instructions for `prune`, `deletionPolicy`, suspension, and orphaning. Commit and
  push before reconciling Flux.

## Bucket behavior

- A `Bucket` CR manages an external bucket selected by `spec.name` (falling back to
  `metadata.name`). `reclaimPolicy: Retain` removes the CR/finalizer but leaves the
  physical bucket. `Delete` deletes the physical bucket when the CR has recorded its
  bucket name. Use `Retain` for app data, backups, and non-reproducible contents.
- If the physical bucket exists, `adoptExisting: false` fails with
  `BucketAlreadyExists`. With `adoptExisting: true`, the operator records the
  existing physical name in the new CR and reconciles object lock, requested
  versioning, quota, owner, access, and placement. Adoption does not transfer or
  remove the old CR or its Flux inventory.
- An adopting CR created before the old owner is removed means two `Bucket`
  controllers temporarily reconcile the same physical bucket. Keep the specs
  identical, use this only for a staged handoff, and remove the old owner only
  after the new CR is Ready and the consumer's actual S3/backup path is verified.
  Never leave independent long-term owners for one physical bucket.
- A cross-namespace `spec.clusterRef` requires a `ResourceReferenceGrant` in the
  Seaweed namespace allowing the source namespace's `Bucket` to reference the
  specific `Seaweed` resource.

## Identity and credential behavior

- `S3Identity` claims a cluster-global IAM identity name. `S3Credentials` does not
  create an identity from `identityRef`: a same-namespace `S3Identity` resolves the
  effective IAM name; without one, `identityRef.name` is used literally.
- `S3Credentials` manages one IAM access key and mirrors it into `spec.secretRef`.
  A missing same-namespace Secret causes a fresh key pair to be generated, the
  Secret to be created, and the Secret to be controller-owned. An existing
  same-namespace Secret, even if incomplete, must already be marked
  operator-managed and owned by that exact `S3Credentials`; otherwise
  reconciliation fails with `SecretOwnershipConflict`. During a handoff, use a
  new Secret name instead of reusing the old owner's Secret.
- Multiple `S3Credentials` objects may target the same IAM identity and hold
  separate active keys. Switch the consumer to the new Secret and verify it before
  retiring the old credential. `Retain` leaves the old key behind; `Delete` removes
  the key recorded by that CR and deletes only a same-namespace Secret it controls.
  The operator never deletes a foreign Secret.
- A cross-namespace `spec.seaweedRef` requires a `ResourceReferenceGrant` in the
  Seaweed namespace. A cross-namespace `secretRef` requires a separate grant in the
  Secret's namespace, and that Secret must already exist with both configured
  fields; the operator will not create or delete a Secret in a foreign namespace.

## Safe handoff sequence

1. Preserve the existing Flux owner and its Bucket/S3Credentials resources.
2. Add the new Bucket with `adoptExisting: true`, matching access rules, and
   `reclaimPolicy: Retain`. Add a new tenant-local S3Credentials Secret name and
   `reclaimPolicy: Retain`, plus the required grants.
3. Wait for the new Bucket and S3Credentials to become Ready. Confirm the new
   Secret exists without exposing its values, switch the consumer, and perform an
   actual S3 read/write or backup verification.
4. Only then remove the old Flux-owned CRs. Removing an old credential with
   `Retain` intentionally leaves its old IAM key and Secret behind; handle any key
   revocation as a separate, explicit cleanup after the new path is proven.
