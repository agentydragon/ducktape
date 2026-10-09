---
name: ci_reliability
description: Quantify CI flakiness and recurring failures from GitHub Actions and BuildBuddy, diagnose specific culprits, inspect fragile test patterns, and publish an evidence-backed HTML reliability report. Use for flaky-test audits, retry/recovery analysis, and measuring whether reliability fixes helped; use ci_latency for feedback-time analysis.
---

# CI reliability and flakiness

Produce a recent reliability diagnosis with failure frequencies, explicit
denominators, concrete culprits, and prioritized fixes. A red build is not by itself
a flaky test. Distinguish observed intermittent outcomes, sustained regressions,
infrastructure failures, and source patterns that are only hypotheses.

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

BuildBuddy's flake statistics are useful leads. Report the service's heuristic
separately from independently verified recoveries and document its sample window.
Unavailable historical logs limit diagnosis; they do not erase recorded outcomes.

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
history. Skill/tool changes belong in a separate source PR to devel. This branch
does not deploy GitHub Pages: give repository/download links without claiming a
hosted report. State what remains unknown and the next observation that could resolve it.
