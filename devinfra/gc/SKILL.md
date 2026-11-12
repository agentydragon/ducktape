---
name: workspace-gc
description: >-
  Inspect and safely clean stale local dev state: unused Git worktrees, local branches,
  orphaned Bazel output bases, and unreferenced repo-contents-cache entries. Use for pruning
  worktrees and branches or reclaiming Bazel disk — drive `workspace-gc` and apply PR judgment
  to REVIEW items.
---

# Clean Workspaces

`workspace-gc` processes four coupled domains concurrently (branch classification
depends on its worktree holders; an output base orphans when its workspace worktree is removed;
a repo-cache entry is unreferenced once the bases linking to it are gone):

- **worktrees** — local worktrees whose work is already merged;
- **branches** — local branches whose work is already in the default branch;
- **bazel-bases** — orphaned Bazel output bases (the default MD5-named ones and Claude Code's
  per-session ones) and bases linked to workspaces under `/tmp`;
- **repo-cache** — entries of Bazel's repo contents cache that no output base links to.

`workspace-gc` with no subcommand runs `all` (every domain). `workspace-gc worktrees`,
`workspace-gc bazel-bases` and `workspace-gc repo-cache` scope the work to one domain.
All use the PRUNE/KEEP/REVIEW model and are dry by default. Workspace state is assumed not to
be modified externally during a run; output bases and repo-cache entries are also rechecked
under their respective locks before removal.
With a deletion flag, removals start while other items are being scanned; completed work can
unlock further work. Findings describe each item as observed, not a single snapshot. The live
display shows ready, running, dependency-waiting, and finished counts; each bar covers only work
discovered so far, so it can move backward as new work is added.
Gitignored files are not uncommitted changes, so removing a worktree deletes them, including
local data such as a gitignored `.env`. Foreign clones are only reported and remain in place.

Scans emit INFO progress to stderr as they inspect each worktree, branch, output base, PR
batch, or requested size, so a large or slow item is identifiable before the final report.

Git or filesystem failures inspecting a worktree, branch, or foreign clone produce `REVIEW`
with the error, leaving that item intact while other items continue. An existing directory with unreadable Git metadata is never treated as
a missing worktree. Failures that block the entire command exit nonzero with a diagnostic.
An item skipped at deletion (it no longer qualifies, or its Bazel lock or the cache is busy) is
reported as `SKIPPED` and does not change the exit status; a failed removal does.

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

