# Forgejo recovery follow-ups, 2026-10-23

## Backup catalog loss

The final audit found all 13 earlier completed Backup objects absent and the
physical `forgejo-db-ssd/base/` prefix empty. Metadata for the pre-cutover backup
`20261023T025219` and post-recovery backup `20261023T040545` returned 404.

At 04:53:09.935 UTC the primary applied retention `30d`; at 04:53:10.821–10.879
it logged `Deleting backup not in the catalog` for all 13 objects. In [plugin v0.15.0](https://github.com/cloudnative-pg/plugin-barman-cloud/blob/v0.15.0/internal/cnpgi/instance/retention.go#L142)
this is Kubernetes garbage collection after Barman retention and a catalog read,
not proof of who removed physical objects. Filers subsequently cleaned empty
directories. The bucket retained its Oct 12 UID, `Retain`, versioning off, and no
S3 lifecycle configuration. Available S3 logs contained no requests for this
bucket; the physical loss's cause remains unknown.

Replacement Backup `forgejo/forgejo-key-recovery-final`, ID `20261023T051355`,
completed at 05:14:46 UTC, LSN `8E/7C3FEF78`–`8E/7D088168`. Its physical Barman
catalog entry remained `DONE` after subsequent retention. It was not restore-tested.

## CI proxy path

From the same runner, a shallow public-action fetch through `10.244.8.236:8080`
hit a 20-second limit; through `10.244.1.10:8080` it completed in three seconds.
Haku CI [run 10029](https://git.allegedly.works/haku/haku-state/actions/runs/10029)
passed validation, tests, builds and all three image pushes on temporary OVH
runners with runner/daemon proxy variables selecting the working replica. Inner
job containers retained the normal proxy Service hostname. Default scheduling
and the slow proxy path remain unverified; the temporary runners were removed.
