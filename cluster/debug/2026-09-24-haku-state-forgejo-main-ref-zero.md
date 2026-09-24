# haku-state: `main` ref lost its content on Forgejo during a PR merge

**Date**: 2026-09-24
**Status**: Data recovered, service restored — `main` ref rewritten on both replicas,
pushes confirmed working again. **Root cause / trigger mechanism NOT established** — what
follows is a precisely characterized symptom (exact byte state, exact timing, what's been
ruled out), not an explanation of why the write landed empty. Left for follow-up
investigation; see "Open follow-up" below.

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

## What's confirmed: the exact state and timing (not why)

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
`packed-refs` file, so `refs/heads/main` is a plain loose-ref file.

**What this is not**: three prior incidents document SeaweedFS FUSE coherence problems on
this same storage backend (`2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md`,
`2026_08_24_descheduler_filer_eviction_loop_sigbus.md`,
`2026_08_31_forgejo_seaweedfs_fuse_coherence_recurrence.md`), and it's tempting to file
this under the same cause. Resist that — none of their signatures are present here (no
SIGBUS, no `unsynchronized dir`, no `possible coherence bug`), their trigger (a filer
restart/eviction) did not happen this time (checked below), and their failure shape is
different in kind: all three were _read_-path staleness where a client's cache was wrong
but the underlying stored data was intact elsewhere; this is stored data itself coming
back empty, and both Forgejo replicas read the identical empty file directly from the
shared RWX mount (never a "one replica has bad cache" split, unlike Aug 31). **The
similarity is "same storage backend, git broke anyway" — that is pattern-matching, not a
mechanism.** Do not write this up elsewhere as a confirmed SeaweedFS FUSE incident; it
resembles one superficially and does not yet meet the bar the three prior writeups do
(direct fault evidence, a fsck/probe comparison, or an identified trigger).

**Checked and ruled out as the trigger**, all via `kubectl get events`/`describe`/`df`
in the `forgejo` and `seaweedfs` namespaces, ~20 minutes after the incident (well within
default event retention):

- No SeaweedFS filer/volume-server/master pod restart, eviction, or event of any kind in
  the `seaweedfs` namespace around `23:55:30–23:55:41Z` (or at all in the visible window).
- No `seaweedfs-csi-driver-mount`/`-node`/`-controller` pod restart around that time —
  every mount pod's last restart was 2+ days prior to the incident.
- No disk pressure: `/data` inside the Forgejo pod reads `200.0G, 4.9G used, 2%` — nowhere
  near ENOSPC.
- No Forgejo pod restart (`RESTARTS: 0` on both replicas, ages predating the incident).

None of the obvious candidate triggers panned out. What's left unexplained: a
network/RPC-level hiccup between the FUSE client and the filer that produced no k8s-visible
event; something in Forgejo's own git library's ref-write code path (version, error
handling); or something else not yet considered.

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

- **What actually caused the torn write is not established.** The obvious candidates (filer/
  volume-server/mount-pod restart, disk pressure) are ruled out — see above. Not checked:
  actual pod/container logs from the SeaweedFS filer and the specific
  `seaweedfs-csi-driver-mount`/`-node` pod backing this PVC's mount for anything around
  `23:55:30–23:55:41Z` (only `kubectl get events`/`describe` were checked, not `kubectl
logs` on those components); Forgejo's own git library version and how it performs a ref
  update (single write, or lock-then-rename — assumed but not verified from source); whether
  any other operation was concurrently touching `refs/heads/main` at that exact second
  (Flux's `gitrepository/haku-state` reconciler, a CI job, etc.).
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
