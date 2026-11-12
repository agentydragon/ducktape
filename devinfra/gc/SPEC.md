# Workspace garbage collection

`workspace-gc` discovers worktrees, branches, Bazel output bases, repository-cache entries,
and foreign clones. Without an explicit removal flag it reports classifications only.

## Work and dependencies

Discovery, inspection, and eligible removals can run concurrently. An asyncio task group owns
the item coroutines and a shared semaphore bounds active work. Discovery publishes items
incrementally; waiting for a prerequisite does not consume a worker. Follow-up tasks are
created before their producers finish, so the task group exits only after discovery and all
dependent work finish. A failed or unknown prerequisite is not evidence that an item is safe
to delete.
Worktree identities are resolved once per inventory and indexed for output-base matching;
an unresolved or ambiguous identity cannot establish a dependency on a particular worktree.

Branch classification accounts for worktree holders. A branch held by a prunable worktree waits
for that worktree's removal. Before deleting a branch, the run checks worktree holders, preserves
recovery refs, and uses a compare-and-delete against the classified tip. Git does not provide an
atomic operation combining the tip comparison with a checkout check: a simultaneous checkout can
race with deletion. The run checks again afterward and attempts to restore a branch if it finds
such a checkout. If the branch reflog cannot be inventoried, deletion fails closed. Recovery refs
retain captured commits until those refs are removed separately. An output base can be
reinspected after its workspace disappears. A repository-cache entry is
eligible only after the relevant output bases have been enumerated and their references
examined; immediately before trashing, it and its references are checked again under Bazel's
cache lock. Unknown references prevent deletion. A single cache flusher processes available
entries in batches without waiting for a full batch; emptying trash follows outside the lock.
GC takes an exclusive advisory lock on
the cache directory before opening Bazel's `gc_lock`, then takes Bazel's record lock. This also
excludes other GC threads in this process, whose closing a second `gc_lock` descriptor would
otherwise release the first thread's process-scoped record lock.

An independent foreign clone is reported for review and is not automatically deleted: its Git
repository may contain user data that checking worktrees alone cannot establish is disposable.

## Worktree data safety

A worktree is not eligible for removal if it has tracked or untracked changes or a detached
HEAD. Ignored files and directories do not prevent removal and are deleted by Git, including
local data such as ignored secrets. Every detached HEAD is REVIEW because matching tree content
or patch IDs does not prove that the exact commit is reachable from a surviving ref.

Before removal, re-listing worktrees and reclassification must confirm that the checked-out
branch still matches the scanned branch. The removal entry point independently checks current
HEAD so direct callers cannot bypass that protection. Inspection errors fail closed. A worktree
whose directory is already missing is skipped by removal; inspect `git worktree prune --dry-run
--verbose` and remove stale Git metadata only after confirming each record.

These checks are not atomic with `git worktree remove`. A concurrent detached commit created
after the final check can still be lost; a concurrent branch change after the branch comparison
can also invalidate the PR snapshot. Git's normal refusal to remove tracked or untracked changes
remains the final guard for those classes of files.

## Interruption

Stopping closes admission to new mutations. Admission is decided on the event loop immediately
before an operation starts or is submitted to the thread executor; an already submitted thread
operation may start after the stop request. An admitted mutation finishes and records its
outcome, even if the caller is cancelled again while shutdown drains. Queued work is discarded;
running read-only async work is cancelled and its owned subprocesses are reaped. Blocking
filesystem and libgit2 work is owned until its thread finishes; Python cannot forcibly cancel
such a thread. The CLI treats repeated SIGINT as one stop request, prints the partial removal
report, and exits 130 after draining. A forceful process kill, crash, or stuck kernel operation
cannot provide that guarantee. A subprocess descendant that escapes its process group can
also keep an inherited pipe open; the run waits for it rather than claiming it has stopped.

An async caller receives `CancelledError` after draining; the run object retains its partial
report. An inspection failure stops new work, drains admitted mutations, and reports the
failure together with their outcomes.

## Progress

Progress counts ready, running, waiting, and completed work and records outcomes. Totals can
grow as work is discovered. A bar describes discovered work, not a complete-run estimate, and
the display does not infer an ETA while the total is unknown. The discovering status covers
tasks that may still find inventory items, including producers waiting for other work; once
inventory discovery is finished the display may still show ongoing inspection and cleanup.
