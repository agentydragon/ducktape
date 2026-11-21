# Retire a SeaweedFS S3 tenant

Use this sequence after the old consumer has stopped using its S3 backend and the
replacement is verified. Deleting a Bucket, S3Credentials and S3Identity together
can recreate an IAM user after its identity finalizer completes. Zot did this on
2026-11-01 with operator `1.0.39` / chart `0.1.42` and filer `4.46`: the identity
delete succeeded, a concurrent write recreated it 27 ms later with one of its two
keys, and independent reads through both filers still listed it. #13590 removed all
three CRs at once; #13594 re-adopted the identity with `Delete` and #13595 removed it.
See [Cause and upstream status](#cause-and-upstream-status).

Keep each stage in GitOps and wait for its external-state gate before publishing
the next removal. A merged PR, Flux `Ready` or absence of CRs alone is insufficient.
Follow the [operator operations skill](../../skills/seaweed_operator/SKILL.md) for
ownership, grants, Secret handling and stateful Flux migration rules.

1. **Inventory and authorize.** Identify the physical bucket, every identity and
   credential claimant across namespaces, all bucket grants/policy bindings, target
   Secrets and Flux owners. Check actual bucket contents and current consumers. A broad
   scan for the legacy Secret name stops on Reloader's historical
   `reloader.stakater.com/last-reloaded-from` annotation (Mimir gateway, Tempo): exclude
   only that annotation under `metadata.annotations` and keep every other spec and
   annotation in the scan.
   Use `Delete` only for explicitly disposable or approved obsolete state. Keep
   shared identities and keys needed by another consumer.
2. **Stage policies.** Set intended `Delete` reclaim policies while keeping the
   resources. Wait for the applied specs and reconciled observed generations;
   verify their finalizers and pinned external names. A previous `Ready` phase can
   predate the policy change. Preserve the operator, Seaweed cluster and grants.
3. **Retire IAM writers and buckets.** Remove the obsolete Bucket CRs and policy
   bindings that target the retiring identity. Wait for finalizers to finish and
   verify the intended physical buckets and collections are gone. Keep the
   identity and credentials present. Ensure no surviving Bucket or policy writer
   can recreate or update that user. Preserve shared buckets; revoke only the
   retiring tenant's grants on them and verify reconciliation instead.
4. **Revoke credentials.** First probe the replacement credentials on each bucket:
   conditionally create a unique tiny object, read back identical bytes, delete exactly
   that object and confirm a 404, leaving existing objects untouched. Revoke only after
   every probe passes and IAM shows exactly the obsolete key and the current key.
   Remove obsolete S3Credentials with `Delete`, keeping
   the S3Identity present. Wait for finalizers and verify the recorded keys were
   revoked through each filer; compare keys in memory and report only booleans or
   counts. Check operator-owned Secret deletion separately. Foreign Secrets are
   not deleted by this controller; `Retain` intentionally leaves keys behind.
5. **Delete the dedicated identity last.** After all credential and other IAM
   writers for that user are gone, remove its S3Identity with `Delete`. Wait for
   finalization, then independently query every filer and confirm the named user
   and all its keys are absent. Check again after remaining GitOps owners have
   reconciled. Deleting a whole identity also deletes unmanaged or previously
   retained keys: inventory and authorization must cover those keys too.
6. **Remove supporting grants and owners.** Only after the external checks pass,
   remove the unused tenant reference grants, Flux dependencies and obsolete
   delivery Secrets. Check that the replacement consumer still works.

If a user remains after its CR disappears, stop cleanup at that boundary. Inspect
IAM and filer metadata evidence before retrying. Re-adopting only a confirmed
obsolete identity with `Delete`, waiting for `Ready`, then removing it in a
separate reconciliation succeeded for Zot; it is an explicit recovery operation,
not a substitute for staged deletion. Never print credential values or restore an
old S3Credentials merely to obtain a new deletion attempt: that can mint a key.

For proof, `s3.user.list` reports identity names and key counts. Query each filer
explicitly rather than relying on load balancing. Metadata journals under
`/topics/.system/log/YYYY-MM-DD/` can distinguish deletion from later recreation,
but their inline contents contain credentials: project only timestamps, paths,
event types and counts, and compare secrets only in memory. Segment names give only
their starting window, not a bound on the event timestamps inside, so scan
neighboring segments on every filer. Ordinary filer IAM RPC detail logs may also
include access-key values; sanitize before publication.

## Cause and upstream status

Proven: the delete was followed by a persisted recreation with exactly one of the two
keys, and two mutation paths are unprotected. The operator's
[`IAMClient.DeleteUser`](https://github.com/seaweedfs/seaweedfs-operator/blob/e424a372b7f7457175a4d1a2948807e534ec3725/internal/controller/swadmin/iam_client.go#L233)
takes no lock, while `DeleteAccessKey`, credential rewrites, policy and bucket-access
updates hold a process-global per-filer, per-user lock. The filer's
[`FilerEtcStore.DeleteAccessKey`](https://github.com/seaweedfs/seaweedfs/blob/4.46/weed/credential/filer_etc/filer_etc_identity.go#L451)
reads the identity, drops one key, then `saveIdentity` rewrites the whole file with no
existence or version check. Neither finalizer verifies that the user stays deleted.

High-confidence inference, because filer metadata journals record no IAM RPC or caller:
the credential controller read the user (keys A and B), the identity controller deleted
it, and the credential controller saved the old snapshot minus A. The second key's
origin was not established. An operator S3Credentials reconcile error (`not found`) in
the same second does not identify the failing path.

Ruled out for Zot: `Retain` as the applied policy, removal of the tenant grant (#13592,
merged later), a surviving name claimant, the Bucket deletion path (`s3.bucket.delete`
writes no IAM), a static filer `-s3.config` and an imperative IAM command from the
cleanup worker. A separate IAM client cannot be excluded without caller information.

Not fixed upstream, and no upstream issue is recorded here: operator `master` and filer
`4.53` (deployed) still have this code (read 2026-11-18). The fix is for `DeleteUser` to take
the existing user lock, tested with separate client instances: pause `DeleteAccessKey`
after its read, start the user delete, release it and expect the user to stay absent
(existing tests cover key rewrite versus key deletion only). That lock covers one
operator process; other clients, several filer addresses and stale read-modify-write
still need server-side atomic mutations or version-checked writes.
