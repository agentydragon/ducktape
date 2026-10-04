---
name: ci_latency
description: Diagnose where recent CI feedback time and compute go across PR types, GitHub runners, BuildBuddy provisioning, Bazel analysis/actions/tests and critical paths; prioritize practical improvements with reproducible evidence.
---

# CI latency analysis

Produce a **recent, representative, evidence-backed CI performance diagnosis**, not a
single score. Answer: from a new PR commit to **eligible for auto-merge under the current
rules**, how long does it take for different kinds of PRs? When do each status,
check result and diagnostic log become available to agents watching PR updates?
Where do runner time and remote compute go, what blocks the user-visible critical
path, and what changes offer the best payoff for the least cost/risk? Use reviewed
snapshots and their commit history on the canonical [`ci-latency-history` branch](https://github.com/agentydragon/ducktape/tree/ci-latency-history)
as _hypotheses and historical examples_, not conclusions about current CI. Keep
working reports and measurement snapshots out of `devel`; publish each reviewed
run as the latest snapshot on the history branch (below). `cihealth` covers
release/pin currency and failed CI more broadly. No Mimir metric, Shapley
calculation, or dashboard is required for a useful report; propose
instrumentation only when it closes a decision-relevant evidence gap.

## Investigate, don't just run a recipe

Treat the scripts and earlier history snapshots as **starting points**, not a
report generator or a fixed set of hypotheses. Decide what the operator actually needs to know;
follow surprising evidence across GitHub, BuildBuddy, Bazel and agent-visible
notifications. Inspect the live workflow/ruleset and representative invocations;
seek counterexamples and distinguish observation, inference and speculation. A
script may compute a correct aggregate but still omit the bottleneck or the
relevant PR class. Extend or bypass cookbook steps when needed, explaining data
coverage and why a different approach better answers the question.

Write the report yourself: name the main bottleneck(s), show why they matter to
auto-merge readiness or agent feedback, quantify what is known, identify what
remains unknown, and prioritize actionable changes. Compare with earlier entries
to explain what has improved or regressed, without blindly copying conclusions.
A generated JSON file, rendered HTML page or fresh commit **by itself is not a
completed analysis**. Do not publish a new history entry unless it contains a
substantive reviewed narrative and supporting evidence; when access is missing,
report the limitation rather than create a plausible-looking report.

## Collect and reproduce

Scripts are relative to this skill directory. Run from a named-branch Ducktape
worktree with the Nix devshell loaded. Dependencies: Bash, GNU coreutils, jq, gh;
`inspect.sh` additionally uses bbapi with `BUILDBUDDY_API_KEY`. Follow the repository's
network/sandbox rules. These scripts read APIs; they do not rerun/cancel jobs or
change scanning settings.

For a specific slowdown choose an explicit UTC window, initially 60–90 minutes.
For the general question of what _recent PRs_ experience, also sample a longer
period (e.g. several days to a week, split API windows below the search cap).
Stratify by changed paths/PR change class (including cdk8s vs previous cluster
configuration), size, fork/trust path, cold/warm cache where observable, and
completed/cancelled/superseded state. Sample fast, typical and slow PRs in each
relevant class rather than choosing only outliers. Record selection criteria and
sample counts; do not equate all runs with distinct PRs or reruns with new pushes.
Tie runs, attempts, head SHAs and check results to the _same_ PR head when
estimating feedback latency. Resolve the _current_ upstream ruleset/branch
requirements and auto-merge eligibility policy; branch/ruleset, required contexts,
review/code-owner approvals, merge conflicts, draft status, deployment gates and
synthetic merge/merge-queue behavior may matter. Do not confuse all visible checks
finishing, required checks succeeding, GitHub reporting mergeability, auto-merge
being enabled, and the eventual merge itself. If push timestamps or exact head
association cannot be recovered, say so and present a labeled proxy instead of
inventing a precise push-to-green percentile. Use a new output directory outside
the checkout. `collect.sh` refuses a filtered query with 1,000 or more results: split it into smaller windows instead of accepting
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

## Primary outcome: commit-to-actionable feedback

Measure clocks for **one PR head SHA at a time**, including superseded heads as
separate/censored observations:

- **Start:** new commit pushed to the PR head, or earliest observed head-change
  webhook if push timestamp is unavailable (label the proxy). For an existing PR,
  PR creation time is not a substitute for that head's commit time.
- **Ready to auto-merge:** earliest _observed_ time that all actual policy gates
  for this SHA are satisfied, including required checks, applicable review and
  branch/mergeability conditions. If gates are unknown or not met, report
  pending/blocked/censored with reason, not an invented timestamp. Separate CI
  gating duration from review/merge-queue/human delay. A merge timestamp alone
  is not readiness time. Re-read the rulesets and current eligibility behavior,
  rather than assuming the historical report's required contexts still apply.
- **Agent-visible signals:** independently record when a run/job starts, the
  check/status becomes queryable, a terminal result is queryable, the corresponding
  webhook is accepted/delivered into an agent's subscription inbox, and the agent
  actually receives or reads it (if observable). Record when the job log and
  BuildBuddy invocation/target logs become _accessible_ to the relevant agent,
  not merely when a job ends or a log URL is advertised. Measure the observable
  hop(s) only; webhook creation, delivery, inbox read and harness receipt are
  distinct clocks. Default PR subscriptions receive `check_run` completions and
  `status` events, but coverage depends on App installation, subject matching,
  and delivery health; see `agentplane/notification_service/docs/api.md`. Never
  infer delivery from GitHub's check timestamp alone.

Show a per-head timeline of these milestones and sample-size/p50/p90 for each
relevant interval where timestamps exist. Declare polling/sampling resolution,
clock skew and permission/availability limits. Do not publish raw webhook payloads,
logs or agent inbox contents into the public history. Use bounded status checks
when measuring availability; do not generate API request bursts or fabricate an
agent reception time from an inbox acceptance time.

## Diagnose end to end (do not stop at a queue chart)

For each representative slow/typical case, construct an annotated timeline from
PR head/push (if known) to required checks becoming terminal. Use the workflow
DAG and job attempts. Break time into **observed** mutually exclusive intervals
where possible; note unknown and overlapping intervals explicitly:

1. **GitHub orchestration and runners:** workflow pending/concurrency gates,
   queued job awaiting a runner, runner start/image setup, checkout/tool/Nix setup,
   dependency waits, retries, and non-Bazel steps. Show both queue-tail percentiles
   and the age of jobs still queued. Occupied runner-minutes explain capacity
   demand; critical-path elapsed time explains user feedback. They differ.
2. **BuildBuddy remote runner:** time from request to allocated remote runner,
   VM queue versus boot/provisioning versus repo/tool setup, execution and teardown.
   Use timestamped job and BuildBuddy events if exposed; log gaps are not proof of
   a particular phase. Look across related invocations for remote VM reuse and
   reasons for a cold start (pool/instance, isolation, concurrency, image changes,
   idle expiration), but do not claim reuse failed without evidence that reuse was
   possible. Avoid confusing a GitHub runner with a BuildBuddy VM or an RBE action.
3. **Bazel:** inner test/build invocation(s), startup, loading/analysis, execution,
   downloads/uploads, finalization. Inspect `command.profile.gz`, critical-path
   tool log, cache scorecard, target and execution records as needed (see the
   `buildbuddy_api` skill). Distinguish analysis time from executed actions;
   configured-target count or cache hit rate alone cannot diagnose analysis-cache
   reuse. Identify expensive actions/tests by label, frequency and _critical-path_
   contribution; include failures/retries and cache misses when relevant. Separate
   remote scheduling/queue time from worker provision, process execution and
   transfer. One slow execution does not necessarily extend the end-to-end path.
4. **Parallelism and bottlenecks:** draw the dependency/critical-path chain and
   compare its length with available work and executor saturation. Would more
   GitHub slots, remote VMs or Bazel parallelism actually shorten that chain?
   More concurrency can increase contention elsewhere. State which resource is
   saturated and what independent work was ready but blocked, or say it is unknown.

The report should include (1) a PR-class table with counts, commit-to-auto-merge-
readiness and agent-feedback p50/p90 (where actually observable), unfinished/
cancelled counts and definitions of the start/stop clocks; (2) a
phase-by-phase wall-clock timeline for a few linked cases with unknown intervals;
(3) runner occupancy and remote action cost in their own units; (4) the longest
confirmed critical-path contributors, by test/action/step; and (5) a ranked
impact/effort/confidence list of interventions with a validation plan. A ranked
list of total CPU users is _not_ a ranked list of feedback bottlenecks.
Compare distributions (p50/p90 and tails with sample sizes) and resource consumption
across PR classes; show direct job/invocation links and source SHA for cases. Do not
sum parallel step durations and call the result latency; label runner-minutes,
worker-seconds and wall-clock separately. When GitHub/BuildBuddy data cannot resolve
an interval, leave it unknown and identify the minimal additional trace needed.

## Recommend changes using 80/20 reasoning

Rank _concrete_ interventions by likely impact on required-check feedback for common
PRs, engineering effort, operational/security risk and confidence. Prefer low-risk
quick wins with directly observed recurring waste; distinguish fixes to the long
tail from fixes to a typical PR. Estimate possible savings as a range, not a
fabricated point value; test against matched PR classes and preserve required test
coverage. Examples to evaluate rather than assume: tighter validation selection,
shorter runner/VM startup or reuse, avoiding repeated Bazel analysis, test/action
critical-path improvements, and concurrency changes. Say what evidence would
falsify each recommendation and how to compare before/after. Put instrument-first
items behind actionable fixes unless missing evidence actually blocks the choice.

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

Include the observation window, source commit, sample/coverage limits, PR-class
feedback and agent-availability distributions, runner and remote-compute
breakdowns, representative critical-path timelines, current required checks,
unknown intervals, and ranked proposals. Include CodeQL/queued-Bazel overlaps
only when current evidence supports that diagnosis. Generate `evidence.json` with
`evidence.sh` as **supporting data**; review the derived evidence and explain its
significance before publishing the report and evidence together as the latest
history snapshot. Keep full API payloads, logs and profiles local unless a durable,
reviewed fixture needs them. Publish small relevant excerpts and direct
job/invocation URLs in the report.

Re-evaluate recommendations against current YAML and GitHub settings. Already-landed
changes leave the recommendation list. If ongoing monitoring is warranted,
propose only decision-relevant Mimir metrics with bounded labels, collection
ownership, freshness and alert conditions; distinguish proposals from metrics
confirmed live. A metric proposal is not a prerequisite for the diagnosis. Do
not change workflow/scanning policy during an analysis-only request. If a mitigation is authorized later, compare matched workload
windows and verify latest-head PR feedback before calling it effective.

The package's Bazel tests execute these jq recipes against captured Actions metadata,
covering queue/runtime arithmetic and CodeQL contention. Package and tests are under
`//devinfra/ci/skills/ci_latency/...`; CI validates them on the PR.

## Durable history and test-cost attribution

`agentydragon/ducktape`'s canonical [`ci-latency-history` branch](https://github.com/agentydragon/ducktape/tree/ci-latency-history)
holds the latest reviewed report and its evidence. Its root contains `README.md`,
`report.md`, `evidence.json`, `manifest.json`, a standalone `index.html`, and
optionally `attribution.json`. The README provides durable navigation; the HTML
renders the current report. Keep exactly one snapshot at the root, with no dated
run directories or generated archive index. Git commit history preserves earlier
snapshots. Each new report replaces only the snapshot files; preserve the README
and any unrelated files. If a new run has no attribution, remove the old
`attribution.json` so it cannot be mistaken for current evidence. The history
branch is not a mirror of `devel`. Its earlier commits preserve the explicitly
labeled _historical_ copy of the former maintained report, not a fresh cdk8s
comparison. Read it as a baseline for **methodology**, not proof of current
performance.

For each run, fetch the canonical history branch and create a separate worktree
at its tip. Compare previous snapshots from earlier commits, along with their
windows, workload mixes and coverage. Collect fresh evidence and read the current
workflow/path filters and BuildBuddy profiles. Pin the actual inspected devel SHA
(do not use a merge SHA, PR head or the history branch's HEAD). Publish into the
history worktree root:

```bash
SKILL=devinfra/ci/skills/ci_latency/scripts
python3 "$SKILL/publish.py" --source "$DEVEL_SHA" \
  --window-start "$SINCE" --window-end "$UNTIL" \
  --report "$REPORT" --evidence "$EVIDENCE" --out "$HISTORY"
# Add --attribution "$ATTRIBUTION" only when measured attribution is available.
```

`publish.py` only packages an **already written and reviewed** report and a safe
HTML rendering of its text; it does not investigate CI, rank fixes, or validate
conclusions. Review the narrative before publishing. The publisher replaces only
the root snapshot files and leaves `README.md` and unrelated files intact. Do not
mistake successful script execution for completion of the skill. Open a PR
targeting the canonical `ci-latency-history` branch. Before submitting, fetch the
branch again and confirm the PR includes its latest tip. If another run landed
first, compare the observation windows and do not let an older run replace a
newer snapshot; rebase or rebuild on that tip and republish when the new run is
later. Never reset, amend, or force-push history. Review the report and diff for
credentials, identities, raw logs, payloads and personal data before publishing.
History updates go through a PR to `ci-latency-history`; changes to this skill or
its scripts go through a normal source PR. GitHub does not serve branch HTML as a hosted page:
download/open the artifact locally, or use the raw URL; don't imply Pages is
deployed.

### Quantifying who triggered work

Rank directly measured costs and test-specific **avoidable** costs first:
removing one trigger may save nothing if another trigger still selects the same
test. The name for one optional allocation of shared costs across overlapping
causes is the **Shapley value**. `scripts/attribution.py` computes the exact Shapley value for a restricted
but useful counterfactual: each **measured additive cost unit** is incurred if
_any_ member of its independently verified `triggers` set is present. For that
OR game, the exact value is `seconds / number of triggers`, without enumerating
coalitions. A provisioning unit triggered by two tests splits its measured setup
cost equally; a test unit triggered by two changed-path groups splits its measured
execution cost equally. **Keep the player definition consistent** (either test
labels or disjoint changed-path groups) across all units in one calculation.
Never infer triggers solely from glob names; resolve actual changed paths against
filters, Bazel test selection and invocation evidence. When overlapping globs
match the _same_ path group, deduplicate it. If provisioning is triggered by
non-test build work, include that cause as a player rather than charging tests.

Reviewed JSON input example (one resource at a time):

```json
{
  "resource": "runner-seconds",
  "runs": [
    {
      "id": "github-job-id/attempt",
      "source_commit": "example-source-sha",
      "units": [
        { "name": "setup", "kind": "provision", "seconds": 90, "triggers": ["test-a", "test-b"] },
        { "name": "test-a", "kind": "test", "seconds": 30, "triggers": ["test-a"] }
      ]
    }
  ]
}
```

Use actual full SHA in place of the illustrative source field. Run
`python3 scripts/attribution.py reviewed.json attribution.json`; inspect that
sum of attributed seconds equals measured seconds. Include invocation/job URLs,
selection and timing methodology in `report.md`. A runner step measures runner
occupancy; an RBE action measures remote worker execution. **Never add these two
resources**, or sum overlapping test wall times and call that feedback latency.
Do not allocate an entire job to tests when provisioning, analysis, download,
queueing, other build actions, or postprocessing cannot be separately timed.
Report unmeasured time as unallocated. Shapley shares are a counterfactual model
under stated assumptions, **not** causal evidence that deleting a test will save
that much CI wall time. For feedback latency, compare matched runs/critical paths,
parallelism and runner contention separately; rerun with and without a proposed
filter to validate savings.

To assess cdk8s: compare matched pre/post-deployment devel/PR change classes,
actual selected test targets and globs, provisioning/analysis/execution/queue
breakdowns, runner-minutes and required-check latency distributions. Track
sample sizes, cache state and canceled/unfinished runs; do not attribute a
changed p90 to cdk8s without checking workload composition and other changes.
If trigger/timing data is unavailable, publish the other measurements with
`attribution: not collected` instead of manufacturing percentages.
