---
name: clean-workspaces
description: >
  Inspect and safely clean stale local development state: old or unused Git
  worktrees and orphaned Bazel output bases. Use when the user asks to clean up
  or remove old, merged, dead, or unused worktrees; prune stale workspaces;
  clean Bazel output bases; reclaim Bazel development disk; or inspect what
  local workspace state can be removed. Automate provenance-confirmed Bazel
  candidates and apply repository and PR judgment to worktrees and ambiguous
  state.
---

# Clean Workspaces

Treat cleanup as one inventory with two safety models: judge Git worktrees from
repository state, and delegate default Bazel output-base deletion to
`bazel-output-base-gc`.

## Establish Intent And Scope

- If the user asks what _could_ be cleaned, inventory and report without
  deleting.
- If the user asks to clean up, remove clear local candidates, report every
  ambiguous item, and summarize retained state. Do not turn that into permission
  to delete remote branches, unique work, shared caches, or unrelated build
  state.
- Keep the active worktree and anything used by a running agent or process.
- Prefer `bazel-output-base-gc` from the released, artifact-pinned Nix
  `ducktape` package. If it is absent in a Ducktape source checkout, load the
  repository devshell and use `bb run //devinfra/gc:bazel_output_base_gc --` as
  the command prefix. Do not recreate its logic with an ad hoc deletion script.

## Inventory First

Run these read-only views before deleting anything:

```bash
git worktree list --porcelain
git worktree prune --dry-run --verbose
bazel-output-base-gc --sizes
```

The released command is the runnable recipe. Component tests cover its behavior,
and Nix CI imports its backing module from the wheel. Size calculation invokes
`du` and can be slow; omit `--sizes` for a quick first pass on a large root. Use
`--all` when retained bases matter to the investigation.

The GC scans one output-user-root at a time. Its default is
`$XDG_CACHE_HOME/bazel/_bazel_$USER` (or `~/.cache/bazel/...`). Inspect known
custom cache locations and run it again with `--output-user-root PATH` rather
than assuming the default is the only root.

If this repository uses `wt`, add `wt ls` for its PR/status view, but verify the
underlying Git and process state independently.

## Judge Worktrees

For every candidate, inspect its path, branch, status, upstream, commits, PR,
and live use. A removable worktree is normally all of the following:

- not the active worktree and not the repository's main checkout;
- clean, including untracked files;
- not used as a process working directory and not holding relevant open files;
- associated work is merged, abandoned with explicit user intent, or otherwise
  recoverable from a named local or remote ref; and
- removal will not make any commit unreachable, with detached HEADs receiving
  special scrutiny, and no open PR still needs the tree.

Use both Git reachability and the hosting service's current PR state. A squash
merge may be reported merged without making the worktree commit an ancestor of
the base branch. Conversely, a local branch name alone does not prove a PR is
open or merged.

Remove a managed tree with interactive `wt rm NAME` when appropriate, otherwise
use `git worktree remove PATH`. Do not use `--force` merely to bypass dirty-state
protection. Worktree removal does not imply branch removal; delete local or
remote branches only when the user separately requested it and their safety is
established. A branch-attached worktree may be removed while its unique or
unpushed commits remain safely reachable through that local branch.

After removing trees, review `git worktree prune --dry-run --verbose` again,
then run `git worktree prune --verbose` only if every listed administrative
record is confirmed stale. The command cannot select individual records from a
mixed list.

## Clean Bazel Output Bases

Interpret the tool's classifications literally:

- `PRUNE`: a default MD5-named base has agreeing Bazel provenance, its recorded
  workspace is absent, no live Bazel server or nested mount was found, and the
  inactivity grace has elapsed. After a cleanup request, run
  `bazel-output-base-gc --delete`; it revalidates and locks every candidate
  before deletion.
- `KEEP`: leave it alone.
- `REVIEW`: inspect the stated metadata, ownership, symlink, mount, live-use, or
  failed-quarantine issue. Never convert `REVIEW` into `rm -rf` without proving
  what the directory is and why removal is safe.

Keep the default seven-day grace unless the user explicitly wants immediate
reclamation and the recently removed workspaces are known. In that case,
`--older-than 0s` still receives all other safety checks.

Run the dry scan again after deletion. A nonzero delete result or a
`.bazel-output-base-gc-*` quarantine means cleanup is incomplete and requires
inspection.

## Leave Judgment Where It Belongs

Handle these manually per situation instead of expanding the GC tool's deletion
surface:

- dirty or untracked worktrees, detached heads, missing upstreams, unique
  commits, open PRs, and running processes;
- stale directories that are not registered Git worktrees;
- `REVIEW` output bases and failed GC quarantines;
- nondefault output-base layouts and additional output-user-roots; and
- Bazel's shared repository, repo-contents, disk, and install caches.

Finish with a concise ledger: worktrees removed and retained, branch actions,
output bases deleted, space reclaimed, roots scanned, and every item still
requiring review.
