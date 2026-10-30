---
name: ci_reliability
description: Quantify CI flakiness and recurring failures, trace their mechanisms through test internals, fixtures and infrastructure, and test coverage-preserving repairs. Publish evidence-backed reliability reports; use ci_latency for feedback-time analysis.
---

# CI reliability and flakiness

Produce a recent reliability diagnosis with failure frequencies, explicit
denominators, concrete culprits, and prioritized fixes. A red build is not by itself
a flaky test. Distinguish observed intermittent outcomes, sustained regressions,
infrastructure failures, and source patterns that are only hypotheses.

Test implementations and architecture are in scope: synchronization, fixtures,
setup/teardown, shared resources, target structure, parallelism and dependencies.
Follow important failures to whatever depth produces a useful repair; failure
rankings, log signatures and successful retries are starting points, not diagnoses.

## Continue the previous investigation

Fetch the canonical [ci-reports branch](https://github.com/agentydragon/ducktape/tree/ci-reports)
into an isolated worktree. Read the complete `reliability/index.html`, its
`manifest.json`, and relevant `evidence.json` records before selecting a new sample.
If the first report is still under review, inspect its PR and label it unmerged.
Use earlier commits when needed to recover prior diagnoses and experiments.

Carry forward unresolved failures and interventions whose effects remain unmeasured.
Verify proposed fixes against current source and merged PRs before recommending them
again. Distinguish a landed candidate fix from measured reduction in recurrence.
Keep prior measurements labeled with their original window and source revision;
explain which findings were confirmed, superseded, resolved, or deferred.

## Build a defensible sample

Choose an explicit UTC window, normally seven recent days initially, and pin the
inspected devel SHA. Record repository, branches, workflow filters, collection time,
and missing coverage. Keep mainline invocations, PR runs, and selected retry examples
as separate populations. Expand the window when low counts limit conclusions.

Use the `buildbuddy_api` skill for API/log/artifact recipes and the current `bbapi`
CLI help for filters. GitHub Actions supplies workflow attempts, jobs, and tested
revisions; BuildBuddy supplies invocation, target, shard, and attempt outcomes.

- Follow pagination on GitHub runs, jobs, and check runs and on BuildBuddy history.
  A `--count` cap is a collection limit, not proof that a time window is complete.
  Record earliest/latest timestamps, truncation, and unavailable pages/artifacts.
- Resolve runner/workflow invocations to child Bazel invocations. Deduplicate IDs
  so a parent and its child do not count as two independent builds.
- Reconcile success, test failure, build failure, cancellation, and incomplete
  states. A missing test result after a build/setup failure is not a failed test.
- Preserve test target, configuration, shard, run, attempt, cache status, and exact
  tested commit where available. Cached passes do not represent fresh execution.
- Use invocation details or checkout/job evidence for the actual tested full SHA.
  A PR head and its synthetic merge commit are not interchangeable.

### Query BuildBuddy's Flakes APIs

Use `GetTargetStats` to discover candidates and `GetTargetFlakeSamples` to inspect
the important candidates, alongside ordinary failure history and non-green merges.
The corresponding CLI commands are:

```bash
bbapi target stats --repo https://github.com/agentydragon/ducktape --json > target-stats.json
bbapi target flakes "$TARGET_LABEL" --repo https://github.com/agentydragon/ducktape --json > flake-samples.json
```

Select the canonical repository explicitly. Local `file://` checkout URLs and
forks have separate histories; automatic repo detection or a remembered UI
selection can query the wrong one. Verify the organization, recorded repository,
branch and time filters before interpreting an empty response. A missing branch
name in recorded invocations cannot match `devel`. These APIs default to seven
days when no window is supplied; use supported CLI filters or the RPC's
`started_after`/`started_before` fields for a different window, and record it.
Follow sample `nextPageToken` values; one page is not a complete sample set.

Keep Bazel-reported `FLAKY` verdicts separate from BuildBuddy's `likely_flaky_runs`
heuristic: an isolated failure/timeout bracketed by passing runs of the target.
That heuristic does not require the same commit or configuration and excludes
cached rows. It can miss repeated failures and does not establish a cause.
Check whether CI target tracking includes the relevant invocations (`ROLE=CI`
and target tracking enabled); this repository deliberately excludes PR invocations
from that mainline tracker, so reconcile the results with GitHub PR checks and
exact target histories. Report API candidates separately from independently
verified recoveries. Unavailable historical logs limit diagnosis; they do not
erase recorded outcomes.

### Find PRs merged without green checks

Enumerate PRs merged during the window, including those opened earlier, and find
ones merged while required checks were failing, timed out, cancelled or still
pending. In this repository, the owner sometimes force-merges because of flakes:
these are priority discovery leads even when the PR or mainline is now green.

Reconstruct check state **at merge time** from the PR's merged head, merge
timestamp, associated check/status and workflow-attempt history, and the actual
tested revisions. A later passing rerun must not erase a failure or pending check
at merge. Keep synthetic merge runs and PR heads correctly associated; record
missing historical state as unknown. Current rollups alone cannot establish what
was green when the PR merged. Call a merge an explicit bypass/force-merge only
when timeline/audit evidence or owner commentary supports it.

Inspect the failed targets and logs, bypass explanations and PR discussion for
each candidate. Correlate with same-source fresh recoveries and recurring failure
signatures, then investigate the mechanism. A non-green merge is evidence of
operator friction and a strong flake lead, not proof that the failure was flaky
rather than a source regression, infrastructure fault or an unrelated gate.
Report this candidate population separately from verified flakes and recoveries,
with PR links, merge-time check states and the diagnosis or visibility limit.

### Verify cache exposure and physical executions

Do not infer an uncached test run from target-history `startTime` versus invocation
creation time. A cached result can be indexed with a start time after the current
invocation was created. Query the exact target in each child Bazel invocation:

```bash
bbapi target "$INVOCATION_ID" --label "$TARGET_LABEL" --json > target.json
bbapi execution "$INVOCATION_ID" --json > executions.json
```

In the target response, inspect
`targetGroups[].targets[].testSummary.totalRunCount` and
`totalNumCached`, along with `testResultEvents[].testResult.executionInfo.strategy`.
When the summary is present, report cached runs and non-cached runs separately;
the latter is `totalRunCount - totalNumCached`. A missing target, summary, or
cache field is unknown unless another response field confirms the cache result.
The protobuf JSON encoder omits scalar `totalNumCached` when it has the default
value of zero. For example, when `totalRunCount` is present and every matching
test-result event has strategy `remote`, record the omitted cache count as zero
and cite the strategy as confirmation; do not mark it unknown solely because
the zero-valued field is absent. Conversely, do not silently coerce an absent
field to zero when result-event coverage or strategy is incomplete. Never infer
cache state from timestamps.

Keep three counts distinct: target verdicts, cached versus non-cached test runs,
and unique physical RBE execution IDs. A non-cached run is not automatically a
unique physical execution: action sharing or merging can link work across
invocations. Match exact target labels and deduplicate the execution IDs returned
by `bbapi execution`; leave physical exposure unknown when no matching execution
row exists. A cached pass is a recorded result, not a fresh execution or a same-
source recovery. Preserve the invocation ID, target, tested SHA, configuration,
cache fields, and execution IDs with the evidence.

## Quantify without overstating

For each ranked target or failure signature, show counts, exposure, time range,
first/last occurrence, affected commits, and representative links. State the unit:

| Measure                      | Numerator / denominator                                                                                            |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Invocation failure frequency | Failed completed invocations / all completed invocations in the stated population                                  |
| Target failure frequency     | Failed target outcomes / observed completed outcomes for that target and configuration; distinguish cached results |
| Retry recovery fraction      | Verified recoveries / comparable failed runs that were retried and whose results were available                    |

Do not divide target failures by all repository builds: target selection varies.
Do not present recovery among selected retries as the repository's flake rate.
Co-failing targets overlap; their counts need not sum to failed invocations.
Keep raw shard/attempt failures separate from the final target verdict so automatic
retries cannot silently hide instability or multiply the denominator.

A verified same-source recovery needs a failure and later explicit `PASSED` result
for the same target/configuration on the same full tested SHA. A successful workflow
alone is insufficient: the target may have been skipped, filtered, or served from
cache. Label cached recoveries separately. Check changed toolchains, executor images,
external dependencies, credentials, and environment before calling the cause random.
Same-SHA recovery proves inconsistent outcomes under observed conditions, not an
intrinsically flaky test or a specific root cause.

Group log signatures to distinguish repeated symptoms from independent defects.
Use counts and sample limits prominently; where confidence intervals are useful,
state their assumptions rather than treating correlated retries as independent.
For post-fix comparisons, match target/configuration and execution exposure across
windows. Zero failures in a small sample is not proof of elimination.

## Diagnose the important failures

Inspect representative failing logs and at least one comparable pass for each major
cluster. Trace the first causal failure rather than the final timeout or wrapper
error. Link the workflow attempt, job, invocation, target, and relevant source.
Separate failures in dependency fetch, analysis/build, fixture startup, test logic,
assertions, teardown, and runner infrastructure. Repeated failures across consecutive
commits may be a sustained regression even if a later commit passes.

Inspect current source around the evidence: fixed sleeps, one-shot assertions on
eventually consistent state, readiness based on an earlier backend stage, port
reservation races, shared mutable fixtures, unbounded waits, external services,
and cleanup ordering. Explain a concrete failure mechanism and location for each
finding. Keep source-only risks separate from observed incidents; a grep match
does not establish causality or frequency.

### Follow the failure mechanism through the test

For each important failure cluster, reconstruct a causal timeline from a failure
and comparable pass. Trace the relevant fixture lifetimes, subprocesses, service
events, assertions and cleanup, following dependencies until the competing causes
can be distinguished. Identify the violated invariant and ordering or resource
condition that permits it. A timeout, idle period or racy-looking source pattern
does not by itself identify the cause.

Inspect sleep/poll loops, read/subscribe gaps, wrong-stage readiness, stale reads,
shared mutable state, resource/port ownership, leaked processes and teardown that
overlaps the next case. Distinguish a slow producer from a late observer and an
application race from a harness race or infrastructure failure. Investigate
why a retry changes the outcome, including caches, retained state, execution order,
random seeds and external dependencies; do not assume independent attempts.

Evaluate changes to the test and harness as well as the application: event-driven
predicate waits, explicit happens-before relationships, fixture scope and ownership,
isolation, setup/cleanup ordering, target boundaries and safe concurrency. Preserve
the tested contract, failure sensitivity and real integration boundaries. Longer
sleeps/timeouts, more retries, weaker assertions, serialization, mocks and disabling
coverage are not equivalent repairs without evidence and explicit tradeoffs.

When feasible, use a bounded isolated reproduction and candidate repair, retaining
the failing conditions. Exercise the relevant scheduling/order/resource variation
with fresh uncached executions and record actual exposure and failures before and
after. Use controlled scheduling or fault injection when it reproduces the race
more decisively than repetitions. A passing rerun alone does not validate a repair;
when the failure cannot be reproduced, label the change a candidate and state what
evidence is missing. Check latency/resource effects so reliability is not bought
with unexplained waiting or reduced coverage.

Continue available source/runtime investigation until there is a supported
mechanism and concrete repair, a rejected hypothesis, or a specific visibility or
resource boundary and next discriminating observation. Scale depth to decision
value; a flake ranking or "inspect fixture races" is not a finished diagnosis of
the major failures. Include the source location, causal evidence, preserved
coverage, reproduction/validation result and remaining uncertainty in the report.

Before adding diagnostics, inspect pytest captured logs, existing service logging,
and undeclared test outputs. Prefer enabling standard logs and reusing an existing
failure artifact fixture. Add only the missing command IDs, cursors, phase/progress,
or process state needed to distinguish competing explanations. Avoid a tracing
framework, HTTP-client monkeypatch, or tests of diagnostic plumbing when ordinary
logging answers the question. A timeout increase requires evidence of bounded work
still progressing, not simply a timeout result.

Rank changes by observed recurrence, operator/retry cost, confidence in mechanism,
and implementation/review cost. Keep already-fixed incidents as historical findings.
An analysis request does not itself authorize test-policy changes, disabling tests,
or rerunning workflows. When fixes are requested, use independently reviewable PRs
and define the evidence needed to measure their effect. Do not claim scheduled
monitoring unless a schedule was actually configured.

## Publish the reliability snapshot

Write a standalone, readable HTML report with frequencies, evidence links, diagnoses,
fragile-pattern hypotheses, recommendations, and coverage limitations. Make the
observation window and source SHA easy to find. Use semantic sections/tables and
purposeful charts; do not wrap a Markdown report in a preformatted block. Visually
inspect desktop/mobile rendering and exercise any filters before publication.

Keep compact reviewed measurements and provenance in `evidence.json`. Full API
payloads, logs, credentials, and profiles stay local. Preserve still-relevant prior
findings with commit-pinned links rather than copying every historical report.

Both skills package `devinfra/ci/reports/publish.py` as `scripts/publish.py`. From the
inspected source checkout, with the reports worktree at `$HISTORY`, run:

```bash
python3 "$SKILL_DIR/scripts/publish.py" --kind reliability --source "$DEVEL_SHA" \
  --window-start "$SINCE" --window-end "$UNTIL" \
  --report "$REPORT" --evidence "$EVIDENCE" --out "$HISTORY"
```

Set `$SKILL_DIR` to this installed skill's directory. The publisher copies reviewed
HTML; it does not generate or validate the analysis. Keep exactly one snapshot in
`reliability/`: `index.html`, `evidence.json`, `manifest.json`, and optional
`attribution.json`. Omit attribution unless actually measured. Publication removes
stale attribution only in this directory. Preserve `latency/`, root navigation,
and `.gitattributes` (which marks evidence JSON as generated).

Open the report PR against `ci-reports`, never `devel`. Fetch the branch again before
publishing; compare windows within `reliability/` and do not replace a newer snapshot
with an older one. Integrate concurrent latency changes without overwriting them.
Git history holds previous snapshots; do not add dated run trees or force-push
history. Skill/tool changes belong in a separate source PR to devel. GitHub Pages
serves the branch at <https://agentydragon.github.io/ducktape/reliability/>; link
that page and note it redeploys only when the PR merges. State what remains unknown
and the next observation that could resolve it.
