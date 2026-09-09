---
name: ci_latency
description: Diagnose slow PR feedback by separating GitHub Actions runner queues, CodeQL load, workflow dependencies, and BuildBuddy execution. Refresh the maintained CI latency report with reproducible evidence and monitoring proposals.
---

# CI latency analysis

Update `devinfra/ci/debug/ci_queue_saturation.md` in the Ducktape checkout; replace its
current-state conclusions and evidence rather than appending another dated report.
Read that report for hypotheses, then verify them. This skill diagnoses latency;
`cihealth` covers release/pin currency and failed CI more broadly.

## Collect and reproduce

Scripts are relative to this skill directory. Run from a named-branch Ducktape
worktree with the Nix devshell loaded. Dependencies: Bash, GNU coreutils, jq, gh;
`inspect.sh` additionally uses bbapi with `BUILDBUDDY_API_KEY`. Follow the repository's
network/sandbox rules. These scripts read APIs; they do not rerun/cancel jobs or
change scanning settings.

Choose an explicit UTC window around the symptom, initially 60–90 minutes. Use a
new output directory outside the checkout. `collect.sh` refuses a filtered query
with 1,000 or more results: split it into smaller windows instead of accepting
GitHub's search cap. It pages runs, jobs (all attempts), open PRs and head checks,
and includes older unfinished runs separately. Each collection is a sweep, not an
atomic snapshot; preserve its start/end timestamps.

```bash
bash scripts/collect.sh agentydragon/ducktape "$SINCE" "$UNTIL" "$OUT"
bash scripts/evidence.sh "$OUT" > "$OUT/evidence.json"
```

`evidence.sh` runs the same jq recipes used for the report. `summarize.jq` produces
queue/runtime percentiles, occupied time and step totals. `contention.jq` attributes
CodeQL language cost and samples one-minute occupancy alongside queued Bazel jobs.
`checks.jq` counts visible unfinished checks on current PR heads. For a before/after
comparison, run the jq recipes again with narrower `--arg since` / `--arg until`
values on the same snapshot; collect older runs too if a complete interval census
is needed. Nearest-rank percentiles, seconds throughout.

For a slow job, take its ID from `html_url`, retrieve its log, and identify the
**inner** Bazel test/build invocation before asking BuildBuddy for a critical path.
Do not choose an arbitrary recent invocation: developer queries and tiny publisher
builds bias the apparent speed of PR CI.

```bash
gh api --allow-escape-sequences "repos/$REPO/actions/jobs/$JOB/logs" > "$OUT/job.log"
# Inspect Invocation ID / elapsed-time lines, then select the test/build invocation.
bash scripts/inspect.sh "$REPO" "$JOB" "$INVOCATION" "$OUT/inspect"
# Deeper phase diagnosis, only if needed:
bbapi tool-log download "$INVOCATION" command.profile.gz -o "$OUT/command.profile.gz"
bbapi execution "$INVOCATION" --json > "$OUT/executions.json"
```

`inspect.sh` also records generated workflows, CodeQL default setup and devel's
rulesets. An inaccessible endpoint or timeout fails visibly; record the visibility
limit and retain successful independent evidence. GitHub's legacy branch-protection
endpoint can return 404 while rulesets still require checks.

## Interpret correctly

- Discover CodeQL by `dynamic/github-code-scanning/codeql` or workflow ID, **not**
  display name: default-setup runs are often named `PR #...` / `Push on devel`, and
  no CodeQL YAML appears in `.github/workflows`.
- `run_started_at` is often equal to run creation even while jobs queue. Use
  **job** creation to assigned-runner start; separate workflow pending/concurrency,
  dependency/environment gates, and GitHub scheduling. Inspect the DAG before
  labeling all creation-to-start delay runner starvation.
- Skipped/cancelled jobs can have synthetic timestamps without a runner. Require
  a positive `runner_id` before counting occupancy or runtime. Exclude unfinished
  jobs from completed-duration percentiles; report their counts/ages separately.
  Never turn missing timestamps into zero durations.
- Occupancy uses `[started_at, completed_at)` and clips to the measurement window.
  Scripts exclude still-running jobs, including stale “in_progress” records months
  old: their occupancy is a **completed-job lower bound**, not live utilization.
  Runs completed before collection but created before the window can be absent.
- A peak near the published account concurrency limit plus queued independent
  jobs supports contention. It does not prove the account plan or scheduler
  priority. Check other owner repositories and GitHub Status if attribution is
  incomplete. Public `plan: null` does not identify the plan.
- A runner waiting on `bb remote` still consumes a GitHub slot. Split outer runner
  startup, Bazel analysis, action queue and action execution. Profile configured
  target counts do not alone prove analysis-cache recomputation.
- Keep required-gate feedback distinct from every visible check becoming terminal.
  Missing head checks, reruns, synthetic merge SHAs and fork trust paths need explicit
  treatment. Check ages start at check creation, not necessarily the latest push.
  Cancellation is terminal but is not a red/green verdict; missing checks are not green.

## Refresh the artifact

Include the observation window, source commit, sample/coverage limits, workload
shares, concrete queued-Bazel/occupied-CodeQL overlaps, slow execution evidence,
current required checks, and ranked proposals. Update `ci_latency_evidence.json`
from `evidence.sh`; review derived evidence before committing. Keep full API payloads,
logs and profiles local unless a durable, reviewed fixture needs them. Publish
small relevant excerpts and direct job/invocation URLs in the report.

Re-evaluate recommendations against current YAML and GitHub settings. Already-landed
changes leave the recommendation list. Propose Mimir metric definitions, bounded
labels, collection ownership, freshness and alert conditions; distinguish proposals
from metrics confirmed live. Do not change workflow/scanning policy during an
analysis-only request. If a mitigation is authorized later, compare matched workload
windows and verify latest-head PR feedback before calling it effective.

The package's Bazel tests execute these jq recipes against captured Actions metadata,
covering queue/runtime arithmetic and CodeQL contention. Package and tests are under
`//devinfra/ci/skills/ci_latency/...`; CI validates them on the PR.
