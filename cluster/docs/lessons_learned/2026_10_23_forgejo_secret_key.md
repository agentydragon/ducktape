# Forgejo database encryption key

Forgejo ran with a public placeholder `SECRET_KEY`. On 2026-10-23 we restored
a backup into isolation and rehearsed forward/reverse re-encryption. In production
we stopped Forgejo, backed up the database, re-encrypted four Actions secrets and
one mirror credential, and started both replicas with the SOPS-managed key through
`SECRET_KEY_URI`. Native reads passed on each replica. We then replaced Haku's
recoverable password and revoked its old API tokens. Implementation: [#11588](https://github.com/agentydragon/ducktape/pull/11588),
[#11597](https://github.com/agentydragon/ducktape/pull/11597).

Provision persistent encryption keys in SOPS **before the first application
start**. A deployable placeholder becomes real production state; fixing it later
requires preserving decryption and replacing credentials exposed by old backups.
The chart preserves an existing inline key, so changing Helm values alone does
not rotate it.

The active key is in `cluster/k8s/forgejo/forgejo-secret-key.sops.yaml`; preserve it
with database backups. [Unresolved findings from recovery](../../debug/2026_10_23_forgejo_recovery_followups.md).
