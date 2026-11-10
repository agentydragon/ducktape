# Zot IAM identity recreated during retirement

The first Zot cleanup deleted the IAM identity successfully, but a concurrent
write recreated it 27.321040 ms later with one surviving key. Removing all three
Seaweed resources in one Flux prune therefore left external IAM state behind.
The permanent cleanup procedure is in the
[S3 retirement runbook](../docs/runbooks/seaweedfs_s3_retirement.md).

## Deployed implementation

Observed on 2026-11-02 UTC:

- Operator Helm chart `0.1.42`, application image `chrislusf/seaweedfs-operator:1.0.39`,
  image digest `sha256:301f49b8bdde8c5f510db080e0543c61e1fad5087bea54afb7e3cc21166068a8`.
- Chart source tag resolves to commit
  `e424a372b7f7457175a4d1a2948807e534ec3725`.
- Both filers run `chrislusf/seaweedfs:4.46`, image digest
  `sha256:08d516132314207d10c8e37cbffc1f32b147d870169688734cc61c6231625b62`.

## Direct evidence

All times below are UTC on 2026-11-01. The persisted filer metadata journals retain
old and new inline identity contents. The projection below emits only names,
key counts and event times; credentials were compared in memory and never printed
or saved.

| Time                      | Evidence                                                                                                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `23:34:51`                | [#13589](https://github.com/agentydragon/ducktape/pull/13589) merged, staging `Delete` for the Bucket, S3Credentials and S3Identity.  |
| `23:36:02`                | Live resource check showed all three policies `Delete`, all phases `Ready`.                                                           |
| `23:39:19`                | [#13590](https://github.com/agentydragon/ducktape/pull/13590) merged, removing all three CRs together.                                |
| `23:40:16.221290204`      | Journal: delete `/etc/iam/identities/registry-cache.json`; old identity has **two** keys.                                             |
| `23:40:16.248611244`      | Journal: create the same path, `old_entry` absent; new identity has **one** key.                                                      |
| `23:40:16.589660746`      | Operator logs an S3Credentials reconcile error: `registry-cache` not found. This error alone does not identify the failing code path. |
| `23:40:20.512320166`      | Journal: delete `/buckets/registry-cache`.                                                                                            |
| `23:46:46` and `23:46:52` | Independent `s3.user.list` reads through each filer still report the identity with one key.                                           |
| `23:48:21`                | [#13594](https://github.com/agentydragon/ducktape/pull/13594) merged, re-adopting only the identity with `Delete`.                    |
| `23:50:15`                | Re-adopted identity reaches `Ready`, generation `1`, with `Delete`.                                                                   |
| `23:52:33`                | [#13595](https://github.com/agentydragon/ducktape/pull/13595) merged, deleting that identity alone.                                   |
| `23:53:57.770947700`      | Journal: delete `registry-cache.json`, old identity has one key.                                                                      |
| `23:54:04` and `23:54:08` | Independent reads through both filers report no matching IAM user; both remain absent at `23:55:29`.                                  |

Journal locations under `/topics/.system/log/2026-11-01/`:

- Initial delete: `23-39.41c4b077`.
- Recreation and bucket delete: `23-39.300386d6`.
- Final identity delete: `23-53.41c4b077`.

Both filer streams were scanned for segments `23-39` through `23-54`; these were
the only matching identity/bucket events. Segment names describe their starting
window and do not bound every event timestamp inside them.

The recreated identity matches the deleted identity in every noncredential field.
Its remaining credential is byte-identical to one of the previous two; exactly
one credential was removed. This is a persisted recreation, not merely a stale
S3 gateway cache or a delayed Kubernetes deletion.

## Mechanism and attribution boundary

The deployed operator's
[`IAMClient`](https://github.com/seaweedfs/seaweedfs-operator/blob/e424a372b7f7457175a4d1a2948807e534ec3725/internal/controller/swadmin/iam_client.go#L233)
uses a process-global, per-filer-address/per-user lock for `DeleteAccessKey`,
credential rewrites, policy updates and bucket access updates. `DeleteUser`
omits that lock. The identity and credential controllers can therefore mutate
the same user concurrently, even with one operator replica.

The filer's
[`FilerEtcStore.DeleteAccessKey`](https://github.com/seaweedfs/seaweedfs/blob/4.46/weed/credential/filer_etc/filer_etc_identity.go#L451)
reads the identity, removes one credential, then calls `saveIdentity`.
`saveIdentity` unconditionally writes the whole identity file. It does not verify
that the user still exists or compare a version from the earlier read.
`DeleteUser` separately removes that file. The resulting interleaving is:

```text
credential controller: read user with keys A and B
identity controller:   delete user; finalize S3Identity
credential controller: remove A from old snapshot; save user with B
credential controller: finalize S3Credentials
```

Each finalizer treats its successful IAM operation as completion. Neither verifies
that a concurrent operation has recreated the user after its deletion. The
credential CR manages one key; the second key survives its one-key revocation.
The second key's historical origin was not established by this investigation.

**Proven:** delete followed by recreation, exact two-to-one key transformation,
unprotected concurrent mutation paths, and successful isolated deletion.
**High-confidence inference:** the recreation was the credential controller's
`DeleteAccessKey` racing the identity controller's `DeleteUser`. Metadata journals
do not include the IAM RPC name or caller, and successful IAM deletions are not
logged at the deployed verbosity, so the exact caller is not independently proven.

Alternatives checked:

- `Retain` was not the applied cleanup policy. The identity file really was deleted.
- The tenant grant was retained during the first prune; its later removal in
  [#13592](https://github.com/agentydragon/ducktape/pull/13592) cannot explain the
  earlier recreation. Deletion paths also skip reference-grant checks.
- A surviving name claimant would suppress identity deletion; the journal proves
  deletion occurred. No claimant remained in the later CR inventory.
- The Bucket deletion path calls `s3.bucket.delete`, which removes the collection
  and bucket directory without writing IAM. Normal bucket access and policy
  updates preserve credentials rather than remove exactly one.
- Filer startup has no static `-s3.config` argument. The evidence is an actual
  persisted identity file recreation on the shared filer backend.
- The cleanup worker transcript contains no imperative IAM delete/import command
  during this window. A separate unobserved IAM client cannot be excluded by a
  metadata journal without caller information.

## Preventive changes

Use the staged retirement runbook immediately. An upstream operator fix should
make `DeleteUser` acquire the existing user lock and add a deterministic test
using separate client instances: pause credential deletion after its read, start
user deletion, then release it and verify the user remains absent. Existing tests
cover key rewrite versus key deletion, not user deletion versus key deletion.

That lock fixes this same-process race. Cross-process clients, multiple filer
addresses and stale read/modify/write clients still require server-side atomic
mutations or version-checked writes. No production IAM mutation or canary was
needed for this investigation; no upstream code fix was deployed.
