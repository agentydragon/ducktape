# haku-state: `main` ref reads as all-zero SHA on Forgejo, blocking all pushes

**Date**: 2026-09-24
**Status**: Open — root cause not yet confirmed, recovery not yet attempted (blocked on
cluster access from the investigating session)

## Symptom

Shortly after Rai clicked the Forgejo web UI's "Merge" button on `haku/haku-state` PR
#128 (`haku/booklet-impose-tool` → `main`), the `main` branch became unreadable:

- `git ls-remote origin main` returns `0000000000000000000000000000000000000000
refs/heads/main` — the all-zero SHA, not a normal ref value.
- `GET /api/v1/repos/haku/haku-state/branches/main` returns 404 `branch does not exist
[name: main]`.
- `GET /api/v1/repos/haku/haku-state` still reports `default_branch: main`, `empty:
false`, a nonzero `size` — the repo itself is not empty or newly created.
- Every **other** branch in the repo lists and fetches fine (confirmed against
  `haku/booklet-impose-tool` and ~50 other stale branches).
- A push of a **completely unrelated** branch (`reconstruct-main`, no relation to
  `main`) failed with:

  ```text
  remote: fatal: bad object refs/heads/main
  ! [remote rejected]   reconstruct-main -> reconstruct-main (missing necessary objects)
  ```

  This is the significant data point: the corruption isn't just "the `main` ref is
  missing/pointing at nothing" (which would only break operations touching `main`). It's
  breaking pushes to the repo generally, which means server-side git connectivity/object
  checks are choking on whatever `refs/heads/main` currently resolves to.

Confirmed persistent over multiple retries a few minutes apart — not a caching blip.

## What's ruled in / ruled out so far

- **Not a Rai action beyond the normal merge button.** He confirmed he just clicked
  Forgejo's own "Merge" PR button — no manual git commands, no branch deletion, no API
  calls.
- **Not a repo-emptying event.** `default_branch`/`empty`/`size` on the repo API all read
  normal.
- **Not (as far as checked) affecting the two Forgejo pods' health.** Both
  `forgejo-5c8ff48d44-{f9vm2,vnktl}` are `Running`, normal CPU/memory, no restarts. (Per
  `2026_08_31_forgejo_seaweedfs_fuse_coherence_recurrence.md`: a `Running` FUSE-mount
  consumer pod does not imply the underlying mount/client is healthy — this is a known
  trap, not a clean bill of health.)

## Leading hypothesis: SeaweedFS FUSE coherence loss during the merge's ref write

Forgejo's `/data` (including `haku-state.git`) is a SeaweedFS FUSE mount on the
`forgejo-git-rwx-ssd` PVC. Three prior incidents document the same underlying failure
class — a SeaweedFS FUSE client losing coherence with its backing filer/volume servers,
which then surfaces as git-level breakage even though the stored data is intact:

- `2026_07_04_seaweedfs_stale_mount_cache_after_evacuation.md`
- `2026_08_24_descheduler_filer_eviction_loop_sigbus.md` — filer eviction loop desynced
  FUSE clients (`meta_cache.go:331 unsynchronized dir`); git's `mmap`-based packfile
  reads then faulted with `SIGBUS`, killing `upload-pack`/`unpack-objects` cluster-wide.
- `2026_08_31_forgejo_seaweedfs_fuse_coherence_recurrence.md` — same mechanism, unproven
  trigger; one Forgejo replica faulted on `git fsck --full`/tree reads while its sibling
  read the identical repo cleanly, proving client-side (not repository) corruption.
  Recovery was replacing the Forgejo pods, which tore down and rebuilt the CSI mount
  subscription.

**What would make this incident a new variant rather than a repeat**: the prior three are
all _read_-path faults (SIGBUS on `mmap`, stale directory listings). This one looks like a
_write_-path fault: Forgejo's merge handler updates `refs/heads/main` (a small file write,
or a `packed-refs` rewrite) as part of completing the PR merge. If the FUSE client's
coherence was already degraded, or lost coherence exactly during that write, the ref file
could land as empty/zeroed/torn rather than the correct SHA — which matches an all-zero
ref far better than a random corrupt hash would. Not confirmed; this is the working
theory pending a live look at the pod/mount state.

## What's blocking further diagnosis right now

The investigating Claude Code session (ducktape harness) does not currently have a
working path to cluster shell access:

- Direct `kubectl` from the harness (`oidc-ksbx:haku-k8s` identity) is denied
  `pods/log`/`pods/exec` in the `forgejo` namespace by RBAC (confirmed via
  `kubernetes_can_i`).
- A Haku Console Kubernetes grant for `forgejo` namespace `pods/log`+`pods/exec` was
  created and approved (`grant_id: efae7710-c51a-4b07-8ec3-ad9f04012ccb`, expires
  2026-09-24T01:00:34Z), but nothing in this session actually enforces it: the only live
  Haku MCP servers connected are `github`, `sandbox`, `grants` — `ssh` and
  `kubectl_passthrough_mcp` appear in tool search results but are not connected servers
  (`list_mcp_servers` confirms only the three).
- The Haku sandbox pod's own `kubectl` hits the same `oidc-ksbx:haku-k8s` RBAC denial.
- The harness has no local SSH client config/keys to reach `wyrm2` directly.

Rai has working admin `kubectl` access from `wyrm2` (and SSH access there as
`agentydragon`) — recovery/further diagnosis needs to go through him directly, or through
restoring a live cluster-access path to this session.

## Recommended next step (unexecuted — needs cluster access)

Based on the established recovery pattern from the three prior incidents: **roll both
`forgejo` pods** (`kubectl rollout restart deployment/forgejo -n forgejo`, or delete each
pod one at a time to keep one replica serving throughout) to force the CSI mount
subscription to tear down and rebuild, clearing any stale FUSE client state. Then verify:

```bash
git ls-remote https://git.allegedly.works/haku/haku-state.git main
# expect a real 40-hex SHA, not all-zero
```

Before rolling, it would be worth a quick look (if access allows) at:

- `kubectl get pods -n seaweedfs` — any recent filer/volume-server restarts or evictions
  around the merge time (the Aug 24 trigger).
- Forgejo pod logs around the merge timestamp for FUSE coherence warnings
  (`unsynchronized dir`, `ErrNotFound ... possible coherence bug`) matching the Aug 31
  incident.
- Whether both Forgejo replicas disagree on `main` right now (one might still have it
  correctly cached in memory even if the on-disk ref is bad) — if so, that replica could
  be used to re-derive/re-push the correct `main` before rolling either pod.

## Current safety state

No data has been lost. The investigating session has, validated and ready to push once
`main` is readable again:

- The last-known-good `main` content (through a Tuscan hearing-prep commit and an
  Ivan/Livable-context commit).
- The `haku/booklet-impose-tool` branch (PR #128's `--skip-sheets` commit), which was
  never actually merged into `main` server-side despite the PR showing `merged: true` --
  the merge commit that should exist on `main` was never durably written.
- A clean local merge of the above two (branch `reconstruct-main`, zero conflicts),
  ready to push as the reconstructed `main` once the underlying corruption is cleared and
  a push actually succeeds.
