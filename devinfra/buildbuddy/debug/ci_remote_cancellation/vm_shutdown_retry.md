# CLI retry during the VM shutdown probe

Observed 2026-10-27. This is transport-retry and invocation-identity evidence
from the [VM shutdown investigation](../../../../agentplane/system_tests/k3s/debug/vm_shutdown.md#evidence).
The VM lifecycle findings and selected TestRunner artifact identity remain there.

Baseline source: `51542844ae8d9d18e71cab407b288ec309f80cbf`. The probe ran
`//agentplane/system_tests:test_vm_sandbox_vm` through `bbr`, selecting the
Claude smoke case with uncached test results. This is the historical target name; the current VM
suite target is `//agentplane/system_tests/suites:test_vm`.

The CLI retried after a log-stream `RST_STREAM INTERNAL_ERROR`, reusing the
[Bazel invocation ID](https://app.buildbuddy.io/invocation/c7d1affd-a5eb-44f0-b09b-bf63ae1dcae4).
These outer runs were not independent manual submissions:

| Attempt   | Outer runner invocation                                                                                           |
| --------- | ----------------------------------------------------------------------------------------------------------------- |
| First     | [1a92557f-ddde-49f6-b592-39f786569728](https://app.buildbuddy.io/invocation/1a92557f-ddde-49f6-b592-39f786569728) |
| CLI retry | [5e99e4f5-636f-46a9-a5be-b91d3859ca04](https://app.buildbuddy.io/invocation/5e99e4f5-636f-46a9-a5be-b91d3859ca04) |

Invocation-level target indexes can therefore mix or replace attempt evidence.
Correlate each execution ID and outer attempt separately. Retrieve a retained
worker result with `bb execution get <execution-id> --output=json` even when
retrying has replaced the invocation's target index.

The original VM note does not contain a cancellation-acknowledgment timeline.
This retry observation does not establish failed cancellation, post-cancellation
dispatch, or the hosted lease duration. It is distinct from the 20:28 UTC
[continued-action incident](investigation.md#incident-2026-10-27-vm-test).

## Full-suite replay on 2026-10-31

A `bbr --script` benchmark ran a warm build and then fresh full-suite test
passes with explicit child invocation IDs. The CLI's log observer received
`RST_STREAM INTERNAL_ERROR` while the first outer runner had already accepted
and run work. It retried the whole script under a second outer invocation,
reusing the same child IDs and starting another full-suite pass. Canceling only
that outer invocation while the local CLI stayed alive triggered a third outer
attempt (`invocation cancelled` was classified as retryable). The local launcher
was then stopped before the active outer and child invocations were canceled.

| Attempt                        | Outer runner invocation                                                               |
| ------------------------------ | ------------------------------------------------------------------------------------- |
| Original                       | [ab9396a7](https://app.buildbuddy.io/invocation/ab9396a7-c8ef-4d1c-ad96-506c0472d412) |
| Observer-error retry           | [bed0fc63](https://app.buildbuddy.io/invocation/bed0fc63-54f3-463d-97d8-c77fbcf2e544) |
| Retry after outer cancellation | [6bd05016](https://app.buildbuddy.io/invocation/6bd05016-b08f-4085-bd6c-6d14f1dcf896) |

The outer invocations above and child [baseline test](https://app.buildbuddy.io/invocation/7ea29197-b0ed-467f-adff-ee1720988897)
and [candidate test](https://app.buildbuddy.io/invocation/88529384-7bc3-4ce7-b241-1576b288bd9f)
show the repeated work. After cleanup, both active child IDs had only terminal
execution records. This establishes the replay mechanism, not a latency
estimate for normal CI. It is a **bb CLI whole-run retry**, separate from the
[Bazel Execute RPC retry](rpc_retry_trace.md) controlled by Bazel's
`--remote_retries`; changing that Bazel option would not prevent this replay.

## Log transport evidence, 2026-10-31

Three retained GitHub job logs contain the same complete error:

```text
rpc error: code = Internal desc = streaming logs: read log stream: stream terminated by RST_STREAM with error code: INTERNAL_ERROR
```

| Job                                                                                                | Error timestamp (UTC, 2026-10-30) | Original outer invocation              |
| -------------------------------------------------------------------------------------------------- | --------------------------------- | -------------------------------------- |
| [124312184823](https://github.com/agentydragon/ducktape/actions/runs/41128236821/job/124312184823) | 21:16:53.3200805                  | `a16a58c3-f5a8-4d0a-9605-c162f7a93705` |
| [124318119119](https://github.com/agentydragon/ducktape/actions/runs/41129977412/job/124318119119) | 21:33:20.7453461                  | `b840a7e7-6a7d-4662-b01a-503dc573fac9` |
| [124330549416](https://github.com/agentydragon/ducktape/actions/runs/41133628417/job/124330549416) | 21:57:03.2883697                  | `7d8c0f28-42f1-4975-8dbc-ca209a54445b` |

Each job used Nix's `bb-5.0.466`, before the 5.0.498 repin. Go build
metadata from that retained binary reports grpc-go `v1.83.2` and
`golang.org/x/net v0.59.0`; 5.0.498 uses grpc-go `v1.84.0` and
`golang.org/x/net v0.60.0`. Both grpc-go versions construct this description
in the HTTP/2 client's `handleRSTStream`: the peer sent HTTP/2
`INTERNAL_ERROR` (`0x2`), which grpc-go maps to gRPC `Internal` (`13`).
See [the 1.83.2 reset handler](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http2_client.go#L1270-L1295)
and [status mapping](https://github.com/grpc/grpc-go/blob/v1.83.2/internal/transport/http_util.go#L54-L69).

This is a client-generated transport status, not evidence of a BuildBuddy
application exception returned in gRPC trailers. The reset status itself has
no structured error details. The retained CLI error lines expose no request
ID, trace ID, server stack, or explanation for the reset. They do not identify
whether the application server or an intermediary generated it, and do not
establish that the entire connection failed.

The 5.0.498 CLI follows logs using the server-streaming `GetEventLog` RPC.
Its [read-only reconnect path](https://github.com/buildbuddy-io/buildbuddy/blob/5b4fc7a4ccb0d814ec93f8ed0daae7f0325c7f4f/cli/remotebazel/remotebazel.go#L630-L699)
handles `Unavailable` only; this `Internal` escapes to the outer retry path.
The source revision is tied to the CLI release by BuildBuddy's successful
[release workflow](https://github.com/buildbuddy-io/buildbuddy/actions/runs/40406832098).
The hosted server's deployed revision is unknown.

The upstream [GetEventLog handler](https://github.com/buildbuddy-io/buildbuddy/blob/5b4fc7a4ccb0d814ec93f8ed0daae7f0325c7f4f/server/buildbuddy_server/buildbuddy_server.go#L1871-L1938)
has a five-minute canary that logs `invocation_id`, `duration`, and
`bytes_sent`. Correlating those application logs and the serving proxy's
HTTP/2 reset reason at the timestamps above is the next useful upstream
diagnostic. We do not have those hosted logs. Client debug capture should
record allowlisted response metadata, numeric status and details, cursor,
and stream lifetime; it should never dump authorization metadata or start
another run to recover observation.

## Retained log volume and EROFS

The [census](event_log_census.json) records eight selected invocations, not a
fleet failure rate. It was collected on 2026-10-31 by reading `GetInvocation`,
then `GetEventLogChunk` with explicit cursor `0000` and following each
returned `nextChunkId` until empty. All eight traversals reached the end
within the diagnostic's 64-chunk / 16 MiB read budget. Sizes count decoded
retained buffers and LF characters. Live streams can resend growing buffers,
so these are not wire-byte totals. Metadata `lastChunkId` sometimes lagged
the final data chunk; the census preserves both.

| Invocation                                                                                                            | Log bytes | LF lines | Nonempty chunks |                    EROFS `Processing` lines |
| --------------------------------------------------------------------------------------------------------------------- | --------: | -------: | --------------: | ------------------------------------------: |
| [Oct 27 replay, `1a92557f`](https://app.buildbuddy.io/invocation/1a92557f-ddde-49f6-b592-39f786569728)                |    26,159 |      214 |               3 | Not attributed; four tiny matching segments |
| [Oct 30 reset, `a16a58c3`](https://app.buildbuddy.io/invocation/a16a58c3-f5a8-4d0a-9605-c162f7a93705)                 |   486,169 |    8,149 |              11 |                                       7,984 |
| [Oct 30 reset, `b840a7e7`](https://app.buildbuddy.io/invocation/b840a7e7-6a7d-4662-b01a-503dc573fac9)                 |   501,006 |    8,281 |              30 |                                       7,984 |
| [Oct 30 reset, `7d8c0f28`](https://app.buildbuddy.io/invocation/7d8c0f28-42f1-4975-8dbc-ca209a54445b)                 |   485,458 |    8,139 |              13 |                                       7,984 |
| [Oct 31 replay after quieting, `ab9396a7`](https://app.buildbuddy.io/invocation/ab9396a7-c8ef-4d1c-ad96-506c0472d412) |   213,336 |    2,011 |              17 |                                           0 |
| [Full `//...`, 50 jobs, `0c774d6a`](https://app.buildbuddy.io/invocation/0c774d6a-22e7-4fc5-80a3-76901f297bc6)        |   174,869 |    1,767 |              13 |                                           0 |
| [Routing tests, `856dbe85`](https://app.buildbuddy.io/invocation/856dbe85-5d71-594c-acb0-ad16366407aa)                |    12,608 |       90 |               3 |                                           0 |
| [Tracing tests, `21baa91d`](https://app.buildbuddy.io/invocation/21baa91d-9f05-549e-a08b-6d6d27f45ee8)                |    11,143 |       74 |               3 |                                           0 |

In each of the three Oct 30 reset samples, `mkfs.erofs 1.8.6` output from
`disk_erofs` and `root_erofs` accounts for 7,984 `Processing` lines and
461,777 bytes: 92.2–95.1% of retained log bytes. The EROFS rule landed in
PR #11321 on Oct 21 UTC; `--quiet` landed in
[PR #13098](https://github.com/agentydragon/ducktape/pull/13098) on Oct 30
at 21:48:27 UTC. A PR running after that merge can still use older source.
Quieting removed this noise from the selected later logs, but the Oct 31
replay still happened. High log volume is therefore not a necessary
condition; these measurements do not rule out volume aggravating a transport
problem.

The earliest retained documented replay is the Oct 27 invocation above,
created at 23:22:17.253 UTC. That is an observation bound, not a proven
first-ever incident. The small log and the earlier CLI version both argue
against attributing onset to the recent EROFS quieting or CLI repin.

## The separate `ffff` cursor error

BuildBuddy chunk IDs are four-digit hexadecimal uint16 values. Data IDs are
`0000` through `fffe`; `ffff` is
[`EmptyIndex`](https://github.com/buildbuddy-io/buildbuddy/blob/5b4fc7a4ccb0d814ec93f8ed0daae7f0325c7f4f/server/backends/chunkstore/chunkstore.go#L17-L21),
the sentinel for no persisted chunk yet. An invocation is initially stored
with that last-chunk sentinel. Requesting it as an explicit data cursor
triggers [gRPC `ResourceExhausted`: `Log index limit exceeded`](https://github.com/buildbuddy-io/buildbuddy/blob/5b4fc7a4ccb0d814ec93f8ed0daae7f0325c7f4f/server/eventlog/eventlog.go#L171-L189).
That response does not mean the invocation produced 65,535 chunks.

The `bbapi` watcher could seed that invalid explicit cursor from invocation
metadata; [PR #13288](https://github.com/agentydragon/ducktape/pull/13288)
adds a bounded read-only recovery from `0000`. This is distinct from the
`bb remote` HTTP/2 reset and accepted-run replay in
[issue #13270](https://github.com/agentydragon/ducktape/issues/13270).
The selected logs contain at most 30 nonempty chunks, far below the index
limit. A default two-megabyte write block is not a hard byte capacity:
flushes can persist much smaller chunks, and no-split writes can be larger.

A further source-level suspect for stale terminal metadata is close-time
flush: the [writer's result reader](https://github.com/buildbuddy-io/buildbuddy/blob/5b4fc7a4ccb0d814ec93f8ed0daae7f0325c7f4f/server/backends/chunkstore/chunkstore.go#L406-L445)
returns on `result.Close` before copying `LastChunkIndex`, although close
can flush a residual chunk. A later getter short-circuits because the writer
is closed. A bounded regression would buffer `abc` below the block size,
observe `ffff`, close, verify blob `0000` contains `abc`, and assert the
reported last index becomes `0000`. This test has not been run against
upstream. It does not establish the exact metadata/attempt transition of
the observed tracing invocation, whose later metadata was `0001`.

## Outside GitHub Actions and the timeout hypothesis

The Oct 31 `ab9396a7` incident was launched by this session's local Nix
devshell `bbr` process, outside GitHub Actions. The archived local
`concurrency/interrupted-attempt/pair.log` contains the complete `Internal`
reset at line 3971, followed by the new `bed0fc63` outer invocation. Its
448,722-byte file has SHA-256
`47076dce03fc241acd7a4d44dc31fcb7492d31109facf38b3fc9aa73ce8a0072`.
The [transport evidence](log_transport_probe.json) preserves the error and
invocation identities. This was an incidental failure during the full-suite
experiment, not a deliberate reproduction of the transport failure.

For the three GitHub examples, the client-side `Waiting for available remote
runner...` line gives the following elapsed times to the error. These include
queue/startup time and do not identify the lifetime of the final `GetEventLog`
RPC, which may have reconnected internally.

| Job            | Waiting marker (UTC, Oct 30) | Reset error (UTC) |   Elapsed |
| -------------- | ---------------------------- | ----------------- | --------: |
| `124312184823` | 21:07:28.973                 | 21:16:53.320      | 564.347 s |
| `124318119119` | 21:18:54.568                 | 21:33:20.745      | 866.178 s |
| `124330549416` | 21:51:09.667                 | 21:57:03.288      | 353.622 s |

The visible `Streaming remote runner logs to` line is itself produced by the
[runner's build-event reporter](https://github.com/buildbuddy-io/buildbuddy/blob/5b4fc7a4ccb0d814ec93f8ed0daae7f0325c7f4f/enterprise/server/cmd/ci_runner/main.go#L468-L480)
and stored in the log. It can be buffered or replayed; it is not a reliable
client RPC-attachment timestamp. Noninteractive `bb` deliberately skips live
chunks until finalized. Neither silence in GitHub output nor intervals
between repeated log-start lines establish a transport-idle timeout.

### Controlled low-volume probe

One local probe used the actual pinned `bb` 5.0.498 against
`remote.buildbuddy.io:443`, with system-trust TLS, `--disable_retry=true`, a
16-minute outer timeout, and `EstimatedComputeUnits=1`. The script printed a
start marker, three heartbeats 60 seconds apart, slept for 360 seconds, then
printed three more heartbeats 60 seconds apart and a completion marker. It
did not run a full test suite. Keys came from the existing environment and
were not printed or included in diagnostic arguments.

The [outer invocation](https://app.buildbuddy.io/invocation/4524703c-ee24-4449-84fd-9b5041f358a2)
completed successfully. Its script ran from 11:29:55.315 to 11:41:55.329 UTC;
the quiet interval was 11:32:55.322–11:38:55.323. Command and local CLI exit
codes were zero. Thus this single 12-minute run did **not** reproduce the
reset.

Two read-only, Bazel-built Go observers called `GetEventLog` on the same
endpoint without reconnecting or submitting work. They recorded timestamps,
cursor/live flags, byte counts, and metadata names, without printing payloads
or authorization metadata. Their continuous RPC lifetimes were 572.242 and
448.114 seconds, ending in normal `io.EOF`. The first received 162 messages
during the whole six-minute producer silence; its maximum response gap then
was 3.136 seconds. The second observer's grpc-go `InPayload.WireLength` summed
to 334,523 bytes, including repeated live buffers, not HTTP/2 framing or TLS
overhead. It exposed response header names `alt-svc`, `content-type`, `date`,
`server`, `via`, and `x-envoy-upstream-service-time`, with no trailers.

This confirms that producer silence did not make the gRPC streams idle:
unchanged live buffers still arrived about every three seconds. The
observers attached after the original CLI, so their RPC lifetimes are shorter
than the 12-minute script. This does not rule out other duration thresholds,
load-dependent failure, server events, or different routes. The diagnostic's
generic `status.Convert(io.EOF)` rendered `Unknown`; that is not a server
error and must not be counted as a failed RPC.

At 11:35:17 UTC, the invocation's attempt changed to 2 and the log cursor
restarted, while both observer RPCs remained open and the same script
continued. The new attempt row was created at 11:35:16.950 and updated at
11:35:17.075. The upstream
[build-event publisher](https://github.com/buildbuddy-io/buildbuddy/blob/5b4fc7a4ccb0d814ec93f8ed0daae7f0325c7f4f/server/build_event_publisher/build_event_publisher.go#L62-L147)
retries a failed publishing stream and replays buffered events; this is
consistent with the observed transition. The producer's initiating error
has not been obtained. The completed action's 24,077-byte stderr was
downloaded using its full `bb-snapshot` instance and BLAKE3 resource path;
it contains no retry, stream, gRPC, reset, or error diagnostic. Its SHA-256
and CAS URI are in the transport evidence. A bare `HASH/SIZE` download had
incorrectly reported a miss because it omitted that namespace and digest
function. This is not a reproduced observer `RST_STREAM` or a new whole-run
submission.

The source defaults do not establish a five- or ten-minute timer:
`GetEventLog` sends updates on a three-second fallback; its five-minute
canary only logs. Native gRPC connection aging defaults to disabled, and the
HTTP server literals set no write timeout. Hosted flags, proxy configuration,
and server logs remain unknown. No reset trigger or fixed timeout has been
identified.
