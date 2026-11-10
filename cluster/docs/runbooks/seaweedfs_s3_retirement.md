# Retire a SeaweedFS S3 tenant

Use this sequence after the old consumer has stopped using its S3 backend and the
replacement is verified. Deleting a Bucket, S3Credentials and S3Identity together
can recreate an IAM user after its identity finalizer completes. This was observed
with operator `1.0.39` / chart `0.1.42` and filer `4.46`; see the
[Zot investigation](../../debug/2026_11_01_zot_iam_cleanup.md).

Keep each stage in GitOps and wait for its external-state gate before publishing
the next removal. A merged PR, Flux `Ready` or absence of CRs alone is insufficient.
Follow the [operator operations skill](../../skills/seaweed_operator/SKILL.md) for
ownership, grants, Secret handling and stateful Flux migration rules.

1. **Inventory and authorize.** Identify the physical bucket, every identity and
   credential claimant across namespaces, all bucket grants/policy bindings, target
   Secrets and Flux owners. Check actual bucket contents and current consumers.
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
4. **Revoke credentials.** Remove obsolete S3Credentials with `Delete`, keeping
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
event types and counts, and compare secrets only in memory. Ordinary filer IAM
RPC detail logs may also include access-key values; sanitize before publication.
