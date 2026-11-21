# Workflow dashboard loses PR associations

The CI workflow dashboard reads PR numbers from
`cdviz.pipelinerun.last_payload.customData.github.workflow_run.pull_requests`.
On 2026-11-16, filtering for PR 16994 initially returned its workflow attempts.
After the PR merged, the same filter returned no attempts. The aggregate
`pipelinerun` rows for branch `codex/github-quota-series` then had a null PR
array, although `cdviz.cdevents_lake` still held earlier events for the same
workflow-run attempt subjects with PR 16994 in that array. The lake also held
later events for those subjects with null PR arrays. The dashboard therefore
cannot rely on the aggregate's latest payload retaining association metadata.

Recover the explicit PR associations across events for each workflow-run
attempt, preserving multiple associated PRs and avoiding branch-only or
commit-only attribution. Check the cost on realistic time ranges before using
the lake in every panel: the dashboard makes multiple queries, and at the time
of investigation the lake's only indexes were on `timestamp` and
`(context_id, subject, timestamp)`. Validate both a merged PR whose latest
payload has no PR list and runs with no association, and verify that the PR
filter and recent-workflow table agree after the fix. Remove this note once the
failure is resolved.
