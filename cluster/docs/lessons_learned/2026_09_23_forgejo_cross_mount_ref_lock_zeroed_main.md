# Forgejo ref zeroed by a git lock collision across two SeaweedFS mounts (2026-09-23)

## Symptom

`haku/haku-state` `refs/heads/main` became a 0-byte file at 23:55:35Z, right after the
web-UI merge of PR #128. Every read of `main` failed (`warning: ignoring broken ref
refs/heads/main`), and so did every push to the repository, whatever the branch
(`remote: fatal: bad object refs/heads/main`, `missing necessary objects`), until the ref
was rewritten from the reflog. `git update-ref` refuses to replace a broken ref, so the
recovery wrote the SHA to `refs/heads/main.lock` and renamed it over `refs/heads/main`.

## Root cause

The two Forgejo replicas run on different nodes (required pod anti-affinity), so each
has its own `weed mount` process on the shared RWX volume. The web-UI merge and an HTTP
push of `main` (`d09cb1c2 → e61a825f`, from a now-gone pod at `10.244.1.104`) ran as
concurrent `receive-pack`s on different replicas. Mounts are identified in the filer
metadata log by signature:

| Mount signature | Pod (node)                         | `weed` (CSI mount image) |
| --------------- | ---------------------------------- | ------------------------ |
| `2010294215`    | `forgejo-…-vnktl` (`ovh-ns103656`) | `79b8720` (v1.4.30)      |
| `-788145207`    | `forgejo-…-f9vm2` (`ovh-ns103711`) | `8a532cc` (v1.4.29)      |

`weed filer.meta.tail -pathPrefix=…/haku-state.git/refs/heads/main` (and `…/HEAD`):

| Filer time   | Mount | Event                                                                      |
| ------------ | ----- | -------------------------------------------------------------------------- |
| 23:55:34.780 | vnktl | create `main.lock`: 41 B, 1 chunk (`e875d7ba`, the merge)                  |
| 23:55:34.785 | vnktl | create `HEAD.lock`                                                         |
| 23:55:34.909 | vnktl | rename `main.lock` → `main`: 41 B. The merge committed correctly.          |
| 23:55:35.032 | vnktl | delete `HEAD.lock`                                                         |
| 23:55:35.123 | f9vm2 | create `HEAD.lock`                                                         |
| 23:55:35.242 | f9vm2 | **update `main`: 41 B / 1 chunk → 0 B / 0 chunks, mtime `…:35.000000000`** |
| 23:55:35.245 | f9vm2 | delete `HEAD.lock`                                                         |

f9vm2 never created or removed a `main.lock` on the filer. Its lock file's only filer
write landed under the name `main`. The push it was serving got no post-receive and left
no `logs/refs/heads/main` entry; only its `HEAD` log-only update reached `logs/HEAD`.

Three `weed mount` behaviours combine (code at the deployed commits, unchanged on
upstream `master` as of 2026-09-24):

1. **git's ref lock is not exclusive across mounts.** git takes `<ref>.lock` with
   `O_CREAT|O_EXCL`. On Linux, `weed mount` defers a new file's filer entry to flush
   (`EagerFilerCreate` is set only on Windows) and checks `O_EXCL` only against its own
   metadata view (`Create`, `weed/mount/weedfs_file_mkrm.go`). The flush is a
   `CreateEntryRequest` without `OExcl` (`flushMetadataToFiler`,
   `weedfs_file_sync.go`), so a second mount's flush overwrites silently. This has been
   the case since weed 4.18 (#8769).
2. **A foreign rename relabels the local lock handle.** A mount applies filer events to
   its metadata store inline, and to open file handles later, on an async worker
   (`invalidateWorker`, `meta_cache/meta_cache.go`). f9vm2 took its `main.lock` after its
   store had applied vnktl's `main.lock → main` rename; the placeholder's
   whole-second `…:35` mtime postdates the rename at `…:34.909`. When the worker then
   processed that rename, `invalidateOpenFileHandle` (`weedfs.go`) resolved the old
   path to f9vm2's own open `main.lock` handle and `MovePath`ed it to
   `refs/heads/main`. The rename's content event for `main` carries the same log
   position and is dropped by the handle's version fence, so the handle kept its empty
   placeholder. The handle relabel shipped in weed 4.41 (#10403).
3. **The loser's rollback published its empty lock.** git 2.52's `receive-pack` runs a
   non-atomic push as one `REF_TRANSACTION_ALLOW_FAILURE` transaction. f9vm2's update of
   `main` was rejected because `main` no longer held the old value it expected. The
   rejected update keeps its lock open while the transaction locks `HEAD` and writes
   reflogs, and closes it at cleanup. That close flushed the never-written placeholder
   under its new name, which is the `0 B` update of `main`. The following
   `unlink(main.lock)` found nothing.

A 0-byte ref with no chunks and a zero-nanosecond mtime is therefore a `weed mount`
create placeholder that was never written, not a torn write.

## Reproduction

A local `weed server` with two `weed mount`s built at `8a532cc` and `79b8720`, with the
CSI driver's flags:

- The same `O_EXCL` open of `refs/heads/main.lock` succeeds on both mounts. On one
  mount, the second open gets `EEXIST`.
- git 2.51 `receive-pack` on the second mount, pushing `main` against the pre-merge
  advertisement while the first mount commits, reproduced the production sequence.
  First mount: create `main.lock` 41 B, rename it onto `main`. Second mount: update
  `main` to 0 B / 0 chunks with a whole-second mtime, with no `main.lock` events. The
  downstream errors were the same too: `ignoring broken ref`, and an unrelated push
  rejected with `missing necessary objects`. To hit the window on demand, the second
  mount's handle-invalidation worker was delayed by 2 s (a lab-only sleep) and its push
  was held in the `update` hook until just after the rename.
- Without the relabel, the same collision still empties the ref. The loser's empty lock
  flush overwrites the winner's `main.lock` before the winner renames it, and the
  rename publishes the empty entry.

## Exposure

This can hit any ref of any repository that both replicas update within the window
between a mount applying an event to its store and applying it to open handles. Examples
are a web merge racing a bot push, two pushes racing each other, or a push racing
Forgejo's own ref writes. Every other git lock file on the volume (`packed-refs.lock`,
`config.lock`, `shallow.lock`, …) has the same problem. `cluster/cdk8s/forgejo/app.py`
cites a 2026-06-28 check of atomic exclusive-create for `*.lock` across nodes. Per item 1
above, that property did not hold across mounts at the time.

## Operational conclusion

An empty or `bad object` ref on Forgejo's volume, in the second after two pushes, is this
failure, not a FUSE read-staleness incident like
[2026_08_31](2026_08_31_forgejo_seaweedfs_fuse_coherence_recurrence.md). The bytes on the
filer are wrong, and rolling the consumer pod does not repair them. Confirm with
`weed filer.meta.tail`: look for an update of the ref from a mount other than the one that
renamed its lock onto it. Recover from the reflog as above.

Two Forgejo replicas on separate `weed mount`s cannot serialize git ref updates. Either
one replica, or both replicas on one node (one `weed mount` per volume per node, where
`O_EXCL` holds), removes the race. An upstream fix needs filer-side exclusivity for
`O_EXCL` creates, and remote renames must not move a handle whose entry the filer has
never seen. `weed mount -dlm` would serialize same-path creates across mounts, but the
CSI driver does not pass it.
