# Actions runner queue burst, 30 October 2026

## Finding

The 20:58–21:05 UTC burst includes two visual publisher jobs that each held a
GitHub-hosted runner for about five minutes while waiting for a BuildBuddy test
invocation that could not have been started by its upstream CI job. The upstream
`bazel-ci / Test & Build` jobs were cancelled before runner assignment, with
`runner_id: 0` and no steps. Both publishers eventually reported that no visual
artifacts had arrived. Together these two jobs occupied **11.1 runner-minutes**.
That is occupied time in this selected sample, not a measured reduction in Bazel
queue delay or proof of a fleet-wide capacity limit.

The [first publisher job](https://github.com/agentydragon/ducktape/actions/runs/41127418027/job/124309431690)
ran 21:01:16–21:06:47 UTC for the
[cancelled upstream CI run](https://github.com/agentydragon/ducktape/actions/runs/41127374991).
Its `bazel-ci / Test & Build` job
([124309284134](https://github.com/agentydragon/ducktape/actions/runs/41127374991/job/124309284134))
had `runner_id: 0`, an empty `steps` array, and was cancelled at 20:58:30.
The publisher printed `Superseded invocation did not finish before publication`
at 21:06:45 and found no artifacts. The
[second publisher job](https://github.com/agentydragon/ducktape/actions/runs/41127450452/job/124309542985)
ran 21:02:25–21:08:00 for
[CI run 41127407836](https://github.com/agentydragon/ducktape/actions/runs/41127407836),
whose Bazel job also had `runner_id: 0`, no steps, and a cancelled conclusion.
It reported the same five-minute wait and no artifacts. A third inspected
publisher waited the five minutes and found no artifacts, but its upstream Bazel
job **had** received a runner; it does not qualify for the proposed shortcut.
A fourth inspected publisher completed earlier and published nine visual targets
from a superseded run. These counterexamples are why cancellation alone is not
an absence proof.

The current `devinfra/pr_visuals/publisher.py` `publish_only` path waits up to
`INVOCATION_WAIT_SECONDS = 300` before listing artifacts for every superseded
run. The current `pr-visuals-e69024854338` source retains that wait. The sampled
job logs installed the older `pr-visuals-b9005af21dbb` wheel; the new wheel's
publication speedup is therefore not measured by their durations. The relevant
five-minute missing-invocation branch remains in current source. See
[`docs/publisher.md`](../../pr_visuals/docs/publisher.md#superseded-runs) for why
started, cancelled CI must still have its artifacts published.

## Queue clocks and scope

The wider [30 October latency report](https://agentydragon.github.io/ducktape/latency/#scope)
sampled 29 October 20:50–30 October 22:10 UTC. It found 332 direct-PR Bazel
attempts with a 138-second nearest-rank p90 job queue and completed core-workflow
occupancy peaking at 20 jobs. That core-only count excludes other workflows and
repositories and does not establish the account's Actions plan or runner limit.

For this follow-up, the GitHub Actions runs API returned all 952 Ducktape runs
created 30 October 20:50–22:10 UTC: 451 `Publish PR visuals`, 112 each `CI`,
`Pre-commit checks`, and `Nix wheel check`, and 37 `Nix Attic push`, plus other
workflows. Job pages were fetched for **all 93 runs created 20:58–21:05** and
the preceding 85 runs created 20:50–20:58. The seven-minute burst has 197 job
records, but only 52 have a positive `runner_id`; its 41 visual workflow runs
yielded 14 assigned jobs (five `announce`, nine `publish`), while 25 visual runs
were skipped. Thus the visual run count is not a runner-occupancy count.

Among the 13 CI runs created in the seven-minute burst, nine Bazel jobs received
a runner. Their job-created-to-start waits had a median of 108 seconds and a
maximum of 217 seconds. Four superseded PR Bazel jobs were cancelled after
assignment, consuming 21.6 runner-minutes; four devel-push Bazel jobs were
cancelled without assignment. Pre-commit and Nix wheel checks also waited for
runners, so this was not a Bazel-only queue. From jobs created since 20:50, the
positive-runner busy count at 21:01, 21:02, 21:03, 21:04, and 21:05 was 18, 18,
17, 18, and 19. These are **lower bounds**: jobs created before 20:50, other
owner repositories, and any running jobs absent from terminal pages are missing.
The sample cannot tell whether a slot was actually available when a given job
waited, nor whether GitHub's internal scheduler delayed assignment.

Workflow waiting is a separate clock. For example,
[CI run 41128241509](https://github.com/agentydragon/ducktape/actions/runs/41128241509)
was created at 21:04:43 but `run_started_at` was 21:11:10; its Bazel job was
created at 21:11:12 and assigned 25 seconds later. The roughly 6.5 minutes
before job creation cannot be called runner starvation. The previous head's CI
run ended at 21:04:59, but the API does not identify the reason for the later
workflow start. The reusable `bazel-ci.yml` group cancels superseded PR runs and
coalesces pushes. Top-level `ci.yml` deliberately has no workflow-level group
because release and image publication must finish. This evidence does not justify
changing those concurrency policies or cancelling publication jobs.

The authenticated owner-repository listing contained 140 repositories. Eight
other repositories updated since 1 September were queried for run arrivals in
20:30–22:10 UTC, including `gaffer-private`; none returned a run. This is a
bounded cross-repository check, **not** an all-account utilization census:
older repositories may still run schedules, and repository `updated_at` is not
an Actions activity timestamp. No account-wide runner capacity or pricing
claim follows from these observations.

## Recommended narrow change and proof

Before `publish_only` waits for a superseded invocation, query the upstream
`CI` run's **exact attempt** jobs through
`GET /repos/agentydragon/ducktape/actions/runs/{run_id}/attempts/{attempt}/jobs`.
Only when the Bazel job is conclusively `cancelled`, has no assigned runner
(`runner_id == 0`), and has no executed steps should it return after completing
the neutral visual check. No BuildBuddy invocation can have been launched by that
job. Keep the existing wait and artifact publication path for a job that started,
for incomplete or ambiguous API responses, and for every non-superseded run.
Paginate the exact-attempt response and match the Bazel job explicitly; absence
of a row is not absence of execution. A guard in `publish_only` is sufficient;
there is no need to alter CI or release workflow concurrency.

An implementation should test both the never-started cancellation and a
cancelled job with a runner or recorded steps, asserting that the latter still
waits and publishes available immutable artifacts. Also test unavailable or
ambiguous GitHub job data taking the existing wait path, and mainline
publication retaining its completion and pointer rules. Validate with Bazel
`//devinfra/pr_visuals:test_publisher`, then inspect fresh hosted publisher jobs
after the e690 pin for the no-runner case and the started-cancellation case.
The observed 11.1 runner-minutes is the upper bound for these two jobs' occupied
time; actual savings exclude the short preflight and check completion. Its
effect on required-check queue time remains unmeasured.

## Reproduction

The read-only collection used `gh api --paginate` with
`actions/runs?created=2026-10-30T20:50:00Z..2026-10-30T22:10:00Z&per_page=100`,
then `actions/runs/{run_id}/jobs?per_page=100` for the two bounded arrival
windows. The exact-attempt check used
`actions/runs/41127374991/attempts/1/jobs?per_page=100`; the Bazel row was
`cancelled`, `runner_id: 0`, and `steps: []`. Job logs were read through
`actions/jobs/{job_id}/logs`. The API reports timestamps at one-second
resolution. Raw logs and API responses were kept in task scratch, outside the
repository; no credentials or event payloads are in this note.
