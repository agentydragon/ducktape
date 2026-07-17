---
name: clean-workspaces
description: >
  Inspect and safely clean stale local development state: old or unused Git
  worktrees and orphaned Bazel output bases. Use when the user asks to clean up
  or remove old, merged, dead, or unused worktrees; prune stale workspaces;
  clean Bazel output bases; reclaim Bazel development disk; or inspect what
  local workspace state can be removed. Drive the `workspace-gc` tool for the
  clear-cut candidates and apply repository and PR judgment to REVIEW items.
---

# Clean Workspaces

One tool covers both domains: `workspace-gc` has a `worktrees` scanner (local git
worktrees whose work is already merged) and a `bazel-bases` scanner (Bazel output bases
whose workspace is gone). Both use the same PRUNE/KEEP/REVIEW model, are dry by default,
and revalidate every candidate immediately before removing it.

**The tool automates what's easy to automate; you apply intelligence where the automation
needs supplementation.** It makes the clear-cut calls — an ancestor merge, a merged PR, a
missing workspace. You supplement: inspect the dirty KEEP trees the tool can't reason about
(build noise vs. real work), judge the REVIEW items, and never recreate its deletion logic
with an ad hoc script.

## Establish Intent And Scope

- If the user asks what _could_ be cleaned, inventory and report without deleting.
- If the user asks to clean up, remove clear candidates, report every ambiguous item, and
  summarize retained state. Do not turn that into permission to delete remote branches,
  unique work, shared caches, or unrelated build state.
- Keep the active worktree, the main checkout, and anything used by a running process.
- Prefer `workspace-gc` from the released, artifact-pinned Nix `ducktape` package. In a
  Ducktape source checkout where it is absent, load the devshell and use
  `bb run //devinfra/gc:workspace_gc --` as the command prefix.

## Inventory First

Run these read-only views before deleting anything:

```bash
workspace-gc worktrees              # classify every worktree of this repo
workspace-gc bazel-bases --sizes    # classify output bases (--sizes runs du; slow)
git worktree prune --dry-run --verbose   # stale admin records
```

Add `--all` to either scanner to show KEEP rows too. Omit `--sizes` for a quick first
pass on a large cache. The base scanner covers one output-user-root at a time (default
`$XDG_CACHE_HOME/bazel/_bazel_$USER`); re-run with `--output-user-root PATH` for known
custom cache locations rather than assuming the default is the only root.

## Worktrees

`workspace-gc worktrees` classifies each worktree; interpret it literally:

- `PRUNE`: clean, idle (not main/active, no process cwd'd inside), and its work is already
  in the main branch — an ancestor, a squash/rebase-merge (merging into main is a no-op),
  an empty branch, or a **merged GitHub PR** for its branch. Remove with
  `workspace-gc worktrees --prune`: it re-scans, runs `git worktree remove` **without
  `--force`** (so a tree that turned dirty is skipped), and **never deletes branches** —
  the work stays reachable through them.
- `KEEP`: main checkout, the invoking worktree, uncommitted (tracked or untracked)
  changes, an open PR, or a live process. Not safe to auto-prune — but inspect it anyway
  (see below); most KEEP rows are dirty trees and the label alone doesn't say whether the
  dirt is real work.
- `REVIEW`: clean but has commits not in main and no merged PR, a detached HEAD with
  unique commits, or an undeterminable default branch. Judge these yourself — inspect the
  commits/PR and decide; removal must not make any commit unreachable.

The PR cross-check uses `GITHUB_TOKEN` or `gh auth token`; pass `--no-prs` to classify on
git signals only (offline). After pruning, re-run `git worktree prune --dry-run --verbose`
and run `git worktree prune --verbose` only if every listed record is confirmed stale.

### Inspect KEEP worktrees, don't just trust the label

`KEEP` means "not auto-prunable", not "leave unexamined". The tool cannot tell real
uncommitted work from build-tool noise, so look inside each dirty KEEP row and report its
real disposition. The report's `LAST ACTIVITY` column and the PR annotation on a dirty row
(`uncommitted changes (PR #N merged)`) are the first triage signals:

- **What is dirty?** `git -C PATH status --porcelain`, then `git -C PATH diff --stat`. If
  the only changes are regenerated or reformatted `BUILD.bazel` / other build-tool output
  (e.g. a buildifier reflow of a `third_party` file), that is noise, not work — revert just
  those files (`git -C PATH checkout -- FILE`) and re-scan; the tree usually flips to a
  clean PRUNE.
- **How stale?** `git -C PATH rev-list --left-right --count origin/HEAD...HEAD`. A tree
  thousands of commits behind is almost certainly an abandoned spike; its uncommitted churn
  is stale mass-reformat noise, not recoverable work.
- **Is the work already safe elsewhere?** Commits pushed to a remote branch, or a merged PR
  (the report annotates the dirty row's PR state), survive worktree removal — so the tree
  can go once you confirm nothing uncommitted still matters.

Classify each dirty KEEP as build-noise, abandoned-spike, or live-WIP; only live-WIP is a
genuine keep.

## Bazel Output Bases

`workspace-gc bazel-bases` classifies each base; interpret it literally:

- `PRUNE`: a default MD5-named base whose recorded workspace is absent, with no live
  server or nested mount. Remove with `workspace-gc bazel-bases --delete`; it revalidates
  and locks every candidate before deletion.
- `KEEP`: leave it alone.
- `REVIEW`: inspect the stated metadata, ownership, symlink, mount, live-use, or
  failed-quarantine issue. Never convert `REVIEW` into `rm -rf` without proving what the
  directory is and why removal is safe.

Re-run the dry scan after deletion. A nonzero delete result or a `.bazel-output-base-gc-*`
quarantine means cleanup is incomplete and needs inspection.

## Leave Judgment Where It Belongs

Handle these manually per situation instead of expanding the tool's deletion surface:

- `REVIEW` worktrees (unmerged commits, detached heads, missing upstreams) and open PRs;
- stale directories that are not registered Git worktrees;
- `REVIEW` output bases and failed GC quarantines;
- nondefault output-base layouts and additional output-user-roots; and
- Bazel's shared repository, disk, and install caches, plus any legacy `repo-contents`
  cache.

Branch deletion is never implied by worktree removal; delete local or remote branches only
when the user separately requests it and their safety is established.

Finish with a concise ledger: worktrees removed and retained, branch actions, output bases
deleted, space reclaimed, roots scanned, and every item still requiring review.
