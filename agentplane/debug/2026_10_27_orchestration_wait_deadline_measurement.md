# Orchestration wait deadline measurement, 2026-10-27

The merged correction is [#12189](https://github.com/agentydragon/ducktape/pull/12189), commit `f0036950097ee1bac22cece2589d8f77d80017f4`. This note measures the reported `wait_for_turn(wait_seconds=1)` failure in the full MCP acceptance test. The target is `//agentplane/system_tests:test_mcp`; its Codex launcher case checks that the early wait succeeds with `completed: false` and a resume token before submitting a turn. A later wait must observe the completed turn.

## Earlier evidence and exposure

The pre-fix [failure](https://app.buildbuddy.io/invocation/9ba054ed-664f-5a81-981e-3b365a5268d9) and [fresh pass](https://app.buildbuddy.io/invocation/9a80d2fa-9d90-531c-b0a4-a3fad67ba017) ran on the same source SHA, `a31e93dfa297fa3185e6f94bbefe3120c5b515a9`. The failure log shows 11 passed and one failed: `test_launcher_drives_only_the_sandbox_it_launched[codex]` failed at the early wait's successful Action assertion. This is one failure in two selected fresh full-target executions, not an estimated pre-fix failure rate. The [prior reliability report](https://github.com/agentydragon/ducktape/blob/ci-reports/reliability/index.html) discusses the incident and its selection limits.

The corrected PR head had one [fresh full MCP pass](https://app.buildbuddy.io/invocation/cfdd159c-28dc-4b73-bc8a-2427cd955814): 12 collected and 12 passed, including the Codex launcher case. Its target summary has one run and one attempt, with no cached test result recorded. The test process passed even though the `bbr` wrapper subsequently failed while retrieving metadata. The [merge-commit CI pass](https://app.buildbuddy.io/invocation/84e76c7f-114d-5522-b5c0-f4125e0d8ee3) reports `totalNumCached: 1`, so it is excluded from fresh execution counts.

## Reproduction method

Check out the exact merge commit in an isolated clone and run on the remote Bazel runner:

```bash
bbr test //agentplane/system_tests:test_mcp --nocache_test_results
bbr test //agentplane/system_tests:test_mcp --nocache_test_results --runs_per_test=2
bbr test //agentplane/action_service/orchestration_v1:orchestration_v1_test --nocache_test_results --test_filter=TestWait
```

`--nocache_test_results` forces each selected test action to execute. The second full-suite invocation requests two separate runs. The counts below come from the BuildBuddy target summaries and each `test.log`, including the 12 collected MCP cases. `bbr` mirrored the detached source checkout as a patch over remote `devel` base `bf040daee4efe4cb4bb1f367ff5209425eb0ef84`; its runner logs record the base fetch and successful patch application. The local checkout tree was the exact merge commit.

The focused [Go replay](https://app.buildbuddy.io/invocation/3bf2b90e-72f2-4165-8ea6-bb67b3bc7b48) passed at the merged source tree with one fresh run and one attempt. The `TestWait` filter selects six functions covering the deadline and error boundaries named below. Its target summary records no cached result. The expiry cases wait for the fake stream's context to finish; their short `WaitSeconds` values bound the public API call rather than adding a fixed sleep to coordinate the test.

## Result and limit

All three post-merge full-suite runs passed fresh, with one attempt each:

| Bazel invocation                                                                      | Run | Result | Pytest cases | Pytest duration | Bazel test action |
| ------------------------------------------------------------------------------------- | --- | ------ | ------------ | --------------- | ----------------- |
| [402e9a99](https://app.buildbuddy.io/invocation/402e9a99-9e56-4d2a-81c7-878f122a401b) | 1/1 | Passed | 12/12        | 740.48s         | 783.0s            |
| [0a0f8696](https://app.buildbuddy.io/invocation/0a0f8696-8c78-416b-8e9c-a2f4fef05df4) | 1/2 | Passed | 12/12        | 601.82s         | 635.7s            |
| [0a0f8696](https://app.buildbuddy.io/invocation/0a0f8696-8c78-416b-8e9c-a2f4fef05df4) | 2/2 | Passed | 12/12        | 611.91s         | 642.7s            |

The first target summary records one run and one attempt. The second records two runs and two attempts. Neither summary records a cached test result. Each `test.log` shows 12 collected and 12 passed, including the Codex launcher case. The corresponding `bbr` commands used `--nocache_test_results`, and both Bazel invocations completed successfully. This is zero failures in three fresh post-merge full-suite runs, or three exposures of the reported early-wait case.

A small selected cohort can verify the corrected path executes, but cannot establish that the intermittent failure is eliminated. The selected pre-fix pair and post-merge runs were not matched for executor conditions or contemporaneous dependencies, so the counts do not estimate a causal change in failure rate. The Go tests exercise authorization budget, expiry, History timeout before expiry, caller cancellation, backend errors, reconnect handling, and resume-token preservation; the system test exercises the complete launcher path. This is test-environment evidence, not a staging rollout check.
