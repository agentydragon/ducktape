# Forgejo Git stream failure from SeaweedFS FUSE coherence loss (2026-08-31)

## Classification

This was a **recurrence of the SeaweedFS FUSE failure mechanism**, not a
recurrence of the exact August 24 trigger.

The affected Forgejo replica served the same intact repository as its sibling,
but its SeaweedFS FUSE client could no longer read parts of the repository
reliably. Git reads packfiles and trees through `mmap`; the resulting FUSE I/O
fault was delivered to `upload-pack` as `SIGBUS`. Forgejo then exposed the
failure as slow or invalid Git streams, including `expected 'packfile'`.

## What is confirmed

- Forgejo's `/data` is a SeaweedFS FUSE mount backed by the RWX
  `forgejo-git-rwx-ssd` PVC.
- The `main` ref was identical on both Forgejo replicas.
- On one replica, `refs/heads/main^{tree}`, `git cat-file`, README reads,
  `.forgejo` tree traversal, and `git fsck --full` faulted with `Bus error`.
- The sibling replica read the same tree and blobs successfully. This proves
  the repository data was intact and localizes the fault to the client/mount
  path.
- The mount manager and CSI pod remained `Running`; that did not imply that
  the per-volume FUSE client was healthy.
- The mount logs contained concrete coherence warnings and errors, including
  `ErrNotFound ... possible coherence bug`, `unsynchronized dir`, and failed
  Git commit-graph rename operations.
- Replacing the Forgejo consumers caused the stale client state to be rebuilt.
  The replacement pods could read the repository successfully afterward.

## How this differs from the previous incidents

The downstream failure is the same as the incidents documented in
`2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md` and
`2026_08_24_descheduler_filer_eviction_loop_sigbus.md`: an incoherent FUSE
client causes Git reads to fault, rather than the Git repository being
corrupt.

The initiating event is different or still unknown:

- No SeaweedFS Filer restart or descheduler eviction was observed at the time
  of this failure, so this is not established as another August 24 Filer
  eviction loop.
- The affected haku-state mount had been freshly staged earlier that day, so
  this is not simply a mount surviving unchanged from the July evacuation.
- SeaweedFS volume-server churn and DNS/backend failures occurred in the
  surrounding period. They are plausible contributors to stale location or
  metadata state, but the retained logs do not prove which event first
  poisoned this client.

The correct level of certainty is therefore: **known FUSE coherence/recovery
failure; unproven backend trigger**.

## Recovery and current limitation

The immediate recovery was an automated VPA replacement of both Forgejo pods.
The corresponding consumer teardown caused the CSI mount subscriptions to be
removed and recreated. No manual cluster recovery was performed.

This confirms that consumer/mount refresh is an effective recovery, but it is
not self-healing. CSI v1.4.14 can leave a FUSE client or its cached state
unusable while its pod remains healthy, and `OnDelete` mount management leaves
the repair dependent on an explicit mount or consumer roll. This is the gap
tracked by [#4616](https://github.com/agentydragon/ducktape/issues/4616) and
[#4786](https://github.com/agentydragon/ducktape/issues/4786).

## Operational conclusion

Treat a Forgejo Git stream timeout together with `SIGBUS`, `expected
'packfile'`, `unsynchronized dir`, or SeaweedFS lookup errors as a
SeaweedFS FUSE client incident until a direct comparison against another
replica disproves it. Check the repository through the affected and an
unaffected mount, then recover by refreshing the consumer and verify an
authenticated Git/tree read end to end.

Do not describe this class of incident as Forgejo corruption or as a merely
`Running` CSI pod being healthy. The exact backend trigger remains an
investigation question until mount, Filer, volume-server, and DNS timelines
are retained together.
