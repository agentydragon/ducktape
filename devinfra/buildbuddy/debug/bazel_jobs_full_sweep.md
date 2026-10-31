# Full-repository Bazel concurrency comparison

On 2026-10-31, compare the current RBE `--jobs=50` with `--jobs=100` on the full
`//...` selection. The tested source is fixed at
`7d4079880e93f57bbc6019ba1134854fbf0219f8`; this is an experiment, with no change
to the repository's concurrency default.

## Method and provenance

The [outer remote runner](https://app.buildbuddy.io/invocation/08a399c7-5cb4-4281-bac7-e29e0f2a2bdc)
uses 8 estimated compute units, a 100 GB disk request, and the normal
`ducktape-bazel-ci-v4` recycling key. All phases run sequentially in that one
runner and Bazel server. The existing runner probe records the same boot ID and
server PID across the phases. Both treatments use the same pinned RBE action
image, `sha256:c73af98850208e2a898120f9992f4cb8c320c488bc3f3c8607cc06ace96be3c5`.

A common `build //... --jobs=50` warmup passes first in 15.090 s:
[warmup invocation](https://app.buildbuddy.io/invocation/99897b9d-d60e-416d-9c71-bff2cf9c3c31).
Each treatment then runs `test //...` followed by `build //...`, with
`--keep_going --config=rbe --config=ci`, changing only the final `--jobs` value.
RBE and the action caches stay enabled. Both test phases use
`--nocache_test_results`, so this compares the full fresh-test workload with
warm compiled actions, rather than an ordinary mostly cached CI run. Normal CI
tag filtering remains in force; browser, Docker, and VM tests are included.

Use Bazel's `command.profile.gz`, invocation build events and `GetExecution`
metadata for timing and resource evidence. Fetch profiles with
`bbapi tool-log download <invocation> command.profile.gz`; fetch action metadata
with `bbapi execution <invocation> --json`. Worker process time, worker CPU,
client action lifetime, and queue time are separate measurements.

The outer command explicitly disables whole-script retries. The general command
form is `bbr --disable_retry=true --run_from_commit=<source> --script=<program>`.
Use an explicit boolean value: the installed bb CLI leaks a bare
`--disable_retry` into Bazel startup arguments when a Bazel verb is supplied.
The script-only form used here has no such outer Bazel verb; its inner commands
set RBE, CI, image and invocation flags explicitly.

## Results

| Phase                                     |         jobs=50 |                            jobs=100 |
| ----------------------------------------- | --------------: | ----------------------------------: |
| Fresh full test elapsed                   |       607.141 s |                           637.793 s |
| Following full build elapsed              |        24.752 s |                            41.755 s |
| Test outcome                              |    1,694 passed | 1,692 passed; remote error, exit 34 |
| Test critical path                        |       468.563 s |                           523.965 s |
| Bazel client CPU                          |       386.900 s |                           401.560 s |
| Peak post-GC heap                         | 2,059,087,408 B |                     2,314,233,216 B |
| Peak simultaneous remote worker intervals |              50 |                                 100 |
| Summed remote process time                |    23,717.318 s |                        24,212.683 s |
| Summed remote CPU                         |    36,778.742 s |                        38,365.693 s |

The baseline test is
[`0c774d6a`](https://app.buildbuddy.io/invocation/0c774d6a-22e7-4fc5-80a3-76901f297bc6);
its following build is
[`b821118a`](https://app.buildbuddy.io/invocation/b821118a-4069-4ef3-80bb-fb658dacd198).
The candidate uses
[`825ab337`](https://app.buildbuddy.io/invocation/825ab337-2843-4f64-ba75-01ffad2c71ad)
and [`c2fe122a`](https://app.buildbuddy.io/invocation/c2fe122a-f94b-48ea-8fe9-6c04dda41a7e).

The candidate took 30.652 s longer in this observation and failed with two
BEP-aborted targets. `//finance/augur/frontend:sanity_bands_test` lost its
generated JavaScript input (`REMOTE_CACHE_EVICTED`, digest `d72b9ea0690611ea29a0101ec2df0e5edcc03a4dbc86303604314e622589408e/4450`).
`//agentplane/system_tests/suites:test_pod` returned a client-side `RST_STREAM`
execution failure. Its remote process nevertheless continued until
07:48:51.136 UTC, 90.598 s after the Bazel invocation failed. Its completed
execution record is not proof of a passed pytest verdict. The final execution
snapshot contains no nonterminal records.

The following candidate build passed, but reports 35,732 actions (35,376 remote
cache hits, 347 internal, 9 remote), versus one internal action in the baseline
build. Lost-input recovery and regeneration make this a different workload;
41.755 s is not a clean measurement of ordinary 100-job build cost. Normal CI
would have stopped at the failed test phase.

The baseline profile contains 1,732 `action processing` events with mnemonic
`TestRunner`: 1,694 distinct labels plus 38 additional shards. Its 2,039 NEW
remote execution records comprise those test-shard actions and 307 XML helper
actions, with no failed attempt or retry. Do not count the helpers as extra
tests. Their aggregate worker process time is 23,717.318 action-seconds and CPU
time is 36,778.742 CPU-seconds, neither an elapsed-time saving.

The candidate has the same 1,732 profile TestRunner events and 1,694 labels;
two of those labels aborted. Its 2,035 remote records include 303 XML helpers
and one duplicate attempt: a missing input tree for
`//cluster/cdk8s:test_service_ref` was retried successfully with the same
digest/target/output identity. That recovered miss is separate from the two
aborted targets. The finance action never reached a remote execution record.

Queue p50/p95/p99 is 0.059/0.063/0.094 s at 50 jobs (maximum 1.061 s), versus
0.060/0.064/0.082 s at 100 (maximum 0.629 s). Doubling the cap doubled observed
peak worker overlap, while worker process and CPU sums increased slightly.
Those sums include the candidate's post-failure remote tail.

The baseline critical-path test is `test_pod` (468.471 s profile action); the
last test to finish is `test_vm`. In the candidate, `test_vm` has a 523.822 s
profile action and `test_pod` is the final client-side action. Dependency
critical path and final completion are distinct. Remote VM process time rose
from 304.510 s to 519.810 s, whereas MCP process time fell from 394.469 s to
341.115 s. Different start order and worker runtimes prevent a uniform speed
interpretation.

Keep the current default. This failed single pair establishes that 100 jobs
was exercised, with greater overlap and greater client heap use, but does not
establish a green feedback-time improvement or show that the remote failures
were caused by concurrency.

## Interrupted first attempt

The first outer run
[`ab9396a7`](https://app.buildbuddy.io/invocation/ab9396a7-c8ef-4d1c-ad96-506c0472d412)
completed a 50-job test phase but lost its log observer during the candidate.
The CLI classified `RST_STREAM INTERNAL_ERROR` as retryable and replayed the
entire script, reusing its child invocation IDs. Cancelling the replacement
outer while the client remained alive triggered another replacement. The
launcher was then stopped and all known active outer and child IDs cancelled;
the API showed no remaining nonterminal execution records for them.

That attempt is not the matched comparison above. Its captured baseline had
1,693 passes and a 60.1 s timeout in
`//x/ai_session_archive:test_codex_title_retry`; the later baseline above passes
that test. No candidate total or following build was obtained from the
interrupted attempt. See the [whole-script replay evidence](ci_remote_cancellation/vm_shutdown_retry.md#full-suite-replay-on-2026-10-31)
for the three outer IDs and separation from Bazel's action-level retries.

## Limits

This is one fixed-order pair. The second treatment can benefit from warmer test
snapshots and the prior workload. Remote actions run on different workers, and
other investigations submitted RBE work concurrently. The outer runner's common
warmup also removes the cold runfiles construction seen in an earlier latency
profile; this experiment cannot measure that earlier readiness delay directly.

The probe continues to the following build and candidate even if a test phase
fails, preserving diagnostics. Normal CI stops that sequence on failure.
There is no GitHub required-check or auto-merge timing comparison here; these
are remote Bazel invocations. Do not convert this pair into a fleet-wide saving
or add its delta to other latency estimates.
