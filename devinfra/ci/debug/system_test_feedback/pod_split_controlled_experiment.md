# Pod split evaluation: coverage and experiment boundaries

The single TLS-tail split has equivalent selected scenario coverage, but no measured
full-CI speedup. This continues [the full-graph investigation](four_minute_system_test_graph.md).
Required CI and the original Pod target remain unchanged. The original control
and both split actions passed, with equivalent JUnit case sets and completed
fresh-fixture cleanup. The control has an unresolved progress/attempt timeline
anomaly, so this is not a causal speedup or total-cost comparison.

## Independent collection

Source `e5d91616108c45a962830ccd32de291bb836b9b8`, with the
[manual-only spike](pod_split_controlled_experiment.patch), collected on RBE:

| Alternative | Invocation                                                                            | Selected cases |
| ----------- | ------------------------------------------------------------------------------------- | -------------: |
| Original    | [cb0b960b](https://app.buildbuddy.io/invocation/cb0b960b-128e-400f-97da-894af7938e18) |             21 |
| Split main  | [edd30c0f](https://app.buildbuddy.io/invocation/edd30c0f-459b-4065-87c8-e03e2f52eeb2) |             20 |
| Split TLS   | same split invocation                                                                 |              1 |

Both Bazel collection invocations completed successfully. Each target's downloaded
`test.log` lists the actual pytest node IDs; comparing multisets establishes
`original = main + TLS`, with disjoint split sets. The workspace storage mutation
appears once in main and never in TLS. Collection executes no fixtures or cases.

The original was collected before applying the spike. All physical comparison
alternatives use the same patched tree, preserving the original target and adding
only manual split targets. Each target's session-scoped `deployment` fixture owns
a fresh fixture stack. `CaseBatch` builds its work list from selected pytest items,
runs storage exclusively before other selected main cases, then uses two slots.
The TLS alternative does not select storage. Baseline reuse retains only compatible
infrastructure; `persistence-suite.json` records fresh namespace, PostgreSQL and
certificate/Secret identities plus cleanup certification.

```sh
bbr test //agentplane/system_tests/suites:test_pod \
  --test_arg=--collect-only --test_arg=-q --test_output=all --nocache_test_results
bbr test //agentplane/system_tests/suites:test_pod_main_spike \
  //agentplane/system_tests/suites:test_pod_gateway_tls_spike \
  --test_arg=--collect-only --test_arg=-q --test_output=all --nocache_test_results
```

## Recovered earlier physical evidence

The earlier `630fd860` actions' recovered OTel and fixture outputs sharpen the
candidate's expected ceiling. These actions overlapped, ran on different workers,
and do not form a causal comparison. Both retained a warm baseline and completed
fixture cleanup.

| Earlier action      |   Setup | Case envelope |  Summed case spans | Cleanup |
| ------------------- | ------: | ------------: | -----------------: | ------: |
| Original `88924b1e` | 71.234s |      232.686s | 424.949s across 21 | 31.301s |
| Main `fa944edd`     | 95.921s |      229.566s | 432.549s across 20 | 32.901s |

The original's TLS case took 24.506s and ended 16.722s after every other case,
compared with the older trace's 67.9s tail. Removing one selected case did not
freeze the other scenarios' durations or setup cost. The separate earlier TLS
pass took 188.771s. Its extra setup and action cost need accounting before
promoting a split whose removable tail varies this much.

The OTel files were recovered from each successful ExecuteResponse's
`outputDirectories[].treeDigest`; the original control's `test-spans.jsonl` digest
is `33f09fd1aad51808c6ecc7c23e27caf3c8a6176e5a6995553fadd2667c75e03f/308174`,
and main's is `337123852c5000a4f636224321ceedf62db58713ce28d29d08d94a3ee9e96d89/296237`.
Their fixture evidence digests are
`554597833b6633066502de18542777cb181261e0b179dea44051a0814ddacb66/1128`
and `d4713cb467d78c1310160af27fb2ab11b25a5d87942eaa05b51b7cbe08b10b42/1128`.

## Evaluation protocol

Before physical submission, seed `202611122321` chose split then control for the
first pair, with the second pair reversed to balance order. Every physical action
must be new rather than a reused target-summary duration. Compare action
command/platform/input-root identities honestly: target names, runfiles roots,
manifest names and pytest selection differ even when application source matches.

Measure action queue, worker preparation, execution and output upload separately;
then setup, scenario envelope and cleanup from OTel. Report requested 4-CU action
seconds and observed CPU seconds separately. Stratify observed warm/cold baseline
outcomes and verify fixture UIDs and cleanup. Full Bazel elapsed and the complete
required gate are distinct clocks; a Pod-only pair cannot establish a full-CI saving.

Observer loss never authorizes replay. Preserve the original invocation and exact
`/uploads/...` operation names, read their completion state, and recover original
ExecuteResponse/JUnit artifacts before considering another submission. Missing
protobuf `status` and `exitCode` in a present result mean OK/zero; an invocation
marked disconnected still does not establish a full Bazel gate verdict.

## Next architectural boundary

A broader split cannot treat scenarios as freely interchangeable jobs. The Pod
runner pool keeps each created harness pair until teardown; concurrent shared
cases can create two pairs. The fixture has room for five Sandboxes, while an
`open` case may require two beside those retained pairs. Removing intermediate
cases can create new shared-case adjacency before later `open` cases and exceed
that capacity. Preserve the existing ordering/capacity invariant or change pair
ownership with explicit coverage before balancing cases into arbitrary groups.
The present TLS-tail extraction keeps this edge at the end.

More independently owned stacks can expose case parallelism and reduce that
per-node constraint, but duplicate setup and cleanup plus requested 4-CU capacity.
Their admission must be measured under concurrent demand. Even a successful Pod
split leaves the measured MCP Actions, MCP services and VM paths in the required
graph; those remain separate budgets in the full-graph note. Fixture reuse and
earlier exact-source input availability must be tested in combination before
claiming four-minute feedback. The single-tail experiment cannot establish or
reject that larger architecture.

## Fresh split feasibility run

[bbr invocation `5fda9605`](https://app.buildbuddy.io/invocation/5fda9605-f7bd-4c04-9581-b3dab7a9f9fa)
submitted both physical split targets with `--nocache_test_results
--test_output=errors`. Its outer observer lost `RST_STREAM INTERNAL_ERROR`, and
the inner invocation disconnected. Neither target was resubmitted. The exact
accepted operation names remain the recovery handles:

```text
TLS: /uploads/f455577f-e05c-42e1-bd4b-d1de91009c21/blobs/dd8ecfb5a40a34cca189d323cfe705c302b9bc698b6853e3c4e50681fcbc806f/1218
Main: /uploads/e930ed58-d4b1-4bea-83e8-c67ccab08ade/blobs/d307ae30b6f8be8aeb58eaa0214bbbdcb7c8e3a290763809c59b42411ad18a6e/1218
```

The local source stayed `e5d9161610` plus the manual spike. The remote runner
instead checked out `96253f5e48ad065ab967ff0c56735314330dc18a` and cleanly applied
cumulative patch `4371dd4621ba9ad637e46963bda96912ff5004513317df4e2ec8eac0dcd25337`.
The final remote source tree was not independently hashed; base metadata alone
must not be called tested-source identity.

Decoded actions request identical 4 compute units, 50 GB, Firecracker image,
persistence permission and recycling key. Both have `doNotCache=true` and new
physical execution links. Their input roots differ: main `dbf5c23a.../165`,
TLS `ba2ecdb8.../165`. Decoded command differences are target, pytest selection,
runfiles/manifest/output paths and their corresponding Bazel test environment;
the actual main `-k` expression is one argument.

The TLS action's original ExecuteResponse and JUnit establish one passing case,
0 failures/errors/skips. Its baseline is warm and cleanup certified complete.
Queue was 0.073s, worker preparation 0.801s, execution 171.751s, output upload
0.710s; OTel setup 92.911s, case 23.608s, cleanup 29.253s. The requested execution
budget is 11.45 CU-minutes (4 times execution wall time), not measured billing.

The run overlaps an independently submitted browser validation invocation
`da2d88f3-f894-4a34-ae1b-55eeef5de9de` around 23:24 UTC. This is a feasibility
observation with interference, not an isolated matched comparison. The original
full Bazel gate remains disconnected. No full-gate speedup can be inferred.

### Input identity and comparison scope

A recursive CAS Merkle-tree comparison of physical main/TLS runfiles, then
main/control runfiles, found 123 matching subdirectory digests in each comparison.
Only target-specific launcher, bootstrap, build metadata, virtualenv directory
and image-manifest filenames differ; no common-path file content differs.
The three image manifests have identical content digest
`7c9da1fdd522e166084719e1e866318ad3a9175990f05aa42c8af4b112a57d12/1981`.
Direct CAS hashes of `test_pod.py`, `pod_cases.py` and `pod_runners.py` also match
the local intended source. This verifies common tested inputs and platform,
not identical entire actions or a hash of the complete remote repository.

The original control is [invocation `92e207d1`](https://app.buildbuddy.io/invocation/92e207d1-4e49-4a17-927d-82146fb5d0cd),
outer runner `f609e1dd-23d8-43b8-ae51-1ca187712362`, exact accepted action:

```text
/uploads/ad9242ce-fe35-4514-ba1d-9b640322b5aa/blobs/6b347b76cee4ed275a1a55f90b01e2d38919f77a7fde04c14f905510ecc08878/1218
```

It uses remote base `022c3c9d4fb59587c582a5efa3a7a4087baba346` plus cumulative
source patch `87bb052369c6673e9ee23eefcec2dadbb436c01efbe9da67683b8551d9bd938d`
and two note/patch artifacts. Its original target's common CAS runfiles and image
manifest match the split despite that different base. Its platform equals both
split actions, and it has `doNotCache=true` and a new physical execution link.

The first split also overlaps RBE compilation for the read-only recovery probes
(`b9863a10`, `5b5b1d64`, around 23:24–23:30) and focused bbapi validation
(outer `097090f3`, starting around 23:28). They did not resubmit the split's test
operations, but add provider load; their queue/worker effects are unmeasured.
The control follows the split and known browser completion. This order cannot
isolate general time-varying provider load, and no matched cold pair exists.

The split's recovered main JUnit has 20 passes and no failures/errors/skips. Its
warm fixture cleanup completed. The physical action ran 430.477s, including
112.542s setup, 248.954s case envelope (455.761 summed case seconds) and 34.134s
cleanup; observed CPU was 1189.115s. Combined with TLS, the split consumed
602.227 process seconds, 1610.430 CPU seconds and 40.15 requested execution
CU-minutes. CPU seconds, wall time and requested CU-time are distinct quantities.

For this run the first action started 36.50s after Bazel started, and last
TestRunner worker output completed at 469.46s. The 40s conditional action-start
budget was achieved, while this arrangement still missed 240s. These observations
do not measure the complete gate or rule out another arrangement. Active
`GetExecution` stage clocks were epoch-valued and its stage remained `EXECUTING`
after physical completion; those values must not be interpreted as a nine-minute
physical wedge. Direct `WaitExecution` and original ExecuteResponse clocks
established the completed 430.477s action.

### Recovered control and returned-record comparison

The continuous read-only `WaitExecution` reader returned the original control's
terminal `COMPLETED`, `result_present=true`, `status_code=0`, `exit_code=0`.
Its original JUnit records 21 passes and no failures, errors or skips. Comparing
JUnit case-name multisets confirms `control = main + TLS`, with disjoint split
sets. All three returned fixtures were warm, certified cleanup complete, and
have distinct namespace, PostgreSQL Pod, root certificate/Secret and collector
namespace UIDs. The [machine-readable evidence](pod_split_controlled_experiment.results.json)
retains the returned records, exact operation names and JUnit digests.

| Returned record      |  Process |    Setup | Case envelope | Cleanup | Observed CPU | Requested execution CU-minutes |
| -------------------- | -------: | -------: | ------------: | ------: | -----------: | -----------------------------: |
| Split main           | 430.477s | 112.542s |      248.954s | 34.134s |    1189.115s |                          28.70 |
| Split TLS            | 171.751s |  92.911s |       23.608s | 29.253s |     421.315s |                          11.45 |
| Control final record | 469.744s | 116.576s |      263.375s | 38.458s |    1317.766s |                          31.32 |

The split records sum to 602.227 process seconds, 1610.430 CPU seconds and
40.15 requested execution CU-minutes. The returned control record is not proven
to include all work under its scheduled execution, so these numbers do not rank
total cost. Requested CU-time is four times returned process wall time, not
observed billing. The recorded last TestRunner worker completion was 469.46s
from split Bazel start and 1330.74s from control Bazel start. Both full Bazel
invocations disconnected; neither number is a recovered complete gate verdict
or an attributed improvement.

The control's TLS case took 16.181s and ended 14.601s before the last case,
`every_tab_follows_one_log_and_a_reconnect_resumes_after_its_last_event`.
It had no observed post-other-case TLS tail, while the older controls had 67.9s
and 16.7s tails. Case duration and position in the limiting path are unstable.

### Progress/attempt timeline anomaly

The control's outer observer failed with
`Internal: stream closed with task still claimed`; its inner invocation remains
`DISCONNECTED`. The agent did not resubmit or cancel the original operation.
Before completion, a direct read-only `WaitExecution` snapshot at
23:43:04.409595541 UTC returned the same exact operation and matching action
digest, `EXECUTING`, with an `EXECUTING_COMMAND` progress timestamp of
23:42:10.886849668 UTC. That timestamp is a publication heartbeat, not command
start; live stdout/stderr stream names and worker clocks were absent.

The successful terminal response instead records queued time 23:33:08.606655703,
worker start 23:46:40.677287035, command span 23:46:41.173521517 to
23:54:30.917787143, and worker completion 23:54:31.890726351 UTC. Its
`workerQueuedTimestamp` is 23:46:40.634616416. The final VM log begins with a
Firecracker start/snapshot load at 23:46:41; its usage timeline begins 23:46:51,
JUnit begins 23:47:08 and OTel setup begins 23:47:09. Those independent outputs
corroborate this final record's chronology. They do not assign the earlier
23:42 heartbeat to that record.

Consequently the 812.071s queued-to-final-worker-start span cannot be called
pure waiting, and 469.744s cannot be assumed to cover every physical attempt.
The final `GetExecution` response exposes one scheduled execution row, not an
attempt history. The inspected upstream
[`Execution` schema](https://github.com/buildbuddy-io/buildbuddy/blob/879c2cdfc08b7d432394c3eea7b2fd6e41328cbf/proto/execution_stats.proto#L73-L114)
explicitly says the response and row reflect the latest attempt after an
internal retry. This permits replacement but does not prove a retry happened
here. The returned metadata supplies final worker/executor/runner IDs and
`taskNumber=1`, but no prior attempt or lease IDs. The only returned server log
is the final VM log. Retry, retained progress and clock-domain explanations
remain unresolved without provider attempt/lease history; no cause is asserted.

### Decision and stopping condition

Do not enable this single-tail arrangement in required CI from this pair.
Its early 36.50s action start still led to a 469.46s last returned TestRunner
completion in the observed run. Duplicated setup is real in the returned
records, the selected TLS case has no stable tail, and the control's earlier
progress cannot be reconciled with its final record. A reverse-order pair
would sample more variance without resolving the missing provider history or
showing this arrangement reaches four minutes. No reverse pair or full required
graph trial was submitted.

This does not reject larger partitions or fixture reuse. A broader case split
must preserve the runner-pair/Sandbox capacity invariant above and account for
duplicated stacks, realistic concurrent admission and fresh state. A combined
fixture/input-readiness design and the remaining MCP/VM paths still need a
separate bounded experiment. The current follow-up ends with equivalent
coverage, independently matching tested inputs, passing original action
artifacts, and a precise provider-visibility boundary; it claims no full-CI
speedup or total-cost ranking.