Repo-cache reports summarize PRUNE and KEEP entries and list REVIEW entries; use `--verbose`
to list PRUNE entries and individual deletion results, or `--all` to list all cache entries.
Add `--all` to show KEEP rows in other domains too. Omit `--sizes` for a quick first pass on a large cache.
Pass `--no-prs` to skip the GitHub PR cross-check (offline, or to avoid one API call per
branch on a repo with many branches). The base scanner covers one output-user-root at a time
(default `$XDG_CACHE_HOME/bazel/_bazel_$USER`) plus, always, every Claude Code per-session base
(`~/.claude/session-env/*/bazel-output-base`, where each session's `bazelrc` points Bazel);
re-run with `--output-user-root PATH` for known custom cache locations rather than assuming the
default is the only root.

## Worktrees

`workspace-gc worktrees` classifies each worktree; interpret it literally:

- `PRUNE`: clean (no tracked or untracked changes), idle (not main/active, no process cwd'd inside),
  and its work has landed on the default branch, or its branch has a merged or closed PR.
  `workspace-gc worktrees --prune` calls `git worktree remove` without `--force`; Git deletes ignored files. Attached branches survive worktree removal. A detached
  HEAD with landed content or patches is eligible; Git removes its HEAD and reflog without
  creating recovery refs. The tool assumes no external modifications to workspace state during
  its run, including worktrees and their checked-out branches and HEADs. If the worktree
  directory is already missing, removal is skipped; review stale
  entries with `git worktree prune --dry-run --verbose` and prune only confirmed records.
- `KEEP`: main checkout, the invoking worktree, uncommitted (tracked or untracked)
  changes, an open PR, or a live process. Not safe to auto-prune — but inspect it anyway
  (see below); most KEEP rows are dirty trees and the label alone doesn't say whether the
  dirt is real work.
- `REVIEW`: clean but has commits not in main and no merged PR, a detached HEAD with unique commits, or an
  undeterminable default branch.
  Judge these yourself — inspect the
  commits/PR and decide; removal must not make any commit unreachable.

The PR cross-check uses `GITHUB_TOKEN` or `gh auth token`; pass `--no-prs` to classify on
git signals only (offline). An unavailable PR check produces REVIEW for otherwise-prunable
work. After pruning, re-run `git worktree prune --dry-run --verbose`
and run `git worktree prune --verbose` only if every listed record is confirmed stale.

## Foreign Clones

The `all` view reports same-project clones found through Bazel output-base workspaces.
They are independent Git repositories whose worktrees do not account for all refs, commits,
or other data. A clone whose worktrees appear prunable is REVIEW; the tool never removes
an independent clone. The former `--delete-foreign-clones` flag is rejected.

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

- `PRUNE`: a historical content candidate based on git showing its changes are in the default
  branch (an ancestor, an empty branch, a squash/rebase-merge no-op, or every commit having a
  patch-equivalent on the default branch), or a merged PR whose head the branch has not advanced
  beyond. The classifier also marks a closed-not-merged PR as a candidate when the branch has
  not advanced past its PR head; that is not evidence its unique commit history is disposable.
  A branch checked out in a _prunable_ worktree is a candidate too; `all --prune` removes that
  worktree first. Branch removal preserves recovery refs and compare-deletes the classified
  tip, skipping branches that have moved or are checked out. Git cannot atomically combine the
  tip comparison with the checkout check; the tool checks again after deletion and attempts to
  restore the branch if a concurrent checkout is observed.
- `KEEP`: the default branch, a branch with an open PR, or a branch checked out in a retained
  worktree.
- `REVIEW`: commits not in the default branch and no candidate PR signal, or a branch that has
  advanced past its merged or closed PR head. Judge these yourself — removal must not make any
  commit unreachable.

`--prune` prints a recovery ref for each removed branch. Restore the tip with
`git branch <branch-name> <recovery-ref>`; additional refs in the same recovery directory
preserve commits found in the branch reflog. Those refs keep the captured commits until removed
separately. If the reflog cannot be inventoried, branch deletion fails closed. The PR cross-check uses `GITHUB_TOKEN` or
`gh auth token`; `--no-prs` classifies on git signals alone (so squash-merges the API would
have confirmed fall to REVIEW).

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

Re-run the dry scan after deletion. A nonzero delete result (a failed removal) or a `.bazel-output-base-gc-*`
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
  nested mount, an unreadable entry, or an incomplete reference scan. An incomplete fresh
  reference scan also prevents deletion under the cache lock. Never `rm -rf` one without
  proving what it is.

`LAST USED` is the recorded-inputs mtime, which Bazel touches on every cache hit. `--sizes`
runs `du` per entry and is slow on thousands of entries. The scan derives the cache location
from `--output-user-root` as Bazel's default does; a base under _another_ output user root
that points `--repo_contents_cache` at this cache is invisible, and an entry only it links to
is deleted — that base re-fetches the repo, the same exposure Bazel's own GC already creates.

## Leave Judgment Where It Belongs

Handle these manually per situation instead of expanding the tool's deletion surface:

- `REVIEW` worktrees (unmerged commits, unique detached heads, missing upstreams) and open PRs;
- stale directories that are not registered Git worktrees;
- `REVIEW` output bases and failed GC quarantines;
- nondefault output-base layouts and additional output-user-roots; and
- Bazel's shared repository (download), disk, and install caches.

Deleting remote branches is never implied — do that only when the user separately requests it
and its safety is established.

Finish with a concise ledger: worktrees removed and retained, branches deleted and their
recovery refs, output bases and repo-cache entries deleted, space reclaimed, roots scanned,
and every item still requiring review.
