---
name: semantic_branch_pruner
description: Audit and prune large sets of remote GitHub branches using branch-specific semantic evidence, linked successor or merged-work proof, calibrated estimates of the owner's deletion decision, a high-throughput HTML decision cockpit, and SHA-guarded deletion. Use when asked which branches are droppable, stale work should be reviewed, branches without PRs need ranking, a branch-cleanup report or cockpit is wanted, or an explicitly approved branch batch should be deleted.
---

# Semantic Branch Pruner

Reduce remote branch clutter without treating age, naming, or inactivity as proof. Separate semantic review from destructive execution.

## Establish the live inventory

1. Fetch and prune remote-tracking refs.
2. Resolve the repository, owner, default branch, and current remote heads.
3. Retrieve all open PR heads, paging through the complete result.
4. Protect the default branch, exact open-PR heads, and any configured protected refs.
5. Record the live SHA of every candidate. Treat branch names, counts, and SHAs from an earlier report as stale.

Reconcile every count: `all remote heads = protected + candidates`. Report exclusions explicitly.

Use conservative mechanical evidence first. A branch whose tip is an ancestor of the default branch, an exact merged-PR head, or patch-equivalent to landed commits is a strong deletion candidate, but still preserve the evidence and wait for deletion authority.

## Review semantics branch by branch

Inspect every remaining candidate rather than applying a generic scoring formula. Gather:

- unique commits and patch equivalence against the default branch;
- merge-base diff, changed paths, and the substantive behavior of the delta;
- PRs associated with the head branch and its commits, including merged or closed history;
- current code implementing the same objective;
- successor commits, PRs, files, or designs that supersede the branch;
- whether the owning subsystem or path was removed, migrated, or is now live elsewhere;
- related stack branches, duplicate tips or trees, and intermediate checkpoints.

Search the current implementation, not only commit subjects. Compare contracts and behavior: identical filenames or topics do not prove supersession, and renamed code can implement the exact objective.

Classify the residual delta with a small vocabulary such as `landed/successor`, `dead owner/path`, `duplicate/checkpoint`, `mostly subsumed`, `partial unique delta`, or `active/unknown`. Use topic clusters only for navigation.

Never use age, prefix, author, ahead/behind count, or topic membership as the primary rationale. When evidence is incomplete, retain for review.

## Turn owner facts into premises

Treat statements such as “this subsystem is dead,” “the migration is complete,” or “the replacement is live” as high-value owner evidence. Record the premise, verify its repository scope, and revisit related branches. Do not extend it to neighboring systems without evidence.

Build a premise pack only after individual branch review. Store its exact membership in the report data. A pack compresses a shared decision; it must not manufacture the individual rationales or probabilities.

## Forecast the owner's deletion decision

When probabilities are requested, estimate `P(owner chooses to delete this remote ref now)`, not code quality or abstract obsolescence.

- Assign every probability from an individual semantic judgment.
- Give a brief rationale.
- Link the strongest merged-work, successor-code, or retired-owner evidence.
- Use prior owner decisions and the overall deletion rate only as calibration checks.
- Never silently fill missing branches with a family-level or age-based score.

Keep a living epistemic note during a long review: resolution, evidence learned, owner premises, calibration, and remaining blind spots. Update it when feedback changes related judgments.

## Present many decisions efficiently

For a large inventory, produce an offline HTML cockpit rather than a flat prose dump. Read <references/cockpit_contract.md> before building it.

Always make the branch name a compare link from the default branch. Put branch-specific rationale and evidence in the row even when it belongs to a premise pack.

## Interpret review decisions

Treat `delete`, `keep`, and `review` as decisions about exact current refs. Export the selected branch names as JSON or a plain list for an auditable handoff.

When the user approves a named premise pack, resolve membership from the exact current report artifact and enumerate it before deletion. Do not reconstruct pack membership heuristically.

Probability thresholds, cockpit selections, candidate discovery, and an agent's `drop-now` label are not deletion authority. Delete only the exact branches the user explicitly approves.

## Delete with a live SHA guard

Immediately before each deletion:

1. Fetch and prune again.
2. Re-resolve the branch's live SHA with `git ls-remote --heads`.
3. Recheck that it is not the default branch or an open-PR head.
4. Compare the live SHA with the approved inventory. Stop or skip if it moved.
5. Delete the exact ref with:

```bash
git push --force-with-lease=refs/heads/<branch>:<live-sha> origin :refs/heads/<branch>
```

Never use an unguarded wildcard, a reconstructed prefix, or a stale SHA. A missing ref is a concurrent change to report, not a reason to broaden the command.

After the batch, fetch with prune, verify every named ref is absent, recount all categories, and regenerate the report once the batch settles. Distinguish deletions performed in this batch from concurrent remote changes.

## Hand off

Lead with the outcome. State:

- exact deleted, skipped, or moved refs;
- verification result and new live counts;
- protected/default/open-PR exclusions;
- remaining decision lanes and any uncertain cases;
- the refreshed clickable report path when one exists.
