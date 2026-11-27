# CDviz collection gaps

Broader dashboard requirements: [#14122](https://github.com/agentydragon/ducktape/issues/14122). Remove entries when done.

## Store `status`, `check_run` and `push` webhooks

Not collected today, so `ci_feedback` has no early-red clock (BuildBuddy's live test status is a GitHub commit status) and
no push clock (updates use `pull_request.updated_at`).

- Subscribe: add the events to `events` in `tf/gitops/cdviz-github-webhook/main.tf`.
- Transform: the pinned `github_events/to_v0_5.vrl` dispatches on payload fields and drops these: `status` and
  `check_run` match no branch; `push` emits only on branch creation. Add a local VRL (like `github_policy.vrl`) that runs
  first, keeps the raw body in `customData.github` and passes other events to the upstream transformer. Event clocks:
  `status.updated_at`; `check_run.started_at`/`completed_at`; `push` `repository.pushed_at` (not
  `head_commit.timestamp`, the commit time).
- Keep `X-GitHub-Delivery` (`headers_to_keep`) to detect redeliveries and duplicates.
- Drop `check_run` events from the `github-actions` app: they duplicate `workflow_job`. Keep other apps' check runs.

Done: a PR head's status, non-Actions check run and push events land in `cdviz.cdevents_lake` with raw bodies,
delivery IDs and source clocks, and an Actions job is not stored twice.

## Use them in `ci_feedback`

Add the first failing BuildBuddy live status as a red clock beside job completion, and the push arrival as the head
start clock where one exists. Keep the existing clocks labeled as proxies.

Done: the commit-feedback dashboard shows push-to-green and first-red-from-status for heads that have those events.
