# haku-state: `main` ref lost its content on Forgejo during a PR merge (SeaweedFS FUSE torn write)

**Date**: 2026-09-24
**Status**: Resolved — `main` ref restored on both replicas, pushes confirmed working again.
Root cause confirmed at the byte level. Underlying SeaweedFS torn-write trigger not
investigated further (repair took priority; see "Open follow-up" below).

## Symptom

Immediately after Rai clicked the Forgejo web UI's "Merge" button on `haku/haku-state` PR
#128 (`haku/booklet-impose-tool` → `main`, merged 2026-09-23T23:55:36Z per the API), the
`main` branch became unreadable:

- `git ls-remote origin main` returned `0000000000000000000000000000000000000000
refs/heads/main` — the all-zero SHA, not a normal ref value.
- `GET /api/v1/repos/haku/haku-state/branches/main` returned 404 `branch does not exist
[name: main]`.
- `GET /api/v1/repos/haku/haku-state` still reported `default_branch: main`, `empty:
false`, a nonzero `size` — the repo itself was not empty or newly created.
- Every **other** branch in the repo listed and fetched fine.
- A push of a completely unrelated branch (`reconstruct-main`) failed with:

  ```text
  remote: fatal: bad object refs/heads/main
  ! [remote rejected]   reconstruct-main -> reconstruct-main (missing necessary objects)
  ```

  This was the first sign the corruption was repo-level, not just a missing/deleted
  branch: server-side git connectivity checks choke on whatever `refs/heads/main`
  currently resolves to, for any push.

- Forgejo's own logs, independently confirming it (from an unrelated PR's merge-base
  check, 5 seconds after the merge's `git-receive-pack` completed):

  ```text
  2026/09/23 23:55:41 testPRProtected() [E] testPatch[<PullRequest [158]...>]:
    GetMergeBase: exit status 128 - warning: ignoring broken ref refs/heads/main
  ```

## Root cause — confirmed

`refs/heads/main` on Forgejo's git storage (`/data/git/gitea-repositories/haku/haku-state.git`,
a SeaweedFS FUSE mount on the RWX `forgejo-git-rwx-ssd` PVC, shared by both Forgejo
replica pods) was a **literal 0-byte file**:

```text
  File: .../haku-state.git/refs/heads/main
  Size: 0          Blocks: 0          IO Block: 512    regular empty file
  Modify: 2026-09-23 23:55:35.000000000 +0000
```

That modify timestamp is the same second as the merge's `git-receive-pack` completing in
Forgejo's access log (`POST .../git-receive-pack ... 200 OK in 2329.0ms`, logged
`23:55:35`). The repo's reflog (`logs/refs/heads/main`) is intact and shows exactly what
the update was supposed to write:

```text
d09cb1c2ec57e4e9d2616d7fdb9fb9d61b3f31d0 e875d7bacd4690b1ab04f2dc2e565aafc842959e Rai <agentydragon@gmail.com> 1790207734 +0000	push
```

So: Forgejo's merge handler correctly computed and wrote the merge commit object
(`e875d7ba`, confirmed fetchable, with the correct two parents — `d09cb1c2` the prior
`main` tip and `3cee2484` the PR branch head), correctly logged the ref-update intent to
the reflog, but the actual bytes of the small loose-ref file `refs/heads/main` were never
written/flushed — landing as an empty file instead of the 41-byte SHA+newline it should
contain. Metadata (mtime, reflog) was coherent; file _data_ was lost. This repo has no
`packed-refs` file, so `refs/heads/main` is a plain loose-ref file — the kind git updates
via write-new-content-then-rename, which is exactly the write pattern a FUSE coherence bug
would be positioned to corrupt.

