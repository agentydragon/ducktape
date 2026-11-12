# Recover the accepted action's result using its exact execution ID

The Nov 12 Pod control and corrected split main **passed their physical test
actions**. Their Bazel invocations remain disconnected; neither is a successful
full Bazel gate. The earlier [graph note](../../ci/debug/four_minute_system_test_graph.md)
incorrectly described these test verdicts as unavailable.

## What went wrong in the investigation

The retained control observer had already retrieved its ExecuteResponse at
22:47:39 UTC. We misread JSON with an omitted `status` and omitted
`result.exitCode`: protobuf defaults make these OK and zero, respectively.
The response contained a real `result`, its output file identities and execution
metadata. Subsequent JUnit retrieval confirms all 21 cases passed.

A separate failed lookup removed the execution ID's leading `/`. That changes
the lookup key: BuildBuddy caches a response under the hash of the **entire
execution ID string**, wrapping the serialized ExecuteResponse in an
ActionResult's `stdout_raw`. It is a different lookup from downloading an
advertised `executeResponseDigest` from CAS. Rechecking at 23:19–23:25 UTC:

- Exact `/uploads/5e382…` lookup succeeds.
- `uploads/5e382…` returns NotFound for ActionResult
  `bb3976e63e3a9796ae53917b5f075e8e69420cfdbf70242c433d56e05e5437ae/120`.
- The advertised CAS response
  `4483a8c0c4862fc847e066b8d06243ec093e70b9ed31c05a28df481a47a797a6/121`
  still returns HTTP 404.

This is a correction to our lookup and interpretation, **not evidence of delayed
result availability**. The existing pinned `bb` 5.0.500 can recover both results.
No test was resubmitted to recover them.

## Read-only recovery procedure

Find the relevant `TestRunner` execution in `bbapi --json execution INNER_ID`.
Copy its `executionId` exactly, including leading slash, instance name and digest
function components. Keep separate outer runner, inner Bazel and physical action
identities. Prefer the official command's default Markdown for human review:

```bash
bb execution get "$execution_id"
bb execution get "$execution_id" --output=json > "$scratch/response.json"
```

The default display explicitly prints `RPC status: OK (0)` and `Exit code: 0` for
these actions. Require a real result: an omitted result is unavailable evidence,
even if an absent status defaults to zero. A nonzero execution status or exit
code is not success. A command that successfully reads an error response is
not itself evidence the action passed. A failed read, wrong identity, cache miss
or missing response cannot establish the verdict.

For machine inspection, after a successful fetch, the following checks the
action result only. Use the typed protobuf interpretation of defaults; do not
read a `jq` null as an execution failure or as missing entire result:

```bash
jq -e '(.result | type == "object") and
       ((.status.code // 0) == 0) and
       ((.result.exitCode // 0) == 0)' "$scratch/response.json"
```

Retrieve `test.xml` from the digest in `result.outputFiles`, preserving the
execution's instance name and digest function in the bytestream URI. For the
SHA256, empty-instance actions here:

```bash
bbapi blob cat "bytestream://remote.buildbuddy.io/blobs/$hash/$size" \
  --invocation "$inner_id" > "$scratch/test.xml"
```

Review JUnit failures/errors/skips and the exact testcase names against intended
selection. A recovered TestRunner result does not replace missing other actions,
build/lint results, or the native CI job verdict. `bbapi invocation watch` can
reattach read-only to the existing invocation's logs, but correctly reports
`state: error`, `reason: disconnected`, `invocation_complete: false` for this
control even after its physical test result is recovered. Never launch another
`bbr` just to recover observation.

## Recovered identities and verdicts

