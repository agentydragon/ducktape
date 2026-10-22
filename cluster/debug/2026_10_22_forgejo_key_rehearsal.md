# Forgejo encryption-key rehearsal, 2026-10-22

Candidate: [PR #11588](https://github.com/agentydragon/ducktape/pull/11588),
code head `479e39835ed31f6d55fd4c932a767bc8da152ba9`. No application-code changes
were needed during the rehearsal.

## Inputs and isolation

- Restored completed CNPG plugin backup `20261022T011600` of
  `forgejo/forgejo-db-ssd` to its first consistent state. PostgreSQL image:
  `ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie`; restored schema: `305`.
- Namespace: `forgejo-key-rehearsal-20261022-479e398`, UID
  `72e1cf6c-2ebe-4ceb-b284-1f7425f01188`. The disposable database used a 10 GiB
  `lvm-proxmox-ssd` volume on `wyrm2`; initial OVH placements were unschedulable.
- Recovery referenced the existing backup store, with no archiving plugin or
  retention policy configured on the clone. No backup bucket or IAM owner changed.
- Cilium policies restricted application egress to rehearsal peers and cluster
  DNS. Connection probes confirmed public HTTPS, production Forgejo and
  production S3 were unreachable. Mail, webhooks and cron were disabled; the
  restored OAuth login source was disabled before application startup.
- Private database contents stayed in the cluster. Git repositories were tiny
  synthetic fixtures. Actions reads used synthetic run/job/runner records in the
  clone and fetched tasks without executing workflows.

Images exercised:

```text
Forgejo:
code.forgejo.org/forgejo/forgejo@sha256:8f97b55ca162ef3b538c6c78a2a077df9f2143c41d80b2bc6b6920f8d430df34
Migration:
git.allegedly.works/ducktape-ci/forgejo-key-migration@sha256:89516e63d6fbf4161a5915e12b999379a55b213ff8325eaec36682dc82b3032f
```

The migration image's source matched the candidate byte-for-byte. The local
GitHub token lacked GHCR push scope, so the image was published under
`rehearsal-479e398` in the existing Forgejo registry using its existing credential.
That artifact is retained. Production cutover still requires the runbook's
independent image availability/preloading check before stopping the registry.

## Observed results

| Check                               | Result                                                                                                                                                                                                                 |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Read-only Job against restored data | Authenticated four Actions secrets and one mirror address.                                                                                                                                                             |
| Original-key Forgejo reads          | Runner RPC returned all four original secrets; repository settings decrypted the mirror address.                                                                                                                       |
| Failure on final encrypted row      | Corrupting the clone's mirror ciphertext caused authentication failure after Actions updates; all four Actions ciphertexts remained byte-for-byte unchanged after rollback. The mirror was then restored.              |
| Forward Job                         | Re-encrypted all five records and committed successfully.                                                                                                                                                              |
| Repeated forward Job                | Rejected the old key with authentication failure. A subsequent read-only check authenticated all five records with the replacement key.                                                                                |
| Replacement-key Forgejo reads       | Both restarted replicas independently decrypted all four Actions secrets and the mirror address.                                                                                                                       |
| Reverse Job                         | Re-encrypted all five records back under the original key. Both restarted replicas independently read all five original values again.                                                                                  |
| Activation configuration            | Applying the checked-in patch and running the generator rendered a suspended HelmRelease, the replacement-key mount, the file URI and an empty inline override. The worktree was restored to staging afterward.        |
| Chart startup behavior              | The deployed chart script matched the inspected source. Running it in the exact Forgejo image with activation settings passed existing-inline-key conversion and fresh-config suppression of the generated inline key. |

Value comparisons used an ephemeral salted HMAC baseline inside the probe pod;
no secret values or comparison digests were printed. The native Go reads used
the real runner `FetchSingleTask` RPC and mirror settings template, rather than
only Python reading its own output.

## Cleanup and production boundary

Stopped rehearsal applications and the probe, then deleted the UID-verified
namespace. Confirmed both the namespace and its disposable PV
`pvc-f53109cf-05a6-4cae-b8f8-8264bd9924bc` were absent. PV UID:
`1ce0f7c9-06d1-4f10-8973-724f42f79f73`; reclaim policy was `Delete`.

At completion, both production replicas were ready and still matched the original
effective key, checked without printing it. No production encryption migration, key switch,
password rotation or PAT revocation was performed. The production registry write
was the reusable rehearsal image. This was a database/crypto/startup rehearsal,
not a full restoration of production Git repositories or a production Helm upgrade.