This is the same storage backend and general failure class as three prior incidents
(`2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md`,
`2026_08_24_descheduler_filer_eviction_loop_sigbus.md`,
`2026_08_31_forgejo_seaweedfs_fuse_coherence_recurrence.md`), but **not the same shape**:
those three were all _read_-path faults (SIGBUS on `mmap`, stale directory listings) where
a client's cache was stale but the underlying stored data was intact and readable from
elsewhere. This is a _write_-path fault where the stored data itself came back empty — both
Forgejo replicas read the identical empty file directly from the shared RWX mount (this
was never a split-brain "one replica has bad cache, the other is fine" situation, unlike
Aug 31). That the underlying data was actually lost, not just cached wrong, is a more
serious failure mode than the three prior incidents and is not yet explained by them.

## Recovery

Performed via SSH to `wyrm2` (admin kubectl) once the investigating session got that path
working through Agentplane-staging's `ssh` action group:

1. `git update-ref refs/heads/main <sha>` was tried first and **refused**: git treats an
   already-empty ref file as `reference broken` and won't resolve/overwrite it through the
   normal update-ref path (`cannot lock ref ... unable to resolve reference ...: reference
broken`).
2. Wrote the correct SHA directly, using git's own atomic loose-ref convention
   (write to `refs/heads/main.lock`, `sync`, then `mv` over `refs/heads/main`) rather than
   trusting a straight overwrite, in case the mount was still in a degraded state:

   ```bash
   kubectl exec -n forgejo <pod> -- sh -c '
     echo e875d7bacd4690b1ab04f2dc2e565aafc842959e > .../refs/heads/main.lock &&
     sync && mv .../refs/heads/main.lock .../refs/heads/main && sync'
   ```

3. Applied to `forgejo-5c8ff48d44-f9vm2` first; `git rev-parse refs/heads/main` in that
   pod then correctly resolved the SHA (not just a raw file read — git's own ref
   resolution accepted it).
4. Checked the second replica (`forgejo-5c8ff48d44-vnktl`) before touching it and found it
   **already showed the correct SHA**, with an mtime matching the first pod's write —
   confirming the RWX mount is genuinely shared storage, not per-pod independent state.
   The write against `vnktl` was applied anyway (harmless, idempotent — same value).
5. Verified externally: `git ls-remote` and the REST branches API both now report
   `e875d7bacd4690b1ab04f2dc2e565aafc842959e` for `main`; a push of an unrelated branch
   succeeded (repo-wide push path confirmed unblocked, not just `main` reads).

No data was lost. The merge commit itself was never at risk — only the ref pointer to it —
and it was independently re-derivable from the reflog plus object-graph verification
(right parents, right tree) before ever touching the file.

## Open follow-up

- **What actually caused the torn write is not established.** No SIGBUS, no
  `unsynchronized dir`, no other FUSE coherence log signature was found in Forgejo's logs
  around the merge (only the downstream `ignoring broken ref` / `not our ref 0000...`
  symptoms). Whether this was a SeaweedFS filer/volume-server event, a FUSE client race, or
  something else was not confirmed — repair took priority over root-causing the trigger.
  Worth checking, if revisited: `kubectl get pods -n seaweedfs` / SeaweedFS component logs
  for anything around `2026-09-23T23:55:30–23:55:41Z`.
- **This failure mode isn't covered by the existing SeaweedFS lessons-learned docs**, which
  all describe read-path staleness recoverable by rolling the consumer pod. A `main`-branch
  ref rewritten as empty is data loss on the write path that a pod roll would not have
  fixed (the file would just stay empty) — worth a line in
  `docs/lessons_learned/` if this recurs, since "roll the consumer" is not a fix here.
- Cluster access during this incident went through Agentplane-staging's `ssh` action group
  (`ssh.exec`/`ssh.list_targets`, targeting `wyrm2.nebula.allegedly.works` as
  `agentydragon`), not through Haku Console's `kubectl_passthrough_mcp`/`ssh` MCP servers,
  which turned out to not be live-connected despite appearing in tool search results. A
  Haku Console Kubernetes grant created for this incident
  (`grant_id: efae7710-c51a-4b07-8ec3-ad9f04012ccb`) was approved but never actually used —
  nothing in the investigating session enforced it.
