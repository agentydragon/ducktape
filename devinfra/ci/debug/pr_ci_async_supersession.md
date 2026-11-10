# Admit ordinary PR CI before obsolete runs finish cancellation

Three November 3, 2026 PR update pairs waited 54, 58, and 80 seconds for their
predecessor's concurrency group. GitHub's check annotations identified the same
PR group; replacements started two seconds after old jobs became terminal.
The shell wrapper exited 0.15–0.34 seconds after its cancellation trap. Most
of this interval precedes visible signal handling; public metadata does not
identify when GitHub decided or sent cancellation. Shortening shell teardown
cannot remove the measured queue.

The pilot makes only ordinary same-repository PR Bazel groups unique to the
workflow run. A separate `workflow_run: requested` controller checks out its own
trusted default-branch workflow SHA, with credentials persistence disabled, and
runs the stdlib cancellation helper. It has no dependency edge to new CI and
never waits for old completion. Only this controller job gains Actions write;
Bazel tokens, push/release grouping, and both trusted-fork serialization and
approval policies stay as before. No PR code, artifacts, or BuildBuddy credential
enters the controller.

Cancellation requires matching positive repository/workflow IDs, the CI workflow
path, an ordinary PR event from this repository, exact PR ID/number association,
and the same head repository/branch. Candidates have a strictly smaller
per-workflow run number, different head SHA, an active status, and attempt 1.
The trigger must still match the open PR's live head. Re-fetching each candidate
and the live PR immediately before POST protects observed identity, attempt,
status, and head changes. Delayed controllers cannot cancel their own or newer
run IDs. Nested run `pull_requests[].head.sha` is mutable and is never used as
original-head identity. Ordinary `run.head_sha` supplies that identity.

The pilot deliberately protects observed manual attempts. `requested` does not
fire on reruns, and a workflow's run number does not advance on rerun. Missing or
unexpected metadata/API errors stop processing. Each cancellation POST is sent
once, without force cancellation, retry after an uncertain result, or polling
for termination. GitHub's endpoint accepts a run ID, without atomic conditional
head/attempt guards: a rollback or manual rerun between the last GET and POST
can still race cancellation. This is the residual API boundary, not an absolute
promise that every live SHA or concurrent manual attempt is protected.

The helper's behavior tests validate the identity boundary, pagination,
re-fetches, stale/out-of-order callbacks, and uncertain write outcomes. The
workflow's permissions, checkout, and admission configuration were reviewed directly.
Hosted controller execution
cannot be proven on a PR because `workflow_run` loads default-branch workflow
code. It needs merge and naturally occurring PR updates to verify that a new
job starts before its canceled predecessor becomes terminal, while only older
eligible runs receive cancellation. A pre-merge run exercises source checks but
not that live controller path.

The observed queue supplies a 54–80 second opportunity ceiling, not measured
savings. The two successful replacements still occupied runners for 305 and
479 seconds, so this pilot alone does not establish a five-minute required-gate
mean. Record actual gate time and overlapping RBE cost after deployment.

Validation of the initial candidate: changed-file pre-commit passed, and
`bbr test //devinfra/ci:test_cancel_obsolete_pr_runs //devinfra/ci:cancel_obsolete_pr_runs`
passed with the production library named explicitly for Ruff/mypy aspects
([invocation](https://app.buildbuddy.io/invocation/392d084f-c695-4db7-bd0b-fffea3d6d527)).
This is source/test validation, not hosted controller or latency proof.

Schema verification: the primary Octokit webhook
[requested schema](https://github.com/octokit/webhooks/blob/aa80c24a3f55354465b8b1438d33dd5602956d0d/payload-schemas/api.github.com/workflow_run/requested.schema.json)
uses the common
[workflow-run schema](https://github.com/octokit/webhooks/blob/aa80c24a3f55354465b8b1438d33dd5602956d0d/payload-schemas/api.github.com/common/workflow-run.schema.json),
which requires `path`, `repository`, `head_repository`, `run_attempt`, and
`pull_requests`. Each associated PR requires head/base repository references.
The primary requested example supplies these fields but has no associated PR;
that case is intentionally ineligible. Empty/missing associations never authorize
cancellation. A requested PR webhook from this repository remains a hosted proof
boundary until the controller runs on the default branch.

The controller job has a three-minute timeout. An HTTP 409 response to cancel is
reported as Conflict and stops processing: the API does not establish from that
status alone that a run finished. No cancellation POST is retried, including
409 or an unknown transport outcome.
