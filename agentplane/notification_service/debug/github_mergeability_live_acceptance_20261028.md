# GitHub mergeability freshness live acceptance — 2026-10-28

Follow-up to the [2026-10-27 exercise](../../debug/github_mergeability_live_acceptance_20261027.md)
after [PR #12343](https://github.com/agentydragon/ducktape/pull/12343), merged as
`29962b7eb37d82e63f326b19451112bc79e24564`.

## Environment and fixture

Both staging notification replicas run `devel-20261028005206-29962b7`, are ready with no
restarts, and completed their migration init containers with exit 0. Deployment generation and
observed generation are 69. Goose migration 00005 applied at 00:54:06.379476 UTC; both new
columns are present. Source freshness remains the default 600 seconds; unknown-result retries
use the compiled 5/15/45/60-second schedule.

Disposable draft [PR #12354](https://github.com/agentydragon/ducktape/pull/12354) uses isolated synthetic branches
`acceptance/mergeability-accept-7a61d42f-base` and `acceptance/mergeability-accept-7a61d42f-head`. Its small orphan history has no CI workflows.
Sandbox `mergeability-accept-7a61d42f` (`36d7701b-a433-42bc-a8c9-785dcd4930dd`), Session `5fa431b4-7994-43b4-9c8e-0c81ef5dbdbd`, and inbox
`8d36496d-bbd7-4434-b0b6-df5da3b2080f` are dedicated to this exercise. The receiver is a fresh dormant Codex Session
on GPT-6 Luna with low effort, instructed only to acknowledge receipt in its reply and end its
turn. The workload bearer is Pod-bound; credentials remain in memory. Only `mergeability_changed`
is selected. No database timestamps or source generations are altered to force progress.

## Base-only change and stale positive proof

The baseline stored `OPEN` / `MERGEABLE` at 00:56:14.840675 UTC with an empty inbox.
Head was `c4e189c709c2b74621f0f6a072349b74e66ec414` and base was `115d345136be3e38a8794a9baecd82077b5bc266`.
Test merge `be21d03e3ff39cf3b1502ac26fbbe4b5b364b5a0` had exactly those two parents.

The base alone advanced to `a4aac0361c7b69214aacd421085c19d5f5c1b737`. The push webhook arrived at
00:56:36.927903 UTC. At 00:56:37.619026, the service stored `UNKNOWN`, the new base tip,
no accepted test merge, and a retry deadline five seconds later. It preserved the previous
settled `MERGEABLE` state and emitted no premature notice. The next reads persisted successive
15/45/60-second retry delays; source errors remained absent.

After initially observing only service/database state, one diagnostic GitHub read at
01:00:04.197980 UTC still returned `mergeable: true` / `clean` with the original base metadata
and test merge. Fetching that immutable commit confirmed its old-base/current-head parents,
while a direct branch-ref read confirmed the new base tip. These operator-authenticated reads exposed a stale definite positive while the service retained
`UNKNOWN` for the current refs. Raw installation-authenticated response bodies were not captured;
individual pending reads are not classified as null versus rejected positives. That extra diagnostic read can trigger GitHub computation,
so subsequent timing is not claimed to involve service reads alone. Cache headers do not
establish whether HTTP caching or background computation caused the stale answer.

At 01:06:37 UTC (600.072 seconds after the base-push receipt), an ordinary REST read still
returned that same stale positive/test merge. An immediately following request with
`Cache-Control: no-cache`, followed by another ordinary read, returned the same result and ETag.
Simple HTTP revalidation did not repair this observation; it does not establish the cause.
The ten-minute base-only window produced no conflict notice. A normal service poll at
01:06:54.160465 still stored `UNKNOWN` with its next retry scheduled.

A diagnostic GraphQL query at 01:07:21 returned `UNKNOWN` / null `potentialMergeCommit`, old
`baseRefOid`, and the actual new tip through `baseRef.target.oid`. The test head then advanced
to `f2020ec9222c7f1c39d0f04bc11c0eae4e021098`, still conflicting. At
01:07:31.590069, the service emitted one `MERGEABLE → CONFLICTING` transition (inbox cursor 1,
sequence 12) with that head and the correct current base. Because the GraphQL read and head
change followed the bounded window, this is not proof of recovery from a base-only change.

## Notices and harness delivery

Notice command `1788b341-adec-42e8-b70d-03428a578f62` was admitted at
01:08:32.497797 UTC, started the previously dormant harness, was confirmed at 01:08:35.445921,
and completed a successful model turn at 01:08:38.362919. The model replied that it received the
notice. The entry-to-admission interval includes the configured 60-second notice debounce.

The head advanced again, still conflicting, to `0a1397a1bdf13c64f981e77965021a1fc22fb06a`.
The source returned through `UNKNOWN` to `CONFLICTING`, without duplicating the settled
transition. The late subscription received one independent snapshot at cursor 2, version 6,
with that head and the correct base `a4aac0361c7b69214aacd421085c19d5f5c1b737`. The snapshot retained the observation
from 01:09:23.623858 UTC, predating subscription creation; it matched only the late subscription.

The resolution commit `ae726acbfbf4ae5594f991367ae77942c8e91728` included both that head and the advanced base as parents.
At 01:11:20.777086, the service emitted one `CONFLICTING → MERGEABLE` transition at cursor 3,
sequence 13, matching both subscriptions. Its payload and stored row retained test merge
`93e88aab125da975249fb192911b722d1b2ce87c`. An immutable GitHub commit read independently
confirmed exactly these parents: `a4aac0361c7b69214aacd421085c19d5f5c1b737` and `ae726acbfbf4ae5594f991367ae77942c8e91728`. The recheck attempt
reset to zero and the early deadline cleared.

All three notices were confirmed by the native harness and completed successful model turns.
The runner's command IDs correlate the notice admission with native receipt and turn completion:

| Notice        | Command ID                             | Confirmed UTC   | Turn completed UTC |
| ------------- | -------------------------------------- | --------------- | ------------------ |
| Conflict      | `1788b341-adec-42e8-b70d-03428a578f62` | 01:08:35.445921 | 01:08:38.362919    |
| Late snapshot | `c8e08e4a-8df1-4b9c-b21f-d9273f08b615` | 01:10:27.759730 | 01:10:29.892019    |
| Resolution    | `93e7fa0a-1c6a-43d7-886e-806dc3eb7c32` | 01:12:23.317975 | 01:12:26.970648    |

## Outcome and remaining work

- Passed: merged image/migration readiness, initial positive parent proof, pending service state while GitHub
  exposed a stale definite positive, current base-ref labels, durable early deadlines and the 60-second
  cap, no premature notice, no duplicate transition for a still-conflicting head, current late
  snapshot labels, positive proof persistence/payload, overlapping-subscription deduplication,
  dormant-harness startup and all three completed model turns.
- Not established: prompt base-only conflict detection. GitHub still returned the old positive
  after ten minutes; ordinary and no-cache reads agreed, while GraphQL later reported unknown.
  The eventual conflict followed additional diagnostics and a head change, so its latency cannot
  be credited to base-only polling. The DAG retains this unresolved provider-freshness work.
- Not exercised: intentionally missed webhooks, injected worker/provider failures or restarts,
  retention expiry, historical event completeness, or an app-unavailable drill. The separate
  deterministic tests cover retry persistence and fencing; this exercise does not reopen waived
  broad outage tests or establish recovery for CI/comment/review notifications.

## Cleanup

After verifying all three entries and native turns, the operator acknowledged only this fixture's
inbox through cursor 3 and cancelled its two subscriptions. PR #12354 was closed without
merging. Both temporary branches were deleted with leases on their exact current SHAs and verified
absent. The owned Sandbox's UID and effective suspended state were checked before deletion.
At 2026-10-28T01:14:01.136052+00:00, no matching Sandbox, Pod, PVC, ServiceAccount, ConfigMap or Service remained;
the owned local port-forward was stopped. Normal retained inbox/source rows and archived Thread
history were left under their existing retention policies; no database purge occurred.

Raw scoped artifacts are under `/tmp/pr-12343-live/`; sanitized rollout metadata and commands
are under `/tmp/notifications-rollout-verification-20261028-F29YNY/`. No credentials are saved.
