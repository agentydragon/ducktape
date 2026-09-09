# Current CI latency analysis

Measured **2026-09-09, 20:00–21:15 UTC**; API sweep 21:13:18–21:16:00 UTC.
Source inspected: `b5d4e6f918b0d18bb65a48c94601a1fa5dc56f01`.
This is the maintained report. Refresh it using the
[ci_latency skill](../skills/ci_latency/SKILL.md) and its packaged scripts.
[Machine-readable evidence](ci_latency_evidence.json) contains the aggregates,
job URLs, contention timeline and current-head check counts.

## Finding

**GitHub runner contention is a material cause of slow PR feedback. CodeQL is the
largest measured consumer and is occupying slots while Bazel CI waits.** There is
also a separate Bazel execution tail; removing runner contention would not eliminate
all slow results.

At **20:44 UTC**, 20 jobs occupied GitHub runners, **12 were CodeQL**, and these two
independent Bazel jobs were waiting:

- [run 34402669951](https://github.com/agentydragon/ducktape/actions/runs/34402669951/job/102638082623)
- [run 34402670004](https://github.com/agentydragon/ducktape/actions/runs/34402670004/job/102638083065)

The second waited **177 seconds**, then passed in **89 seconds**. Another
[Bazel job](https://github.com/agentydragon/ducktape/actions/runs/34403608775/job/102641166641)
waited **298 seconds**, then passed in **89 seconds**. Later starts in the snapshot
waited **525** and **503 seconds**; their execution was still unfinished when sampled.

Observed completed-job overlap peaks at **20**, with 23 one-minute samples at 20.
That matches GitHub's published Free standard-runner limit, but the account's plan
and any custom limit were **not exposed** by the queried account metadata. This is
evidence of a saturated effective pool, not proof of the subscription tier or of a
scheduler policy that favors CodeQL. GitHub Status reported all systems operational
with no active incidents during diagnosis.

## Where the runner time goes

This is a bounded busy-period sample: 457 runs created in the window, plus five
older unfinished runs discovered separately; 1,024 job records across all attempts.
Skipped/unassigned jobs are excluded from timing. Completed assigned jobs occupy
**1,057.7 runner-minutes** inside the 75-minute window, a lower-bound average of
**14.1 runners**. Still-running jobs and completed pre-window-created runs missing
from discovery make this a lower bound, not a complete account utilization census.

| Workload                          | Completed occupied minutes | Share | Assigned job queue p50 / p90 | Completed runtime p50 / p90 |
| --------------------------------- | -------------------------: | ----: | ---------------------------: | --------------------------: |
| CodeQL                            |                      492.4 | 46.6% |                   58s / 635s |                 137s / 476s |
| CI, including publishing children |                      166.3 | 15.7% |                   67s / 374s |                  89s / 503s |
| Pre-commit                        |                      135.7 | 12.8% |                   32s / 351s |                 148s / 207s |
| PR visual publication             |                       79.0 |  7.5% |                   18s / 496s |                 115s / 189s |
| Nix wheel check                   |                       72.3 |  6.8% |                   69s / 308s |                 129s / 149s |
| Gazelle diff                      |                       29.2 |  2.8% |                   10s / 382s |                   36s / 40s |
| Other workflows                   |                       82.8 |  7.8% |                 See evidence |                See evidence |

Do not read the aggregate CI runtime as Bazel duration: release planners and publishing
children are in that group. The 37 completed jobs named `Test & Build`, including
trusted-fork CI, have median **201s**, nearest-rank p90 **536s**, maximum **897s**.

The onset is visible within the sample. For jobs created **20:00–20:40**, pre-commit,
Gazelle and CodeQL all had queue p90 **3s**. Across the full window their queue p90s
rose to **351s**, **382s**, and **635s**, while short Gazelle execution stayed around
36 seconds. This supports burst contention rather than a universal slowdown in the
underlying tools. It is not a matched historical baseline.

## CodeQL hypothesis: supported

CodeQL is **GitHub default setup**, generated as
`dynamic/github-code-scanning/codeql`, not a checked-in workflow. Runs are displayed
as `PR #...` and `Push on devel`, so searches for workflow name `CodeQL` alone miss
most of the load. The settings API reports standard runners, the default query suite
and a weekly schedule; observed PR/push scans are additional to that schedule.

There are **45 CodeQL runs created in the window**. Each normal scan fans out to six
analysis jobs: Actions, C/C++, Go, JavaScript/TypeScript, Python and Rust. API settings
also list JavaScript and TypeScript aliases; these are not eight observed matrix jobs.
Completed jobs account for:

| Language              | Completed jobs | Runner-minutes |
| --------------------- | -------------: | -------------: |
| Rust                  |             26 |          193.9 |
| Python                |             26 |          112.6 |
| Go                    |             29 |           77.8 |
| JavaScript/TypeScript |             29 |           48.5 |
| C/C++                 |             28 |           32.7 |
| Actions               |             29 |           27.0 |

Rust and Python contribute **62.2% of completed CodeQL runtime**. The `Perform CodeQL
Analysis` step accounts for **361.5 minutes** across 167 completed jobs, versus
66.1 minutes initializing CodeQL. This is substantive scanner work, not predominantly
checkout overhead. A representative
[Rust job](https://github.com/agentydragon/ducktape/actions/runs/34398566101/job/102624442274)
ran 377 seconds and its log shows query execution and result interpretation.

Default setup does not provide a repository YAML concurrency knob to edit. A proposal
to change matrix parallelism must include the switch to advanced setup and preservation
of scan coverage. `max-parallel: 2` alone caps one run, **not** concurrent runs across PRs.

## Required feedback versus all visible checks

The current devel ruleset requires only **`Pre-commit checks`** and
**`bazel-ci / Test & Build`**. CodeQL is not a required status check in that ruleset.
The legacy branch-protection endpoint returns 404; querying
`repos/agentydragon/ducktape/rules/branches/devel` reveals the actual requirements.

The open-PR sweep found **92 PRs**. Among their latest head checks, 4/87 pre-commit
checks and 8/76 Bazel checks were unfinished. Each CodeQL language had 9–10 unfinished
checks. These counts are checks, not disjoint PR counts; some heads have missing
checks or older/different workflow names. They do not establish that every other PR
is mergeable. Head-check collection was paginated, including beyond the first page.

A UI waiting for every visible check includes CodeQL and visual publication even after
the two required checks finish. Track both outcomes. Exact latest-push-to-terminal
latency is not reconstructed here: head SHA/check creation is insufficient to recover
every push timestamp, rerun and synthetic-merge association. Future collection should
persist those events rather than use PR `updated_at` as a push clock.

## Separate Bazel execution tail

[Bazel job 102629094609](https://github.com/agentydragon/ducktape/actions/runs/34399958227/job/102629094609)
waited only **4s** for GitHub, then ran **897s** and failed. Its log records:

- `Waiting for available remote runner...` at 20:16:22, followed by streamed Bazel
  startup output at 20:17:04. This ~42s interval includes remote startup/log delivery;
  it is not a precise RBE scheduling measurement.
- [Inner test invocation 845daf8b-5d45-5adf-bfed-7bc91aced902](https://app.buildbuddy.io/invocation/845daf8b-5d45-5adf-bfed-7bc91aced902):
  **804.123s elapsed**, **362.15s critical path**.
- The critical path includes **356.52s** in
  `//haku/console/channels/matrix:test_fullstack_e2e`. Its remote breakdown attributes
  99.24% to process execution and 0.02% to queueing. This path is test work, not remote
  action starvation.
- `//props/db/sync:test_example_generation` times out at **60.3s**. 227 tests executed;
  the final summary reports 495 passed and one failed. Diagnose this timeout separately;
  it is not evidence for increasing the timeout.
- Invocation cache statistics: 6,601 action-cache hits / 3,084 misses and **27.8 GB
  transferred downloads**. These API counters and the Bazel console's process/cache
  counts have different scopes; do not combine them into one hit-rate calculation.

This single slow invocation disproves the old blanket claim that Bazel is uniformly
fast. It does not establish a fleet-wide cache or analysis regression. Fetch profiles
and target history if optimizing this lane; configured-target counts alone do not prove
analysis-cache eviction.

## Ranked proposals

1. **Control CodeQL burst demand.** Move default setup to declarative advanced setup
   if custom scheduling is desired. Preserve per-language coverage and SARIF publishing;
   cancel superseded scans per PR, then measure a modest per-run matrix cap. If that
   still leaves many concurrent PR scans, use a dedicated runner pool or an explicit
   scan scheduling policy. A shared concurrency group can coalesce away pending PR
   scans, so it is not a coverage-preserving global semaphore. Restricting PR scans or
   moving them to scheduled/devel scans trades away pre-merge security feedback and
   needs a deliberate policy decision. No scan settings were changed in this analysis.
2. **Cancel superseded pre-commit work per PR.** Unlike Gazelle and Bazel, the current
   pre-commit workflow has no concurrency group. Also reduce its fixed setup cost:
   median Nix setup is 62s versus 37s actually running pre-commit. This is the second
   largest independent non-CodeQL workload. Keep one authoritative check for the latest head.
3. **Reduce the visual workflow's fixed cost.** It starts separate announce/publish
   jobs from CI events, including completion/cancellation paths. The announcement uses
   Nix and `bb run` to post check metadata. Evaluate a lightweight trusted entrypoint,
   while preserving terminal-check handling. This lane consumes 7.5% of measured time.
4. **Profile the actual slow Bazel tests and cold work.** Begin with the Matrix test,
   the separate props timeout, and this invocation's profile. GitHub capacity cannot
   shorten an already-running six-minute test. Preserve full sweep and RBE semantics.
5. **Consider more/isolated GitHub capacity after measuring demand.** Twenty observed
   slots is an effective ceiling in this sample, not a verified billing entitlement.
   Compare queue and required-check feedback before/after any capacity or scheduling
   change under similar PR arrival rates. Do not promise a proportional speedup.

The 2026-08-26 report's principal recommendation—dynamic publish matrices—is already
implemented in `plan_releases.py`, `plan_image_pushes.py`, and their called workflows.
Publishing uses separate concurrency policies that let in-flight releases finish.
The former “95 jobs per merge,” “99% no-op,” “Bazel is fast” and pricing conclusions
are superseded; they are not current measurements. Git history retains the old report.

## Proposed Mimir observability

Repository configuration currently owns GitHub **API quota** exporters under
`cluster/k8s/github-exporter`; no Actions queue collector was found there. This was a
source inspection, not a live Mimir series census. Keep quota and runner capacity
separate. Proposed owner: a dedicated Actions observer under `cluster/exporters`,
GitOps-managed, scraped by ServiceMonitor → Alloy → existing Mimir remote write.
Use an Actions/Checks/Pull requests read-only GitHub App installation and a persisted
collection cursor, separate from the personal GraphQL bucket.

| Proposed metric                                          | Type / definition                                                                                                         | Bounded labels                                |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `github_actions_jobs`                                    | Gauge, outstanding jobs by state; separate queued, in-progress, waiting                                                   | repo, workflow, job_class, state, runner_pool |
| `github_actions_oldest_queued_seconds`                   | Gauge, maximum queued age; include unassigned jobs                                                                        | repo, workflow, runner_pool                   |
| `github_actions_job_queue_seconds`                       | Histogram, job creation → assigned start, once per job attempt; name this observed scheduling delay, separate known gates | repo, workflow, job_class, runner_pool        |
| `github_actions_job_run_seconds`                         | Histogram, assigned start → completion, once per attempt                                                                  | repo, workflow, job_class, conclusion         |
| `github_actions_runner_busy_seconds_total`               | Counter, integrated assigned intervals; supports workload shares                                                          | repo, workflow, runner_pool                   |
| `github_actions_pr_feedback_seconds`                     | Histogram, observed head push → required-gate verdict and → all-checks-terminal as separate scopes                        | repo, scope, outcome                          |
| `github_actions_pr_heads_waiting`                        | Gauge, latest heads awaiting checks, with missing checks counted separately                                               | repo, scope, state                            |
| `github_actions_superseded_jobs_total`                   | Counter, cancelled superseded attempts, with occupied time tracked separately                                             | repo, workflow                                |
| `github_actions_observer_last_success_timestamp_seconds` | Gauge; alert on stale data independently of scrape health                                                                 | repo                                          |
| `github_actions_observer_api_requests_total`             | Counter, API usage/errors; emit rate remaining/reset gauges too                                                           | route_class, status_class                     |
| `buildbuddy_ci_phase_seconds`                            | Histogram from linked invocations/profiles: runner startup, analysis, critical path, remote queue/process                 | repo, phase                                   |

Use stable workflow paths and normalized job classes (`codeql_rust`, `bazel_test_build`,
`pre_commit`), not `PR #123`, SHA, run/job ID, target label or runner instance as labels.
Keep IDs/SHAs in durable event storage or logs for drill-down. Deduplicate webhooks by
job ID/attempt and feedback by head SHA; do not re-observe a finished job on every poll.
A cancelled old head is superseded, not a successful verdict for its replacement.
Ruleset/App identity defines required checks; classify missing or ambiguous checks
explicitly. Detect stale job records before treating them as occupied runners.

Prefer `workflow_job` / `workflow_run` / PR event ingestion with REST reconciliation
and a 60–120s freshness target; a polling prototype should revisit active runs plus a
small recent overlap, cache finished jobs, and budget API calls. One full diagnostic
sweep here needed hundreds of job/check requests; do not repeat it every minute.
An observer inside GitHub Actions would itself wait for the resource being monitored.

Initial warning candidates, to tune after a baseline:

- Oldest queued required job >300s for 5m, with fresh observer data.
- Queue p90 >180s over 30m, only with enough started-job observations; pair this with
  the oldest-age gauge because unfinished queues do not appear in a histogram.
- Required heads waiting >15m, split missing/running/queued states.
- Observer stale >5m, even when the metrics endpoint still returns HTTP 200.
- CodeQL runner share above 40% **and** Bazel queue age elevated: diagnostic dashboard
  correlation, not an alert that security scanning by itself is an incident.

## Reproduction and limits

Run the skill's `collect.sh`, `evidence.sh` and `inspect.sh`; arguments for this sample:

```bash
bash scripts/collect.sh agentydragon/ducktape \
  2026-09-09T20:00:00Z 2026-09-09T21:15:00Z /tmp/ci_latency-new
bash scripts/evidence.sh /tmp/ci_latency-new > /tmp/ci_latency-new/evidence.json
bash scripts/inspect.sh agentydragon/ducktape 102629094609 \
  845daf8b-5d45-5adf-bfed-7bc91aced902 /tmp/ci_latency-new/inspect
```

A refresh should choose a new current window, not reuse these timestamps. The original
raw sweep is local at `/tmp/ci-latency-20260909`; aggregates and a small captured test
fixture are committed. Requerying historical runs can change conclusions for jobs
unfinished during this sweep. Timing percentiles exclude unassigned/unfinished samples;
creation-to-start can include gates, so the strongest starvation evidence is overlapping
assigned jobs while independent Bazel jobs queue. No account-wide competing-repository
census or controlled mitigation experiment was performed.

Reference semantics, checked during this analysis:
[GitHub concurrency limits](https://docs.github.com/en/actions/reference/limits),
[concurrency groups](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency),
[CodeQL setup types](https://docs.github.com/en/code-security/concepts/code-scanning/setup-types),
[CodeQL analysis duration](https://docs.github.com/en/code-security/reference/code-scanning/troubleshoot-analysis-errors/analysis-takes-too-long).
