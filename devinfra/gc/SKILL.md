---
name: workspace-gc
description: >-
  Inspect and safely clean stale local dev state: unused Git worktrees, merged
  branches, orphaned Bazel output bases, unreferenced repo-contents-cache
  entries. Use for pruning worktrees/branches or reclaiming Bazel disk — drive
  the `workspace-gc` tool and apply PR judgment to REVIEW items.
---

# Clean Workspaces

`workspace-gc` runs **one joint scan** over four coupled domains and classifies them
together (the domains depend on each other: a branch checked out in a live worktree can't be
deleted; an output base orphans when its workspace worktree is removed; a repo-cache entry is
unreferenced once the bases linking to it are gone):

- **worktrees** — local worktrees whose work is already merged;
- **branches** — local branches whose work is already in the default branch;
- **bazel-bases** — orphaned Bazel output bases (the default MD5-named ones and Claude Code's
  per-session ones) and bases linked to workspaces under `/tmp`;
- **repo-cache** — entries of Bazel's repo contents cache that no output base links to.

`workspace-gc` with no subcommand runs `all` (every domain). `workspace-gc worktrees`,
`workspace-gc bazel-bases` and `workspace-gc repo-cache` are **views** of that same computed
result, scoped to one domain.
All use the PRUNE/KEEP/REVIEW model, are dry by default, and revalidate every candidate
immediately before removing it.

Scans emit INFO progress to stderr as they inspect each worktree, branch, output base, PR
batch, or requested size, so a large or slow item is identifiable before the final report.

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
- Never discard uncommitted changes on your own. Reverting (`git checkout`/`restore`,
  `git clean`, `worktree remove --force`) is destructive and requires explicit user
  approval each time — surface the finding and propose it, but let the user decide.
- Prefer the `workspace-gc` **binary already on PATH** — it is released via Nix and
  installed as part of `ducktape`, so it is present on your machines. Run it directly
  (`workspace-gc …`). Check with `command -v workspace-gc` first; only if it is absent
  (e.g. a bare Ducktape source checkout without the package installed) do you fall back
  to building+running it under Bazel: load the devshell and prefix the same args with
  `bb run //devinfra/gc:workspace_gc_bin --` — so `workspace-gc worktrees` becomes
  `bb run //devinfra/gc:workspace_gc_bin -- worktrees`.

## Inventory First

Run these read-only views before deleting anything:

```bash
workspace-gc                        # the default: worktrees + branches + bases together
workspace-gc --sizes                # add du-measured base sizes (slow on a large cache)
workspace-gc worktrees              # the worktree slice only
workspace-gc bazel-bases            # the output-base slice only
workspace-gc repo-cache             # the repo-contents-cache slice only
git worktree prune --dry-run --verbose   # stale admin records
```

