---
name: github_stacks
description: Create and maintain native GitHub stacks of dependent pull requests, including REST API membership, rebasing, and asynchronous merging. Use when stacking PRs on GitHub.
---

# GitHub Stacks

Use native stacks for dependent GitHub PRs. All branches must belong to the same
repository; cross-fork stacks are unsupported. Keep independent changes independent.

## Create or extend

1. Push branches and open PRs with each layer targeting the preceding layer's head
   branch. The bottom PR targets the intended trunk (Ducktape: `devel`).
2. Inspect existing membership with `gh api 'repos/OWNER/REPO/stacks?pull_request=NUMBER'`
   or the PR's REST `stack` field.
3. Register the chain explicitly: `POST /repos/OWNER/REPO/stacks` with
   `{"pull_requests":[101,102,103]}` ordered **bottom to top**. Creating ordinary
   branch-based PRs alone does not register a native stack.
4. Read membership with `GET /repos/OWNER/REPO/stacks/STACK_NUMBER`; append PRs with
   `POST /repos/OWNER/REPO/stacks/STACK_NUMBER/add` and the same body shape.

Use existing `gh api` tooling; the `gh stack` extension is optional. GraphQL's
`PullRequest.stack` and `stackEntry` fields are read-only. Fetch the current
[REST reference](https://docs.github.com/en/rest/pulls/stacks) when changing membership.

## Maintain and review

Use GitHub's **Rebase stack** action or, when installed, `gh stack rebase --upstack`
and `gh stack push` to carry lower-layer edits upward. Keep history linear. After
server-side rebases or partial merges, fetch the rewritten branches before pushing;
verify current heads and checks. Unchanged diffs retain approvals on stack rebases.

Every layer inherits the trunk's protections and required checks; GitHub Actions
branch filters also use the stack trunk. Review each layer's incremental diff.
GitHub automatically rebases and retargets remaining layers after a lower merge;
see [stack rules](https://docs.github.com/en/pull-requests/reference/stacked-pull-requests).
Report the stack number, ordered PRs, review commit, and actual check states.

## Merge only when requested

Merging a layer includes **all open layers below it**. Match the authorized scope;
keep deployment gates separate (for example, config must deploy before parser cleanup).

The legacy synchronous merge APIs cannot merge native stacks. Use
`PUT /repos/OWNER/REPO/pulls/NUMBER/merge-async`, passing the expected head `sha`
and desired `merge_method`. The default `merge_action` honors the repository's queue.
Poll `GET /repos/OWNER/REPO/pulls/NUMBER/merge-async/UUID` from the returned request.
`202` means accepted, with branch rules checked later. Distinguish `pending`,
`failed`, `merged`, and `enqueued`: an enqueued result is final for that request,
so check the PR itself for eventual merge completion. Never report queued as merged.
See the [merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request-asynchronously).
