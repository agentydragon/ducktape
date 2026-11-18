# Explicit App fixtures and ordinary-pytest screen

Frozen screens for [PR #16489](https://github.com/agentydragon/ducktape/pull/16489).
The [current branch disposition](2026_11_15_after_native_ports.md) distinguishes the
merged implementation from the remaining manual comparison targets.
Goal: at least 30% faster complete required PR feedback with no more than 25%
extra compute and no lost important-path regression coverage. The goal remains
unqualified; these small screens do not estimate a flake rate.

## Source and ownership

The measured implementation is `6e6ad85a4c9c28a86fed31d83a6ea760677c171b`, based
on devel `b9b280ef856546c67752cbb04331280d26559031`. It preserves devel's
CaseBatch worker scopes, Pod reuse/discard ownership, cleanup drain and private
diagnostics. PR #16615 is merged and contributes no remaining diff here.
All 15 App Pod scenario function bodies are AST-identical to that devel base;
the seven platform scenarios remain in the native target.

Common conftest hooks own tracing, harness parameters and scripted models.
Only App consumers load `app_fixtures`; only operator-login consumers load
`mcp/operator_fixtures`. Shared Sandbox descriptors/template names stay in
`driver/sandbox.py`; App lifecycle operations move intact to `app_sandbox.py`.
The six moved fixture bodies and renamed Sandbox tests were compared against
their originals. Existing diagnostic privacy and error precedence remain.
No new third-party pytest plugin or cancellation implementation was added.

[Rebased configured-runfiles query](https://app.buildbuddy.io/invocation/31396b90-7966-4676-8072-14aaecfaabef)
finds zero `agentplane/app/` runtime files in native Pod platform, central-service
and Gateway-crash targets; the App Pod positive control retains eight. Earlier
[control](https://app.buildbuddy.io/invocation/d2303eb8-3640-4da4-9bab-25af30d7bf1b)
and [candidate](https://app.buildbuddy.io/invocation/291e154b-5a63-44fc-8bd7-cff68cded380)
queries measured 4/5/5 to 0/0/0 and 8 to 8, on their original frozen sources.
This is action-input evidence, not a wall-time saving.

## Selector replay

The production `bazel-diff generate-hashes`/`get-impacted-targets` algorithm,
source/manual filtering and ordinary-test tag filtering replay one historical
file's before/after blob onto each frozen arm. The script verifies that each
historical after blob exactly equals its arm's current file. This is not a
replay of the entire historical PR and does not estimate how often a change
occurs or whether an omitted target was the complete-CI critical path.

The initial [replay](https://app.buildbuddy.io/invocation/c0495a54-fe5f-4307-9fc3-6b804f378b24)
used control `e0e5c286f811731baade1b71ecab7e0ac6cf3700` and candidate
`b2ef43514d7fe151cf5ef199de1baae08b8f8816`: App Client selected 79 to 15
ordinary tests; Gateway 45 to 45; Controller 20 to 20. Its private artifact
archive is SHA256 `9f8dbb3518800afebf5138922571b590509cd3c62dae4a137e105df007956b2a`,
24,577 bytes. Source-selection commands completed; final runner recycling
warned about a removed owned worktree's bazelrc. No test execution occurred.

The first post-rebase replay [stopped](https://app.buildbuddy.io/invocation/ac664bc8-c17e-4264-bdb9-9343e57e007e)
because devel had changed the Controller file. Its assertion prevented an
invalid source comparison. Partial control counts (79 and 45) do not establish
a complete pair. The corrected repeat uses the latest exact Controller edit
`f1a62b7d43d6dd59f9f8e9362d1316e0219a0e6f`; that is a wording edit used as
a dependency-selection positive control, not a behavioral regression test.

The corrected [pinned replay](https://app.buildbuddy.io/invocation/20d7de33-6b2e-4200-b873-2ddc91b8bc18)
completed all selection commands with exit zero, using control
`5dc6d1a567cb9f21db696a9ed26bf36e78c7d8f9` and candidate
`6e6ad85a4c9c28a86fed31d83a6ea760677c171b`. It confirms 79 to 15 App Client
selected tests, 45 to 45 Gateway tests and 20 to 20 Controller tests. The verified
private archive is SHA256
`eaee11140d387be210e809c14bfb1f7eae0c3b0e0f1319fba1a8eec96e4473ba`,
24,707 bytes. The App Client removes native platform, native central services,
Gateway-crash and other ambient-fixture consumers while retaining the App Pod
positive control. The renamed `test_app_sandbox` is an inventory rename, not
extra scenario coverage.

## Ordinary pytest experiment

The required target retains CaseBatch. Three manual targets invoke the same
scenario functions and marks: an unsharded ordinary-pytest control and two
sequential shards. Storage inventory runs first. Shard A groups compatible
shared-runner probes; shard B holds the complementary fresh-resource cases.
The latest fixture uses the same `Worker.open` owner as devel.

The initial allocation used older JUnit case durations as a hypothesis. It did
not measure the current implementation. The first fresh A/B/A screen was:

| Arm                                                                                                         | Pinned source                              | Maximum test execution (s) | Observed CPU (s) | Bazel elapsed (s) |
| ----------------------------------------------------------------------------------------------------------- | ------------------------------------------ | -------------------------: | ---------------: | ----------------: |
| [A1 required](https://app.buildbuddy.io/invocation/5ff3989c-604a-4248-a308-f3a0166af139)                    | `42e87a560760c2f85b7d2afe55fd6ca2c051166d` |                    306.250 |          789.834 |           336.990 |
| [B1 two ordinary shards](https://app.buildbuddy.io/invocation/6952307b-ff92-46be-aac2-549abecdd6f3)         | `e7dac6973182dec43ab0de6bb6c7b1af97f59c92` |                    311.375 |         1087.334 |           342.407 |
| [A2 required, same source as B1](https://app.buildbuddy.io/invocation/2a8cc07f-0878-4608-ac21-06c1605804ea) | `e7dac6973182dec43ab0de6bb6c7b1af97f59c92` |                    315.533 |          774.251 |           344.488 |

A2/B1 preserved the exact 15-case JUnit inventory, with no skipped/failed/error
cases, one execution of each target and test retries disabled. B1 saved only
1.32% of maximum test time and 0.60% of Bazel elapsed, with 40.44% more observed
TestRunner CPU. Summed worker occupancy was 589 versus 316 seconds. Every arm
reported a warm clean baseline with the same fingerprint; image builds were
cache hits. Stack setup was 46.6 seconds in A2 versus 70.6/47.9 in B1; cleanup
29.8 versus 28.4/29.2. Nested phase times overlap and are not added to test wall
time. Valid RBE queue intervals were around 0.06 seconds, not the explanation.
This fails the adoption threshold. A1 is an adjacent-source check; A2 is the
same-source comparator. These are action screens, not complete PR feedback.

The post-worker-rebase repeat pins both arms to
`6e6ad85a4c9c28a86fed31d83a6ea760677c171b`:

| Arm                                                                                              | Maximum test execution (s) | Observed TestRunner CPU (s) | Bazel elapsed (s) |
| ------------------------------------------------------------------------------------------------ | -------------------------: | --------------------------: | ----------------: |
| [B2 ordinary shards](https://app.buildbuddy.io/invocation/63ebfa7e-c7f1-472f-960e-f46d5fcaee52)  |                    313.861 |                    1046.335 |           366.699 |
| [A3 required control](https://app.buildbuddy.io/invocation/2f92a6ab-7b18-479f-ac13-8098bedca6cc) |                    293.608 |                     718.740 |           323.141 |

The exact 15-case inventory matches, with every case passed once and no skips,
errors, test retries or extra physical TestRunner executions. Both shards and
control report the same warm clean baseline fingerprint. The two shards take
255.9/313.9 seconds, use about 5.0/4.8 GB peak memory each, and wait about 0.06
seconds in valid RBE queues. Their stack setups take 55.2/52.6 seconds and
cleanups 27.8/29.8 seconds. The maximum test duration is **6.90% worse**, Bazel
elapsed **13.48% worse**, and measured TestRunner CPU **45.58% higher** than the
same-source required control. Worker occupancy is 572 versus 294 seconds.
Internal RBE retries were not independently audited; CPU is measured TestRunner
CPU rather than total CI compute. These small warm screens are not a p90, cold-start, reliability or complete-CI
qualification. They reject this allocation; they do not prove that every
possible sharding design is worse. Keep the standard-pytest targets manual and
retain required CaseBatch. Do not spend a 30-pair qualification on this layout.

## Excluded failures and correctness checks

The [first shard launch](https://app.buildbuddy.io/invocation/66b91742-9550-4d1b-b478-a6e3064fdf55)
failed analysis because Gazelle had removed the shared source from manually
named siblings. No test ran. The source is now eligible for its default manual
target; sibling declarations retain their source/dependencies explicitly.

Earlier rebased CI exposed a removed fixture-chart template helper and moved
Scenario fixture patch points. Renderers follow the landed explicit template
owners; Scenario tests patch `app_sandbox`. [Focused validation](https://app.buildbuddy.io/invocation/f001f374-df08-4a06-8b9a-958fe99c98bf)
passed those three affected targets. An [explicit library build](https://app.buildbuddy.io/invocation/51760c60-f5d7-488c-8303-5b03710f5d75)
caught a missing protobuf typing dependency in neutral `sandbox`; it was
restored, and [all six fixture libraries](https://app.buildbuddy.io/invocation/9688cc38-4ff6-4cb4-94a0-0c73f583c90e)
pass at the latest implementation source. One mistyped full SHA and one
abbreviated SHA submission failed remote Git setup before Bazel; neither is a
physical test failure or performance sample. Use full `git rev-parse` output.

[Required CI](https://github.com/agentydragon/ducktape/actions/runs/43694440686)
passed for implementation head `6e6ad85a4c9c28a86fed31d83a6ea760677c171b`.
The [Bazel invocation](https://app.buildbuddy.io/invocation/e7da1861-e0e0-5558-85c7-01ad8a301b51)
has 922 passing target summaries on synthetic merge
`f825f8a907f0f86de5d28b70cc023170374595a7`, whose parents are devel
`fd2c5c1bbb4eb5f7bd3bbdab80b794ab4c880559` and that implementation head.
[Pre-commit](https://github.com/agentydragon/ducktape/actions/runs/43694440615)
and [artifact/import checks](https://github.com/agentydragon/ducktape/actions/runs/43694440590)
also pass. Summaries include cached tests; these correctness checks are not a
fresh performance population.

## Next decision and reproduction

Keep required acceptance. Full historical PR replays and complete-feedback
measurements must determine the benefit of the input cut; an App change may
still select the App Pod critical path even after excluding native suites.
The next larger architecture candidate is a contract inventory separating
Session/History/authorization/turn-state assertions from actual Kubernetes,
issuer, workload identity, volume replacement and suspension witnesses. Spike
real service processes plus a small database for the former; retain real
physical acceptance for the latter. Do not substitute a fake for a unique
boundary. Expand to 30 matched pairs/100 fresh executions only after a small
screen plausibly clears the 30% latency/1.25 compute gate. Track failure and
cross-case state isolation, busy windows, warm/cold baselines and a four-PR burst.
[Follow-up tracker #16099](https://github.com/agentydragon/ducktape/issues/16099)
owns this work; raw artifacts remain private.

For a fresh action screen, use Bazel via `bbr` with full source SHA:

```sh
bbr --run_from_branch=agent/native-pod-services-partitions --run_from_commit=<full-source-sha> test \
  //agentplane/system_tests/suites:test_pod_sequential_a_spike \
  //agentplane/system_tests/suites:test_pod_sequential_b_spike \
  --nocache_test_results --runs_per_test=1 --flaky_test_attempts=1 --test_output=errors
```

Run the required `test_pod` target sequentially at the same source with the same
freshness/retry flags. Compare canonical JUnit identities, physical TestRunner
metadata (`cpuNanos`, worker and execution timestamps), queue clocks and existing
trace phases. Do not infer fresh executions from passing cached summaries.
