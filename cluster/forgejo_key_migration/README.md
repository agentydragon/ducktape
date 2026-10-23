# Forgejo encryption-key cutover

The [2026-10-22 rehearsal](../debug/2026_10_22_forgejo_key_rehearsal.md) passed
forward/reverse migration and native Forgejo reads on both replicas. Production
activation remains pending.

One-off migration for Forgejo **15.0.8**, database schema **305**. `check` uses a
read-only, repeatable-read transaction. `apply` locks the relevant tables,
authenticates every old ciphertext, re-encrypts it, verifies the stored result,
and commits together. Wrong keys, changed counts, or unsupported key-dependent
records abort the transaction. No keys, ciphertexts or plaintext values are logged.

Only Actions `secret.data` and pull-mirror `mirror.encrypted_remote_address` are
migrated. TOTP, push mirrors, encrypted webhook authorization, tasks and LDAP must
remain absent. Extend the migration after inspecting Forgejo if that changes.

The format follows tagged upstream [keying](https://code.forgejo.org/forgejo/forgejo/src/tag/v15.0.8/modules/keying/keying.go),
[Actions](https://code.forgejo.org/forgejo/forgejo/src/tag/v15.0.8/models/secret/secret.go)
and [mirror](https://code.forgejo.org/forgejo/forgejo/src/tag/v15.0.8/models/repo/mirror.go)
code: HKDF-SHA256 without salt, domain-separated 32-byte keys, XChaCha20-Poly1305,
and AAD consisting of a field name, colon and big-endian row ID. Mirror AAD uses
`remote_address_auth`, **not** its database column name. PyCryptodome supplies the
cryptographic primitives.

## Staging and activation

`cluster/k8s/forgejo/forgejo-secret-key.sops.yaml` contains:

- `current`: the existing public placeholder, retained for a compatible staging rollout.
- `replacement`: a newly generated random key; inactive until the maintenance cutover.

[PR #11588](https://github.com/agentydragon/ducktape/pull/11588) stages the
`current` mount and retains the same inline key through Helm `valuesFrom`.
Before merging activation, wait for both replicas to finish that rollout and
verify each mounts `current` and still uses it, without an environment override.
Compare inside each pod without printing key material or stable hashes.

This follow-up selects `replacement`, switches to `SECRET_KEY_URI`, converts
the retained inline setting in `initPreScript`, and suppresses the chart's
inline-key generation on a fresh install. The HelmRelease is explicitly
**suspended**: applying these values does not upgrade the running release.
Merge during the coordinated window below, after the database migration.
Keep `current` for rollback.

## Rehearsal prerequisites

Before production `apply`:

1. Restore a recent database backup into an isolated namespace, alongside the
   matching configuration/key. Keep private contents inside the cluster; do not
   upload the backup to CI. Block mail, hooks, mirror pushes and other external
   side effects in the restored Forgejo deployment.
2. Confirm the exact Forgejo image/version and inventory. The audited inventory
   on 2026-10-22 was four Actions secrets and one encrypted mirror address.
3. Exercise `check`, `apply`, and reverse `apply` against the restored database.
   Start the same Forgejo version under the replacement key and exercise its
   Actions-secret and mirror reads. A Python encrypt/decrypt round trip alone
   does not establish interoperability with Forgejo.
4. Record the backup/restore identifiers, image digest and observations in the
   owning `cluster/debug/` investigation. Rehearsal is a gate for live cutover,
   not an action performed by merging this code.

## Running the Job

The four Kustomize directories are outside Flux's manifest roots. Each renders
one concrete **suspended** Job; creating it does not run it. All use the exact
rehearsed image digest, production Secret names, TLS certificate verification,
and the reviewed inventory of four Actions secrets and one mirror address.

| Directory  | Operation                      |
| ---------- | ------------------------------ |
| `check`    | Read-only, current key         |
| `apply`    | Migrate current to replacement |
| `verify`   | Read-only, replacement key     |
| `rollback` | Migrate replacement to current |

The GHCR image has the exact same manifest digest as the rehearsed Forgejo
registry image. It remains available while Forgejo is stopped, needs no private
pull Secret, and all Jobs use `IfNotPresent`. Complete `check` on the selected
worker (`ovh-ns103656`) before downtime to verify image startup, database access
and staged keys. A successful pull in another namespace does not prove an
uncredentialed pod can use a private cached image.

From the repo's Nix devshell, create and explicitly start the read-only check:

```bash
kubectl create -k cluster/forgejo_key_migration/check
kubectl -n forgejo patch job forgejo-key-check --type=merge -p '{"spec":{"suspend":false}}'
kubectl -n forgejo wait --for=condition=complete job/forgejo-key-check --timeout=300s
kubectl -n forgejo logs job/forgejo-key-check
```

For subsequent Jobs, replace `check` with the directory and Job suffix from the
table, at the corresponding step below. `backoffLimit: 0` prevents automatic
retries. A failed wait is not a rollback signal: inspect the Job and sanitized
logs before taking another action. Do not create or start `rollback` routinely.

`check` may run while Forgejo serves traffic, but its snapshot does not guarantee
the inventory stays unchanged. Review changed counts; do not merely increase
the expected numbers to make the guard pass.

## Production window

1. Pause new CI work and credential writers. Suspend the `haku-state` Terraform
   resource in `flux-system` and the `agents-infra/forgejo-token-rotation` CronJob; wait for their
   existing jobs to finish. Preserve their original suspension states.
2. Preserve suspension states and temporarily suspend the parent
   `flux-system/flux-system` Kustomization, followed by the relevant child
   Kustomizations (`forgejo`, `forgejo-gitops`, `forgejo-token-rotation`, `haku-ci`
   in `ducktape-flux`). Use `--field-manager=forgejo-cutover` for maintenance
   patches; Flux clears ordinary `kubectl` edits. Confirm writer suspensions
   remain set. Suspend HelmRelease
   `forgejo/forgejo`. Scale Deployment `forgejo/forgejo` to zero and wait until
   **all** its pods, including init containers and terminating pods, are gone.
   Do not delete its PVCs, database or other persistent resources.
3. Take the final backup:

   ```bash
   kubectl create -f cluster/forgejo_key_migration/backup.yaml
   kubectl -n forgejo wait --for=jsonpath='{.status.phase}'=completed backup/forgejo-key-cutover --timeout=600s
   ```

   Confirm it belongs to `forgejo-db-ssd`, record its backup ID and preserve the
   current `app.ini` securely. The rehearsed restore must cover this schema and
   configuration. A completed backup alone is not restore proof.

4. Create and explicitly start `apply` using the command pattern above. Wait
   for `forgejo-key-apply` to complete and review its sanitized logs. Keep
   Forgejo stopped, then create/start `verify` and require its read-only check
   under the replacement key to succeed before activating either replica.
5. Merge this activation PR. Resume the Forgejo Kustomization so it
   applies the new Helm values **and the explicitly suspended HelmRelease**.
   Verify the source revision, key selection `replacement`, and suspended state;
   suspend that Kustomization again, then resume the HelmRelease with `upgrade.remediation.retries: 0` and
   `remediateLastFailure: false` for this upgrade. An automatic Helm rollback
   would restore the old key against the migrated database. Its chart restores
   the configured replicas.
   A mounted Secret changing does not reload the key in an existing process.
6. Verify both replicas use `SECRET_KEY_URI`, have no populated inline key or
   environment override, and match the replacement key without printing it.
   Exercise Forgejo's Actions-secret reads and mirror synchronization. Restarting Forgejo permits
   new writes; the simple backup rollback boundary ends here.
7. Rotate Haku's underlying password through `tf/gitops/haku-state`, verify the
   plan orders the user change and every distributed copy correctly, then resume
   its controller. Verify Git, CI clone/build/push, registry pulls and Flux access.
   Replace/revoke Haku PATs through their owners; password changes do not revoke
   them. Resume the token rotator only after its source credentials converge.
8. Remove the temporary declarative HelmRelease suspension in a cleanup commit,
   restore normal Helm remediation, resume its Kustomization and the parent,
   restore remaining paused work and take a fresh
   backup. Clean up the one-off Jobs after retaining their sanitized evidence.

The activation commit's explicit suspension prevents Flux from dropping a live
maintenance override while applying new values. Check for a pending Helm
operation before proceeding; suspension does not cancel one already running.
Merging this PR suspends Helm reconciliation; it does not run a Job or rotate credentials.

## Failure and rollback

- Before commit, any SQL/decryption/verification failure rolls back the entire
  transaction. Keep Forgejo stopped and investigate. If the connection fails at
  commit, success is **unknown**; use the read-only `check` and `verify` Jobs to establish which key is in use before taking further action.
- After commit, before restarting Forgejo, either restore the final backup with
  the corresponding old key/configuration, or create and explicitly start the
  `rollback` Job using the command pattern above. It uses the same reviewed counts
  and reverses the key direction. After successful reversal select `current` in
  the generator again before starting either replica.
- After restart or credential rotation, stop writers again and inventory fresh
  data. Prefer forward repair or a reviewed reverse migration. Do not restore a
  stale whole database over new writes or reintroduce revoked credentials.
- Rerunning forward `apply` after success fails old-key authentication without
  changing rows. Do not switch the mounted key based only on a Job's existence.

Re-encryption preserves the existing secret values. It does not revoke credentials
recoverable from old backups, establish that no one accessed them, or replace the
separate Haku credential/token recovery work.
