# Forgejo encryption-key cutover, 2026-10-23

Activated the SOPS `replacement` key from PR #11588 using the maintenance
configuration in PR #11597. The production migration implementation was unchanged.
Re-encryption preserved values; the subsequent Haku credential recovery below
invalidated credentials recoverable from older backups.

## Restore point and migration

- Scaled Forgejo to zero at 02:49:51 UTC and waited until every application pod,
  including terminating pods, was absent. Preserved `app.ini` in the temporary
  `forgejo/forgejo-key-cutover-config-backup` Secret; no contents were printed.
- Final CNPG backup: `forgejo-key-cutover`, database `forgejo-db-ssd`, backup ID
  `20261023T025219`; completed 02:53:17 UTC. Begin/end LSN:
  `8E/610128A0` / `8E/62000110`. The previous isolated restore/reverse rehearsal
  is recorded separately; backup completion is not an additional restore drill.
- `forgejo-key-apply` committed all four Actions secrets and the one encrypted
  mirror address. `forgejo-key-verify` authenticated all five with `replacement`.
- An ephemeral salted HMAC baseline inside the in-cluster probe confirmed all
  five plaintext values were preserved. No values or comparison digests were
  printed or uploaded to CI.

Migration image (same OCI manifest digest as the rehearsal image):

```text
ghcr.io/agentydragon/forgejo-key-migration@sha256:89516e63d6fbf4161a5915e12b999379a55b213ff8325eaec36682dc82b3032f
```

## Production checks

At 03:16:11 UTC both final replicas were ready and independently passed:

- Secret projection selects `replacement`; `SECRET_KEY_URI` points at the
  mounted file; no populated inline key or environment override.
- Read-only comparison of all five records against the original baseline.
- Native Forgejo web login and mirror settings decryption, addressed directly
  to each Pod IP.

Replicas: `forgejo-797576cb89-qd2tk` on `ovh-ns103711` and
`forgejo-797576cb89-x8wd9` on `ovh-ns102453`. Both used the rehearsed Forgejo image:

```text
code.forgejo.org/forgejo/forgejo@sha256:8f97b55ca162ef3b538c6c78a2a077df9f2143c41d80b2bc6b6920f8d430df34
```

The public HTTPS version endpoint also returned `15.0.8+gitea-1.22.0`.

## Corrections found during execution

1. Haku's image-pull credential cannot access the `ducktape-ci` migration
   package. A preload through a managed credential in another namespace did not
   make the uncredentialed private-image pod start. PR #11617 switches to the
   independently available GHCR copy, whose digest and embedded source match.
2. The first read-only database check failed at connection establishment; a
   connectivity probe and the subsequent read-only Job succeeded. No `apply`
   retry was performed.
3. Flux removed ordinary `kubectl` maintenance patches. The pre-apply guard
   stopped without creating the migration Job. Suspending the parent and using
   `--field-manager=forgejo-cutover` made the pauses persist; all guards were
   confirmed before applying.
4. Chart 17.1.6 concatenated the pre-script's final `fi` with its `# END`
   comment. The standalone pre-script/config rehearsal missed this embedding
   boundary. Added a trailing newline, checked the complete rendered script's
   Bash syntax, and observed successful production startup. Both Secret script
   and Helm values were corrected while normal reconciliation was held.
5. Disabled upgrade retries/remediation during activation so Helm could not
   automatically deploy the old-key release against the migrated database.
   Restore normal remediation only after the replacement-key upgrade succeeds.

## Haku credential recovery

PR #11625 made the password replacement and ownership of the two formerly
unmanaged UI Actions secrets declarative. Reviewed pending plan
`plan-latest-f4b41eb8ab`: the account updated in place; both repositories,
branch protection, collaborator grants and webhook stayed unchanged. Only the
password and opaque Actions-secret resources were replaced. The controller
applied it at 03:51:17 UTC and subsequently reported no drift.

Verified without emitting values:

- The new password authenticated; the old password returned HTTP 401.
- All four Actions secrets decrypted under `replacement` and matched the
  managed username/password sources.
- Forced ESO refresh rather than waiting its one-hour interval. All six
  downstream copies, including Reflector destinations and the Agentplane
  staging credential, matched their sources.
- Both Flux Git sources fetched successfully and all three Haku registry
  scanners completed authenticated scans after rotation. Reloader replaced
  Haku UI, Anki and Jupyter pods; their containers became ready.
- Both Forgejo replicas again passed native login and mirror-settings reads;
  the original mirror plaintext still matched the pre-migration baseline.
  Native pull-mirror synchronization completed at 04:07:15 UTC.
- The new managed password authorized a registry blob-upload handshake (202);
  the empty temporary upload was cancelled (204).

PR #11623 added explicit recovery operations to the existing token owner.
Published image, built from `47fb0775cea3801cc19b79cf353768cfc7a9c541`
(successful CI run `39896381087`):

```text
git.allegedly.works/ducktape-ci/forgejo-token-rotation@sha256:59a02841715b22e57b6373faa770b6423c0dcfe5de4ad2448f5dec5810ad7422
```

