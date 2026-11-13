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

## PR #15995 CI stream reset after a passing Firecracker action

The exact-head [CI invocation](https://app.buildbuddy.io/invocation/00947ae1-95ef-594d-9177-f5e4934b63d2)
for [PR #15995](https://github.com/agentydragon/ducktape/pull/15995) reported
`INTERNAL: stream terminated by RST_STREAM with error code: INTERNAL_ERROR`
for `//agentplane/system_tests/suites:test_firecracker`. Bazel published no
`TestResult` for that target, and the [native CI job](https://github.com/agentydragon/ducktape/actions/runs/43348327584/job/131632971905)
remains red. `//agentplane/app:test_thread_browser` passed all 16 shards in the
same invocation; the Go auth unit and session integration targets also passed.

Read-only `bbapi execution wait` and `bb execution get` on this exact operation
returned a completed result with RPC status zero and action exit code zero:

```text
/uploads/8d54c921-60a5-4979-8f97-9d15861e8635/blobs/0351ffe6233de0b4721791ae02d435d2cc7833b0d994e7359b8a5f2cbdfb02e7/593
```

Its retained `test.xml` is
`bytestream://remote.buildbuddy.io/blobs/70e9943f59ab026ae586dc5a38f2ae5b8f995070323cdae6da49a77014dbe145/548`.
The JUnit reports two tests, zero failures, errors or skips:
`test_firecracker_preserves_live_guest` and
`test_a_cluster_ca_leaf_naming_the_gateway_is_refused_by_the_guest_runner`.
The physical action passed, while Bazel lost its response stream and the whole
gate stayed red. No action was replayed or resubmitted to recover this evidence.

## Native Bazel accepted-operation control, 2026-11-13 UTC

[Tracking issue #16025](https://github.com/agentydragon/ducktape/issues/16025)
keeps the natural false-red result-delivery failure open. The pinned
[Bazel 9.2.0 implementation](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/remote/GrpcRemoteExecutor.java)
already changes from Execute to WaitExecution after receiving a nonterminal
Operation. Before its first Operation it retries Execute; NOT_FOUND during
observation also clears the operation. Thus adding another reattachment
implementation or blindly increasing `--remote_retries=1` would not establish a
repair and could weaken the deliberate cancellation replay bound.

A bounded [loopback diagnostic proxy](../diagnostics/execution_stream_fault/README.md)
forwards to the real hosted execution service and interrupts a stream only after
forwarding a nonterminal Operation. It does not submit work independently. With
Bazel 9.2.0 and `--remote_retries=1`, the
[first native control](https://app.buildbuddy.io/invocation/a2ad40aa-df96-4908-ad6a-4107e8a0e007)
passed its one remote TestRunner in 3.330 s. Its native gRPC log records one
Execute response followed by status INTERNAL (13), then one WaitExecution of
that same operation, six updates, and a terminal result with RPC status zero.
The exact execution ID was:

```text
/uploads/dc3cacb0-4087-44cf-a269-9f0db5969c5c/blobs/1f8794c11804e2d2c91f9b3547e6ff6f9f899e4d069de6ce5cda9333595b838d/499
```

This is the only physical TestRunner ID for that target; the other 833 Execute
calls were build actions returning completed responses. There was no Execute
replay for the interrupted TestRunner. The first Go log reported PASS but its
non-verbose JUnit was empty, so it supplies an action/transport verdict, not a
case count. A
[second explicit-case control](https://app.buildbuddy.io/invocation/fef8a50b-86bc-4c73-82a4-212fbf9c9bb9)
used `-test.v -test.run=TestExecutionWaitCommandCredentialsAndOutput`; its JUnit
contains that one passing case, zero failures/errors/skips. The proxy again saw
exactly one Execute, one WaitExecution, and a terminal update for the same ID:

```text
/uploads/1f0b5501-a8d4-4e35-bed4-ce86a42f0a71/blobs/7eb9bfff3ffb57b1e132fe36a5828f349e754f12ccc13552ae8afe04532f6a3c/501
```

These controls reject an unconditional absence of native reattachment. They do
not reproduce the natural Firecracker failure: the proxy returns gRPC INTERNAL,
not a literal HTTP/2 RST_STREAM, and the original invocation did not retain a
native RPC log. Both controls use effective source `4415ac2544` plus only the
scoped diagnostic tool; native Bazel and the tested bbapi code were unchanged.
Raw native logs stay private, mode 0600. Only reviewed operation/status/counts
are published. The next natural recurrence needs narrowly enabled native
`--remote_grpc_log` retained privately and summarized for Execute/WaitExecution,
first Operation, statuses and retry consumption; compare that sequence with the
exact hosted execution/provider trace before changing retry or recovery policy.

A [two-reset control](https://app.buildbuddy.io/invocation/1a459975-abdb-4670-bed6-378beeb40d40)
then interrupted Execute after its first nonterminal Operation and interrupted
WaitExecution after its first update. Native Bazel exhausted the existing
one-retry budget and reported Exit 34, INTERNAL, no TestResult and `NO STATUS`.
The proxy recorded exactly one Execute and one WaitExecution, both naming:

```text
/uploads/d1365bec-2964-46bb-8ec8-32b1283637ec/blobs/7eb9bfff3ffb57b1e132fe36a5828f349e754f12ccc13552ae8afe04532f6a3c/501
```

Read-only `bb execution get` recovered that same action with a real result,
default OK status and zero exit code. JUnit
`967ca6b68eb1e65893060afb71ea783b322743223e60cf4fe874ebb5c56c0f46/367`
contains the one intended passing Go case, zero failures/errors/skips. No action
was resubmitted for recovery. This establishes one sufficient mechanism for a
physically passing action and a red native gate: two observer interruptions can
exhaust the deliberate retry cap. It does not establish that two interruptions
occurred in PR #15995; its missing native RPC sequence is still the boundary.
A pre-first-Operation reset was not injected into a live accepted action: native
source already demonstrates its Execute retry path, and the needed discriminator
for this incident is the observed operation/retry sequence, not more submission.