Add `--all` to show KEEP rows too. Omit `--sizes` for a quick first pass on a large cache.
Pass `--no-prs` to skip the GitHub PR cross-check (offline, or to avoid one API call per
branch on a repo with many branches). The base scanner covers one output-user-root at a time
(default `$XDG_CACHE_HOME/bazel/_bazel_$USER`) plus, always, every Claude Code per-session base
(`~/.claude/session-env/*/bazel-output-base`, where each session's `bazelrc` points Bazel);
re-run with `--output-user-root PATH` for known custom cache locations rather than assuming the
default is the only root.

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
  (e.g. a buildifier reflow of a `third_party` file), that is noise, not work. Report it and
  **propose** reverting just those files (`git -C PATH checkout -- FILE`), which makes the
  tree flip to a clean PRUNE — but never run the revert without explicit user approval:
  `git checkout` discards uncommitted state and is not the agent's to decide.
- **How stale?** `git -C PATH rev-list --left-right --count origin/HEAD...HEAD`. A tree
  thousands of commits behind is almost certainly an abandoned spike; its uncommitted churn
  is stale mass-reformat noise, not recoverable work.
- **Is the work already safe elsewhere?** Commits pushed to a remote branch, or a merged PR
  (the report annotates the dirty row's PR state), survive worktree removal — so the tree
  can go once you confirm nothing uncommitted still matters.

Classify each dirty KEEP as build-noise, abandoned-spike, or live-WIP; only live-WIP is a
genuine keep.

## Branches

The joint scan (`workspace-gc`, shown in the `# Branches` section) classifies each local
branch; interpret it literally:

- `PRUNE`: its work is provably in the default branch and it is not held by a kept worktree —
  established by git (an ancestor, an empty branch, a squash/rebase-merge no-op, or every
  commit having a patch-equivalent already on the default branch) **or** by
  a merged PR whose merged head the branch has not advanced beyond. A branch checked out in a
  _prunable_ worktree is eligible too; `all --prune` removes that worktree first, then the
  branch. Deletion uses `git branch -D` (git's safe `-d` rejects squash-merges) **after
  re-proving** the branch is still prunable — safe because the content is in the default
  branch, but it removes the ref (recoverable only via reflog).
- `KEEP`: the default branch, a branch with an open PR, or a branch checked out in a retained
  worktree (git won't delete a checked-out branch anyway).
- `REVIEW`: commits not in the default branch and no merged PR, or a merged PR the branch has
  advanced beyond (unmerged local commits past the merge point). Judge these yourself —
  removal must not make any commit unreachable.

Branch deletion requires an explicit `--prune` and is never implied by the dry scan. The PR
cross-check uses `GITHUB_TOKEN` or `gh auth token`; `--no-prs` classifies on git signals
alone (so squash-merges the API would have confirmed fall to REVIEW).

## Bazel Output Bases

`workspace-gc bazel-bases` classifies each base; interpret it literally:

- `PRUNE`: a default MD5-named base, or a per-session base under `~/.claude/session-env/`,
  whose recorded workspace is absent or under `/tmp`, with no live server or nested mount. A
  server is live only when some process's argv names that very base as `--output_base` — a
  bare recorded PID proves nothing, since a sandboxed server records its namespace-local PID
  and a host PID is recycled once the server exits. Remove with `workspace-gc bazel-bases --delete`;
  it revalidates and locks every candidate before deletion. Deleting a `/tmp` output base
  leaves its workspace intact.
- `KEEP`: leave it alone. A base whose workspace is a **prunable worktree** outside `/tmp`
  stays KEEP in the `bazel-bases` view (its workspace still exists) but is annotated
  "workspace is a prunable worktree"; run `all --prune` to remove the worktree and then its
  newly orphaned base in one pass.
- `REVIEW`: inspect the stated metadata, ownership, symlink, mount, live-use, or
  failed-quarantine issue. Never convert `REVIEW` into `rm -rf` without proving what the
  directory is and why removal is safe.

Re-run the dry scan after deletion. A nonzero delete result or a `.bazel-output-base-gc-*`
quarantine means cleanup is incomplete and needs inspection.

## Repo Contents Cache

Bazel stores every fetched repository once, under `<output user root>/cache/repos/v1/contents/
<hash>/<uuid>/` with a sibling `<uuid>.recorded_inputs`, and an output base uses it through the
symlink `external/<repo>`. Bazel's own GC (`--repo_contents_cache_gc_max_age`) is age-based,
runs only on an idle server, and never asks whether anything still links to an entry.
`workspace-gc repo-cache` does; interpret it literally:

- `PRUNE`: no output base the scan knows — the root's and the per-session ones — links to the
  entry. Remove with `workspace-gc repo-cache --delete`: it takes Bazel's `gc_lock`
  exclusively (every Bazel command holds it shared while it runs, so nothing can be fetching
  or linking meanwhile; a busy lock skips the round), re-lists the bases and their links,
  then removes the entry the way Bazel's GC does (recorded inputs first, directory via
  `_trash/`).
- `KEEP`: some base that itself stays (KEEP or REVIEW) links to it. An entry whose only
  referrers are **prunable bases** is KEEP in this view, annotated "only prunable bases
  reference it"; run `all --prune` to remove those bases and then the entry in one pass.
- `REVIEW`: a malformed name, a directory without its `.recorded_inputs` (or the reverse), a
  nested mount, or an unreadable entry. Never `rm -rf` one without proving what it is.

`LAST USED` is the recorded-inputs mtime, which Bazel touches on every cache hit. `--sizes`
runs `du` per entry and is slow on thousands of entries. The scan derives the cache location
from `--output-user-root` as Bazel's default does; a base under _another_ output user root
that points `--repo_contents_cache` at this cache is invisible, and an entry only it links to
is deleted — that base re-fetches the repo, the same exposure Bazel's own GC already creates.

## Leave Judgment Where It Belongs

Handle these manually per situation instead of expanding the tool's deletion surface:

- `REVIEW` worktrees (unmerged commits, detached heads, missing upstreams) and open PRs;
- stale directories that are not registered Git worktrees;
- `REVIEW` output bases and failed GC quarantines;
- nondefault output-base layouts and additional output-user-roots; and
- Bazel's shared repository (download), disk, and install caches.

`workspace-gc` deletes only **local** branches it proves are merged; deleting remote branches
is never implied — do that only when the user separately requests it and its safety is
established.

Finish with a concise ledger: worktrees removed and retained, branches deleted and retained,
output bases and repo-cache entries deleted, space reclaimed, roots scanned, and every item
still requiring review.