The forced Haku-only rotation retained every old token and completed at
03:54:21 UTC. Its SOPS outputs were pushed through the normal GitOps path.
Fresh token ID `134` and its `haku-ci` Reflector copy matched; authentication
and private-repository admin access passed before any revocation attempt.
The operator approved revoking all six old IDs: `131`, `95`, `56`, `54`, `46`,
`39`, including the four legacy registry tokens with unverified external consumers.

The first revoke-only Job failed in inventory lookup because internal DNS
returned `Name or service not known`; all six old IDs remained present. The
retry used the same managed image and arguments, with Forgejo's verified
Service IP in the temporary Pod's `hostAliases`.

That retry also stopped before deletion, with a connection timeout on `wyrm2`.
A second retry on `ovh-ns103656`, using the same Service-address mapping,
completed at 04:04:35 UTC. All six old IDs were absent afterward; token `134`
still authenticated and retained private-repository admin access. The temporary
network workaround does not establish the cause of the intermittent DNS and
connectivity failures.

## Restored ownership and backup

PR #11622's corrected startup script and removal of the temporary HelmRelease
suspension reconciled successfully. Normal Helm remediation is restored.
The Forgejo, GitOps, token-rotation and Haku CI Kustomizations are resumed;
`haku-state` is back to automatic controller approval, the token CronJob is
unsuspended, and KEDA's temporary pause annotation is removed.

Fresh CNPG backup `forgejo-key-post-recovery`, ID `20261023T040545`, completed
04:06:37 UTC. Begin/end LSN: `8E/6F013AC8` / `8E/7000F8B8`. It contains the
replacement-key ciphertexts and recovered credentials; the pre-cutover backup
was available at that point; the final audit below found it missing.

## Native CI verification

Existing Haku workflow dispatched as [run `10029`](https://git.allegedly.works/haku/haku-state/actions/runs/10029) (internal ID `40416`) at
`edc8200388ae0d1b627d561567ab720203a2fc8b`, event `workflow_dispatch`. Both
runners fetched their tasks and started Docker job containers, but setup
stalled cloning the public `https://data.forgejo.org/actions/checkout` at `v4`.

Both original runner connections used proxy `10.244.8.236`. From the same
runner, a bounded shallow fetch through that proxy hit the 20-second limit;
through OVH proxy `10.244.1.10` it completed in three seconds. The original
verification attempt was cancelled through Forgejo's supported UI route at
04:45:05 UTC. Its temporary runner Jobs were removed. Attempt 2 ran the same
workflow and commit on two one-off OVH runners, with runner/daemon proxy
variables selecting the working replica. Inner Docker job containers retain
the configured proxy Service hostname. The normal KEDA pause was removed
after both jobs were assigned. The proxy-path defect remains a separate
follow-up; this does not prove default scheduling/egress is healthy.

The second attempt succeeded. Validation completed at 04:53:57 UTC; the image
job completed at 05:07:43 UTC. Test, build, registry authentication and all
three image pushes (UI, Anki, Jupyter) passed. Profile upload also passed;
publishing failure logs was correctly skipped. Forgejo recorded both jobs
and the overall workflow as successful. This was a manual dispatch of the
existing workflow, not a PR status check.

## Final audit: backup catalog loss

Both temporary CI Jobs and the API observer Pod were UID-checked and deleted.
The earlier migration/rotator Jobs, probes and configuration-backup Secret were
also removed. Final controller-state and application-rollout checks passed.

The final backup check found a separate failure: all 13 previous completed
Backup resources were gone. Barman's physical base-backup catalog was empty;
HEAD requests for `backup.info` of `20261023T025219` and `20261023T040545`
returned 404. The `base/` prefix was empty while the server/WAL prefix remained.
Do not treat either earlier completion record above as a retained restore point.

The primary's plugin applied retention policy `30d` at 04:53:09.935 UTC and
logged `Deleting backup not in the catalog` for all 13 resources at
04:53:10.821–10.879 UTC. In [plugin v0.15.0](https://github.com/cloudnative-pg/plugin-barman-cloud/blob/v0.15.0/internal/cnpgi/instance/retention.go#L142), this message is Kubernetes garbage
collection after Barman's retention command and a catalog read; it does not
identify who removed physical objects. Filers subsequently logged empty-folder
cleanup. The bucket has its original Oct 12 identity, `Retain`, versioning off,
and no S3 lifecycle configuration. Available S3 logs contain no requests for
this bucket, so the cause of physical catalog loss is not established.

Requested `forgejo-key-recovery-final` rather than relying on the missing
restore points. Backup ID `20261023T051355` completed at 05:14:46 UTC; begin/end
LSN `8E/7C3FEF78` / `8E/7D088168`. A direct Barman catalog check confirmed `DONE`
afterward and again after a later retention cycle. This establishes a fresh
physical backup, not a restore drill or a fix for the unexplained earlier loss.
The backup-retention/storage investigation and slow default CI proxy route
remain follow-ups; neither invalidates the live key and credential checks.