Control inner invocation:
[`88924b1e-808c-4da9-89ec-3ac634268d6c`](https://app.buildbuddy.io/invocation/88924b1e-808c-4da9-89ec-3ac634268d6c).

```text
/uploads/5e382bf0-e39e-4a42-945a-f0c37a097494/blobs/69793b7d7d6c328c3265b7416e144ede38acd3877f81a5e300fe0ae8dfe909a3/1218
```

Its uncached ExecuteResponse has a result, OK status and exit code zero. JUnit
has 21 tests, zero failures/errors/skips, and 342.893 seconds of pytest suite
time. Its SHA256 and CAS identity are
`178735e860261c01896fc06759e5d8f907c95f5a0aadc95d3b56ebea949922e1/3778`.
The downloaded ExecuteResponse JSON is 111,979 bytes, SHA256
`07cab116790bc5dcbee5198960eacad3dc667f5d3e17868b8e126c74aeb0e7a3`.
JUnit URL:
`bytestream://remote.buildbuddy.io/blobs/178735e860261c01896fc06759e5d8f907c95f5a0aadc95d3b56ebea949922e1/3778`.

Corrected split main inner invocation:
[`fa944edd-c4a0-4c18-b576-1f0ac09d5e58`](https://app.buildbuddy.io/invocation/fa944edd-c4a0-4c18-b576-1f0ac09d5e58).

```text
/uploads/a89cd4d9-3c98-48b3-ac37-f0d0b85ad3d3/blobs/fddfd23561f8d122acbcfeaf04c644bc01ebb8f2aed8dd1be06c15a985d1808a/1218
```

Its uncached ExecuteResponse also has a result, OK status and exit code zero.
JUnit has 20 tests, zero failures/errors/skips, and 365.982 seconds of pytest
suite time. Its SHA256 and CAS identity are
`cbb70823bffe9952ba18c7b13823f44f77dfac4cc41764292c5192d237e0d991/3604`.
The downloaded ExecuteResponse JSON is 106,878 bytes, SHA256
`5a1c43e316a81ca767f23c9c275823876d50ff29780903375d2c901d29db78c3`.
JUnit URL:
`bytestream://remote.buildbuddy.io/blobs/cbb70823bffe9952ba18c7b13823f44f77dfac4cc41764292c5192d237e0d991/3604`.
The testcase set is exactly the control set minus
`test_pod_scenarios[test_a_cluster_ca_leaf_at_a_runner_address_is_refused_by_the_gateway]`.
The separately passed TLS action covers that case. This proves selection and
test-action verdicts, not a causal speedup or a completed combined CI gate.

## Observer and admission boundaries

An observer transport reset and terminal `Internal: stream closed with task still
claimed` have different meanings. The inspected upstream scheduler's
[`LeaseTask`](https://github.com/buildbuddy-io/buildbuddy/blob/879c2cdfc08b7d432394c3eea7b2fd6e41328cbf/enterprise/server/scheduling/scheduler_server/scheduler_server.go#L2412)
uses that message when its executor lease stream closes while claimed.
Its non-retryable-task branch can mark execution failed with that reason.
This traces a provider-side terminal failure path, not just a lost client log
stream; the hosted server revision, logs and flags are unobserved.

The existing upstream
[`WaitExecution` handler](https://github.com/buildbuddy-io/buildbuddy/blob/879c2cdfc08b7d432394c3eea7b2fd6e41328cbf/enterprise/server/remote_execution/execution_server/execution_server.go#L1287)
subscribes to updates for an existing operation. It does not call Execute.
The
[`cached response reader`](https://github.com/buildbuddy-io/buildbuddy/blob/879c2cdfc08b7d432394c3eea7b2fd6e41328cbf/server/util/rexec/rexec.go#L446)
reads ActionCache; the existing `bb` command already provides the needed terminal
recovery, so adding a parallel `bbapi` implementation is unnecessary.

A fresh split's original main action provided a live attachment check after
its CLI log stream reset with `RST_STREAM INTERNAL_ERROR`. Two independently
opened WaitExecution streams at 23:26:29.300409934 and 23:26:56.964257270 UTC
returned `EXECUTING`, `done: false`, with the exact requested operation name:

```text
/uploads/e930ed58-d4b1-4bea-83e8-c67ccab08ade/blobs/d307ae30b6f8be8aeb58eaa0214bbbdcb7c8e3a290763809c59b42411ad18a6e/1218
```

Each observer read one update then closed its local context. Subsequent
GetExecution for inner invocation
[`5fda9605-f7bd-4c04-9581-b3dab7a9f9fa`](https://app.buildbuddy.io/invocation/5fda9605-f7bd-4c04-9581-b3dab7a9f9fa)
still listed the same two physical TestRunner IDs: this main action and the
TLS action, which had completed. The diagnostic sent only WaitExecution and
did not resubmit or cancel either action. This validates reattachment, not
ongoing physical progress or a terminal verdict; an `EXECUTING` stage alone
does not establish how long the worker has been actively executing.

A third read at 23:30:51.145357880 UTC returned the same operation name with
`COMPLETED`, `done: true`, a result, status zero and exit code zero. That action's
subsequently recovered worker clocks show physical execution ended at 23:29:25.061
and output upload at 23:29:27.276. The earlier indexed `EXECUTING` observations
could outlast physical execution; they did not establish a nine-minute active
worker wedge. The live check used a temporary Bazel-built Go diagnostic, not a
new command already present in `bbapi`.

The control's 536.823-second queue and the malformed split main's 25.7-ms queue
are still valid admission measurements. Current upstream
[`task routing`](https://github.com/buildbuddy-io/buildbuddy/blob/879c2cdfc08b7d432394c3eea7b2fd6e41328cbf/enterprise/server/scheduling/task_router/task_router.go#L191)
ranks preferred hosts and samples others by CPU weight; scheduler reservations
can delay nonpreferred workers. Recyclable runner routing includes platform,
group and instance, plus branch for CI runners. This supplies plausible mechanisms
for divergent admission, not proof of which mechanism affected these actions.
The responses expose requested task sizes and chosen workers, not rejected
reservations, eligible fleet capacity, account entitlements or hosted routing
decisions. Provider lease/disconnect logs and reservation/affinity traces for the
exact execution IDs are needed to close those causal gaps.
