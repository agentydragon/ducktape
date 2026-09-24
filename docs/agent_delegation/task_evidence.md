# Model-by-task evidence

Reviewed 2026-09-24. [Research report](research.md).

## Sources with individual outcomes

### Terminal-Bench: repeated trials and run identities

The publisher's [4.0 charts](https://www.tbench.ai/blog/terminal-bench-4-0/rollout-charts.html?chart=waffles)
contain model-by-task outcomes, five trials per task, timings, and trial IDs.
Inspected series include **GPT-5.6 Luna / Codex** and **GPT-5.6 Sol / Codex**, not
GPT-6. The chart does not specify their reasoning effort. Job links:
[Luna](https://hub.harborframework.com/jobs/b7444ba2-78b7-46a0-a3c1-2196d40a337d),
[Sol](https://hub.harborframework.com/jobs/5aebbab5-1f8f-4cdb-8073-c6ce785a930d).

Selected publisher-reported passes, including mixed outcomes and failures:

| Task ID                    | GPT-5.6 Luna | GPT-5.6 Sol |
| -------------------------- | -----------: | ----------: |
| photonic-waveguide-routing |          5/5 |         4/5 |
| layout-config-recreation2  |          5/5 |         5/5 |
| coq-block-bound            |          4/5 |         5/5 |
| react-lead-form            |          3/5 |         4/5 |
| wal-recovery-ordering      |          3/5 |         2/5 |
| live-database-cutover      |          0/5 |         3/5 |
| mvcc-lsm-compaction        |          0/5 |         0/5 |

Reproduction: fetch the chart HTML; JSON-parse the literal assigned to `const PHASES`
(do not execute downloaded JavaScript). Select series by `label`; join `items` by
`task`; count each item's `runs` with `reward === 1` and retain the denominator.
The chart also provides `jobId` and run `id` for tracing individual outcomes.
These figures were extracted, not independently regraded or rerun.

### What those task names mean

The version-pinned task briefs make the results more interpretable:

- [Photonic waveguide routing](https://github.com/harbor-framework/terminal-bench/blob/v4.0.0/tasks/photonic-waveguide-routing/instruction.md):
  produce routes for nine connections under bend geometry, obstacle clearance,
  separation, and weighted-cost constraints.
- [Layout reconstruction](https://github.com/harbor-framework/terminal-bench/blob/v4.0.0/tasks/layout-config-recreation2/instruction.md):
  recover a component/text configuration from a rendered image, targeting at least
  98% identical pixels with the supplied renderer.
- [Coq proof](https://github.com/harbor-framework/terminal-bench/blob/v4.0.0/tasks/coq-block-bound/instruction.md):
  complete a declared theorem without changing protected signatures or introducing
  unproved assumptions; compilation is required.

These are substantive problem-solving examples, not only extraction or transcription.
But selected successes establish demonstrated capability under those conditions,
not a reliable assignment rule for an entire task family. Even 5/5 is a small sample;
selection after observing success makes broad generalization especially unsafe.
A stronger model's 4/5 versus a cheaper model's 5/5 is not strong evidence of a real
advantage. No GPT-6 or xhigh result should be inferred from these labels.

### SWE-bench: issue-level results and patches

[SWE-bench experiments](https://github.com/SWE-bench/experiments) publishes submission
entries with per-instance outcomes. Artifacts can include predictions, test output,
patches, and trajectories; newer entries point to submitter-owned repositories via
`metadata.yaml`, older ones to public S3 storage. Availability varies by submission.

A concrete inspected directory is
[GPT-5 mini / mini-SWE-agent v2.0.0, 2026-02-17](https://github.com/SWE-bench/experiments/tree/main/evaluation/verified/20260217_mini-v2.0.0_gpt-5-mini),
which contains `per_instance_details.json` and `metadata.yaml`. Matching instance IDs
across submissions permits a model-by-issue comparison. This directory was located,
not analyzed into a matrix here. The inspected Verified directory listing did not
include a GPT-6 submission.

Prefer comparisons with the same harness/version, task split, resource limits, and
effort. Distinguish unresolved, not attempted, infrastructure error, and absent
artifact. A single successful patch is an example, not a repeatability estimate.

### Other useful granularity

[CodeRouterBench](https://github.com/LanceZPF/agent-as-a-router) is explicitly designed
for comparing routers using task/model outcomes; its paper's appendices include
model-by-dimension summaries. Dataset access/schema was not independently validated
in this investigation. [Applied Compute's report](https://www.appliedcompute.com/research/training-an-agentic-router)
provides concrete routed SWE-bench examples, but selected cases rather than a full
downloadable matrix verified here. See [the report](research.md) for limitations.

## Is Terminal-Bench 4.0 harder?

The [4.0 release explanation](https://www.tbench.ai/news/terminal-bench-4-0) says it
removed eight tasks, including two saturated ones, fixed nineteen tasks, and calibrated
resources. Saturation meant every tested latest-generation model class/family passed
all five trials. The release uses eight-hour agent limits and reports reduced timeout
noise versus 3.0. A major version now denotes changes requiring reruns, not necessarily
an entirely new, uniformly harder suite.

Removing universally solved tasks makes the remaining mix less forgiving, but other
changes can help models. The evidence supports “different task distribution and
conditions,” not a measured difficulty multiplier over 2.1.

[AA's methodology](https://artificialanalysis.ai/methodology/intelligence-benchmarking)
adds a separate comparability issue: its 4.0 measurement uses 66 tasks,
mini-swe-agent, and three repeats, whereas its 2.1 measurement used 89 tasks and
Terminus 2. The publisher chart above uses Codex for these two model series and five
repeats. Neither cross-version percentages nor cross-harness results are interchangeable.

## From examples to routing evidence

A useful record joins task description and family, model/effort, harness revision,
resource budget, attempts and outcomes, tool/token cost, and verification/repair.
Missing effort or cost stays unknown. Compare within matched conditions before
pooling data; use representative families and holdouts, not only memorable successes.
For ducktape, the most valuable next evidence is ordinary accepted/repaired delegated
work with these fields—not more aggregate leaderboard percentages.
