# Staging GitHub acceptance

## Initial delivery acceptance — 2026-10-04

App **5188971** is enabled; the operator confirmed installation on all their repositories.
[Rollout proof](https://github.com/agentydragon/ducktape/issues/8956#issuecomment-5983967691): 2/2 ready replicas, successful migrations, Flux healthy; no database reset.

Verified on `agentydragon/ducktape`, without integration-app API calls:

- 13 real branch CI events (`workflow_run`, `check_run`, `check_suite`, `status`) reached inbox and harness.
- [A PR comment](https://github.com/agentydragon/ducktape/pull/8982#issuecomment-5984066218) produced one inbox entry matching both overlapping subscriptions.
- Reads preserved acknowledgement; the agent explicitly acknowledged through cursor 24 and cancelled test subscriptions.

Not proved: webhook-redelivery deduplication, restart recovery, revoked access, broader repository/event/fork coverage or negative ingress probes.
Debounce [#8988](https://github.com/agentydragon/ducktape/pull/8988) is not part of this deployed proof.

## Shared GitHub state rollout — 2026-10-09

PR [#9482](https://github.com/agentydragon/ducktape/pull/9482) merged as `00003af`.
Staging ran notification image `devel-20261009024342-00003af` with 2/2 updated, ready and
available replicas, no old replicas remaining, and zero restarts at the rollout check.
Both image-coupled migration init containers exited 0. Readiness passed on both replicas;
it checks worker tasks, PostgreSQL connectivity and the LISTEN connection.

An authenticated read of an existing cancelled PR subscription returned structured
`github.access` and `github.subject` observations. Shared repository access was currently valid,
with a successful refresh and no recorded error/retry deadline. New pods accepted webhooks
with HTTP 202. This proves rollout and basic status projection, not full behavioral acceptance.

CI passed for the merged change, including 70 GitHub notification tests covering normalized
entities, structured delivery-subject links, leases/generation fences, refresh reuse, expiry
rollback, migrations and required nullable response fields. Those are automated tests, not
live restart/revocation/fork acceptance.

Rollout logs also showed webhook HTTP 400 rejections. A subsequent bounded 35-minute log read
found 154 webhook 400 responses and 352 webhook 202 responses across the two replicas;
rejections were recurring, not a single startup blip. At that point the access log did not
identify an event or rejection reason. The diagnosis and fix are recorded below.

## Workflow-job and issue delivery — 2026-10-09

Rejection diagnostics from [#9528](https://github.com/agentydragon/ducktape/pull/9528), deployed as
`devel-20261009042853-3ee90b7`, identified 142 unsupported `workflow_job` events and one unsupported
`issues` event in the inspected sample. No signature or schema-validation failures appeared in
that sample. This diagnoses the observed rejections, not every possible ingress failure.

Support landed in [#9531](https://github.com/agentydragon/ducktape/pull/9531). Staging ran image
`devel-20261009051250-b7234b2` with 2/2 updated, ready and available replicas, zero restarts,
and both migration init containers exiting 0. A bounded post-rollout log read contained 166
webhook responses, all HTTP 202, with no rejection diagnostics. The live source schema exposed
`workflow_job`, `issues` and the `issue` subject. This is a sampled observation, not a guarantee
that all future deliveries succeed.

Live verification on `agentydragon/ducktape`:

- An explicit `workflow_job` subscription on branch `devel` received a real completed
  [Actions job](https://github.com/agentydragon/ducktape/actions/runs/37888498514/job/113683830200)
  (`announce`, run 37888498514, head `afd8a805dc660ff2786956432996be903061b871`). The entry matched
  the test subscription and reached the session inbox at cursor 1436; it was explicitly acknowledged.
- An ordinary issue subscription to existing issue #8956 passed authorization, shared subject
  refresh and subscription processing without errors, confirming the installation's issue access.
- Operator-authorized test issue [#9535](https://github.com/agentydragon/ducktape/issues/9535)
  produced `issues.opened`,
  [`issue_comment.created`](https://github.com/agentydragon/ducktape/issues/9535#issuecomment-6074921995),
  and `issues.closed` at inbox cursors 1437–1439. Each matched the issue-only test subscription;
  the comment ID and content matched the test comment. The opened webhook arrived after subscription
  creation; this is live receipt, not historical replay.
- The test issue is closed, all temporary subscriptions are cancelled, and the inbox was explicitly
  acknowledged through cursor 1439. No test workflow or repository code change was needed.

These observations close live delivery acceptance for the exercised workflow-job and ordinary
issue lifecycle/comment paths. They do not establish every action filter, issue/PR exclusion
case, fork/revocation behavior, redelivery deduplication, or shared refresh reuse across restarts;
CI coverage is separate from live proof.

## Waived live acceptance exercises

The operator waived deliberate live GitHub failure/backoff injection on 2026-10-09. Error and
retry-deadline projection through subscription introspection and frontend diagnostics is already
implemented and covered by automated tests/rendered fixtures; successful status projection was
checked in staging. No further induced-failure exercise is required for acceptance. This is a
scope decision, not a claim that a live failure/recovery scenario was performed.

The operator also waived live verification of shared GitHub refresh reuse across overlapping
subscriptions and service restarts. Existing automated coverage is sufficient for this behavior;
there is no remaining deployed-environment exercise or acceptance blocker. This does not claim
that a live refresh-reuse/restart test was performed or remove the automated regression tests.

## Configurable GitHub client rollout — 2026-10-09

PR [#9544](https://github.com/agentydragon/ducktape/pull/9544) merged as
`2d23dc31af50999a4ca76f41d90612402a3fa8d0`. Staging Deployment generation 49 was observed
at generation 49 with 2/2 updated, ready and available replicas running
`devel-20261009071137-2d23dc3`, no old replicas remaining and zero restarts.
Both migration init containers exited 0.

A bounded log read (`--since=10m`, at most 5000 lines per pod) found 167 webhook responses,
all HTTP 202, and 51 readiness responses, all HTTP 200. No error, traceback, webhook-rejection
or shared-refresh-failure diagnostics appeared in the sample. An authenticated subscription
status read succeeded and returned the structured shared observations without an error.
Its cached successful access observation predated the rollout; it is not evidence of a fresh
outbound GitHub request by the new client.

The deployed revision and normal API/ingress health check are complete. Custom API endpoint,
timeout and version overrides, token-cache behavior and redirect rejection were tested in CI;
they were not exercised by changing staging configuration. No deliberate GitHub failure/backoff
injection or refresh-reuse/restart exercise was performed or remains required.
