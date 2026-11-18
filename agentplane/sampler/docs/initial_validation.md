# Sampler launch evidence

The runs below used scripted providers with real PostgreSQL and pinned Codex host/executor binaries.
They establish the listed behaviors for the tested generations. Live provider and subscription acceptance,
deployed Responses Lite compaction, and production resource isolation require separate evidence.
The [sampler contract](../SPEC.md) and [verification instructions](../README.md#verification) cover the current service.

## Postgres Codex prototype

The retired prototype committed provider, JavaScript and nested-tool intents before dispatch. A lease generation
fenced stale writes. One lost-reply probe appended a real file marker, then dropped the reply. A successor
found the unresolved cell and the marker remained present exactly once. Stream truncation after a completed
tool item left sampling unresolved without dispatching the cell. Sampler and executor SIGKILL preserved
the corresponding unresolved intents. The positive restart case continued with committed opaque reasoning.
The seven baseline cases used PostgreSQL and Codex 0.162.1, with the same scripted SSE also consumed by
native app-server. [Baseline run](https://app.buildbuddy.io/invocation/8a1a2810-b81f-45ea-b971-ad6cec41e15a)

- A yielded cell was tied to its original host. Native cell IDs restart at `1`, so the prototype qualified
  them by host identity. Replaying nested results cannot restore a V8 isolate. Process IDs were scoped to the
  recorded executor session. The pinned server retained a detached session for 30 seconds and rejected a
  second attached connection with `-32010`. The restart fixture detached its original socket before
  resuming the same session and sending real stdin. Real binaries interleaved `process/output`,
  `process/exited` and `process/closed` hints with replies. Output came from `process/read` using its
  exclusive `nextSeq` cursor.
  [Attached-session failure](https://app.buildbuddy.io/invocation/105393d7-2865-45b6-a2e5-458631dd4b1a)
  [Resume and compaction run](https://app.buildbuddy.io/invocation/8e92aa72-d60a-439a-9f54-3d7412881fd2)
- A control case interrupted after native execution began but before the live-cell registry write.
  Observed host exit closed that window. Other successful cleanup cases observed exit of known shells and
  children. Local HTTP cancellation left provider completion unknown, and the process checks covered only
  known supervised processes. A committed interrupt gated dispatch, and uncertain cleanup retained a
  blocking intent. [Control, interrupt and schema run](https://app.buildbuddy.io/invocation/5dd30dcf-e2c5-4aa2-b4a3-12c86b470a80)
- The scripted remote-v2 fixture sent a `response.output_item.done` compaction item followed by completion
  without an output array. The prototype preserved the complete opaque item, reduced serialized active context,
  and resumed real tools and provider continuation in new processes while retaining the original conversation.
  Terminal failures and rejected summaries preserved usable prior context. Truncation and cancellation
  could leave the intent unresolved and block unattended advance. Lease loss fenced installation, and
  live cells and orphaned calls/results prevented dispatch. Tests rejected tool-returning, nonshrinking
  and duplicate summaries. The cached native regression used the local summary path.
  [Prototype compaction run](https://app.buildbuddy.io/invocation/f84ec0d4-a55f-44d8-b7ad-7f702cb87bd5)
  [Native local-summary run](https://app.buildbuddy.io/invocation/8aca6e91-bd97-4f2a-8836-0bd1c82894d8)

The integrated prototype passed 38 cases across five targets after correcting the attached-session fixture
(20 baseline/control/interrupt/schema and 18 compaction). A previous final-control run timed out on two
targets. Both passed in a streamed rerun that captured PostgreSQL image loading during fixture setup.
[Timed-out run](https://app.buildbuddy.io/invocation/565aa96e-ae4f-490a-aad7-04a220edd5a3)
[Streamed rerun](https://app.buildbuddy.io/invocation/e1991210-f0dc-4cb1-a1d2-8b324e18f7e3)

The prototype grew from 563 physical lines of Python
runtime to 1,501 with controls and compaction, excluding reused Codex binaries, BUILD, SQL and docs. No
persistence fork was built or measured. The native remote-v2 path retained selected goals and developer
context beyond the prototype's opaque replacement item. The prototype supplied its own standing instructions,
tools and handles on each request. PostgreSQL JSONB normalized formatting and number spelling.
[Prototype source and contract](https://github.com/agentydragon/ducktape/tree/58e4261128f61235dd354192a907447882284ceb/agentplane/x/postgres_codex)
[Full spike record and cost breakdown][spike]
[Original compaction record][compaction]

## Graceful shutdown lease regression

A Python service restart stalled before the next provider request. Shutdown cancellation raced with
the database after claim committed and while release was awaiting the database. In both deterministic
PostgreSQL probes, immediate takeover of the stranded lease raised `LostOwnershipError`. The original restart
and both probes passed when shutdown let ownership transitions finish. Abrupt host loss still depended on
expiry and unresolved-effect recovery. Current Go coverage includes
[`TestRuntimeShutdownWaitsForClaimReturnAndReleasesOwnership` and `TestRuntimeShutdownWaitsForReleaseReturn`](../internal/residentruntime/runtime_lifecycle_test.go).
[Restart failure](https://app.buildbuddy.io/invocation/6195e2a1-c114-51d5-b40d-dea24592409a)
[Failing probes](https://app.buildbuddy.io/invocation/95659374-fec7-4338-a236-6ec43bca4f95)
[Passing probes and restart](https://app.buildbuddy.io/invocation/8389b40d-3eb8-4bb5-92e8-be5cc207a1ae)

## Go service with a per-Session host

The [initial Go service record][v1] precedes the shared-host runtime. The native suite exercised real
PostgreSQL lease fencing, shutdown races, interrupt cleanup, numeric precision above 2^53, provider framing
and History export/reconnect. Scripted k3s acceptance exercised Dispatcher → sampler → Gateway → pinned
exec-server → retained and folded History, with the Go binary started on the test node. These runs covered
neither inter-Session host isolation nor the sampler Pod's identity and readiness.
[Go suite](https://app.buildbuddy.io/invocation/4257efda-6967-4d64-a782-93fc4bd193b3)
[k3s and Go race suite](https://app.buildbuddy.io/invocation/61474059-c6d8-40fd-b2e5-875b7df97952)

Standalone Go acceptance exposed two persistence defects. Compaction needed to settle the active command in
the context replacement transaction. A successor needed to consume retained terminal process output from
PostgreSQL while stale remote process mutations stayed fenced. The follow-up passed both PostgreSQL regressions
and standalone API acceptance for wait/stdin, queuing, restart, interruption and uncertainty recovery.
[Failing cases](https://app.buildbuddy.io/invocation/b21d8c03-ebe9-4b2e-bb21-8c0960928970)
[Passing regressions and acceptance](https://app.buildbuddy.io/invocation/3b12753c-62e1-40b7-9983-b34d61ebe74f)
[Current compaction settlement test](../internal/sessionstore/integration_test.go)
[Current retained terminal output test](../internal/executor/executor_test.go)

## Shared host and k3s fixture

The later Go fixture runs the sampler as a Pod with its own image, service account, init container and gRPC
readiness probe. Before that change, the VM coverage could fail at `ErrImageNeverPull` on the separate identity
Pod because it depended on the Runner Pod image. Earlier passes with that image available left the fixture's
image-input closure untested. [Failing invocation](https://app.buildbuddy.io/invocation/36453d32-4bd0-5347-9662-41ae84180c2b)
[Fixture change](https://github.com/agentydragon/ducktape/commit/dd2556caf1eb0fdef8fcc4900258ef4daf0da411)

- In the VM startup investigation, the first provider request never arrived. One run captured lazy exec-server
  readiness after the sampler's ten-second setup RPC deadline. A follow-up with a distinct startup context still
  hit the relay's roughly twenty-second readiness deadline and observed no readiness before teardown. That
  evidence could not establish that increasing the deadline would suffice.
  [First timing failure](https://app.buildbuddy.io/invocation/a736ad69-e27c-46c9-b150-dbfd3dffb5ba)
  [Second timing failure](https://app.buildbuddy.io/invocation/736ab387-4f84-41b7-bdc1-6569d508533a)
- With Gateway-owned readiness cancellation, the Runner race suite and Pod/VM scenarios passed. The VM report
  recorded PID 1 `systemd`, executor UID 107 under `agentplane-executors.slice`, successful setup, and
  `terminal_cursor == folded_through == 163`. This was one scripted-model pass of the VM scenario with real
  execution and History, so the frequency of the earlier startup failure remains unknown.
  [Passing invocation](https://app.buildbuddy.io/invocation/76472dd5-4a0a-49d7-9981-0dc71dd9db05)
- A separate VM invocation read the setup file and retained `setup_finished` with exit code 0, but retained
  setup stdout was empty instead of `setup-on-executor`. The Pod variant passed. The failed run lacked the
  `process/read` response bodies needed to localize the loss. The [scenario](../../system_tests/scenarios/central_sampler.py)
  now captures retained History events, including boundary requests and replies, in
  `central-sampler-session-diagnostics-<session-id>.json` on this assertion.
  Compare the retained executor replies with sampler evidence before attributing the loss.
  [Stdout failure](https://app.buildbuddy.io/invocation/1f9fca25-3dec-528a-96d0-9fdcff5b2c67)
- The original sampler blocked above 1 MiB of unread output. A forced-overflow regression demonstrated that
  behavior without measuring live incidence. Codex 0.162.1 used bounded head/tail retention and an omission
  marker. The exec-server's replay buffer separately evicted oldest chunks past 1 MiB or 50,000 chunks.
  The [current sampler's output contract](../SPEC.md#runtime-and-effects) retains the head and tail
  and tracks exit independently. [Original comparison][v1]

[v1]: https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/central_sampler_v1.md
[spike]: https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/postgres_codex_spike.md
[compaction]: https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/postgres_codex_compaction.md
