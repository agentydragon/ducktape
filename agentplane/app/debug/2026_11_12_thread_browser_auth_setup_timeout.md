# Thread browser authentication setup timeout under 100 CI jobs

## Observed boundary

At source `28dae5ef3`, [BuildBuddy invocation `221d60ab`](https://app.buildbuddy.io/invocation/221d60ab-b324-4cf9-9d74-aabeae174008) ran the broad CI experiment with 100 jobs. Shard 4 of `//agentplane/app:test_thread_browser` reported two passing cases and one setup error. The error was `httpx.ReadTimeout` while `AppProcess.authenticate()` awaited `GET /auth/login` for `test_small_upward_scroll_stays_detached_when_tail_streams`. It was not a failure of that test's scroll or thread-projection assertions. The `test.xml` timestamps show 35.949 seconds for the preceding case, 10.834 seconds for the failing setup, and 13.549 seconds for the following passing case; these are case durations, not HTTP phase timings.

The fixture starts a local mock OIDC provider, starts the Go app, confirms `/readyz`, then uses an `httpx.AsyncClient` with redirects enabled to request `/auth/login` and `/auth/me`. The Go login route lazily discovers the provider, redirects to `/authorize`, and the callback exchanges a code at `/token`. The traceback identifies the initiating `login.get()` and a response-header read timeout, but does not identify which request in that redirect chain was waiting. Go app logs for this shard contain migration start/completion only. The mock provider has no retained request log. The failing case's Playwright trace contains an empty `trace.network` and an `about:blank` page, so browser activity cannot locate the stalled HTTP hop.

The shard's ingress container log records a successful `/auth/login` → `/auth/callback` → `/auth/me` sequence at 10:20:22 UTC. The test XML places the failing middle case earlier, between a 35.949-second first case and a 13.549-second third case in a 61.230-second session starting at 10:19:27.649 UTC. The ingress sequence therefore cannot be assigned to the failed request. It does demonstrate that another login completed in the same shard. The Go app's readiness success establishes a listening process, not OIDC discovery or session-store responsiveness.

## Visibility limit and next discriminator

The retained artifacts do not determine whether the delayed response was app-side session storage, provider discovery, mock `/authorize` or `/token`, callback exchange, or scheduling contention. The 100-job setting is the experiment's concurrency, not a proven cause. Raising the client or test timeout, retrying login, or changing the thread assertion would conceal this failure without identifying the stalled hop. A future occurrence needs existing HTTP client trace events or app/provider request spans for each redirect hop, written to Bazel undeclared outputs; a fresh RBE run should then establish whether one phase consistently stalls. No browser or auth behavior was changed on this evidence alone.

The earlier [cursor-334 thread-window projection gap](2026_11_12_thread_window_projection_gap.md) was a distinct failure after authentication and append. Its diagnostic follow-up is [PR #15811](https://github.com/agentydragon/ducktape/pull/15811) and [PR #15821](https://github.com/agentydragon/ducktape/pull/15821); neither diagnoses this setup timeout.

## Landed phase diagnostics and fresh exposure, 2026-11-13 UTC

[PR #15995](https://github.com/agentydragon/ducktape/pull/15995) merged the gated
HTTP redirect, Go auth and provider admission/header phases. Its original
Firecracker result-stream error is
[separately classified](../../../devinfra/buildbuddy/debug/2026_11_12_exact_execution_result_recovery.md).
It does not invalidate that invocation's sixteen passed browser shards, but
neither those shards nor a diagnostic unit test reproduced this setup timeout.

The [fresh focused repetition](https://app.buildbuddy.io/invocation/937eaae6-3988-4d8a-bede-7730610d1a9a)
used the historically affected
`test_small_upward_scroll_stays_detached_when_tail_streams`, disabled sharding for
the focused selection, and requested three uncached runs. All three real-browser
cases passed. The summary has three remote runs, zero cached; GetExecution
contains three distinct TestRunner IDs. The retained artifacts contain six
successful authentication exchanges across the three cases (fixture/client and
page login), all reaching `/auth/me` 200. Client request-to-final-headers spans
were 40.142–131.082 ms; each Go log contains admitted, session-loaded,
discovery-started/finished and commit-started/finished phases. Discovery took
9.473–99.500 ms in this sample. These are measured phase intervals from the
existing artifacts, not case durations or a claim about scheduling under 100 jobs.

A bounded [real setup fault injection](https://app.buildbuddy.io/invocation/b87aacfa-6152-4481-bd99-308ccd3316c4)
held the mock provider's actual TLS discovery response for 5.25 s, using the
[retained temporary patch](2026_11_12_auth_discovery_stall.patch). One fresh
remote TestRunner produced the same `AppProcess.authenticate()`
`httpx.ReadTimeout` while awaiting `/auth/login` response headers, before page
interaction. The client records only `/auth/login` request then failure after
5.002 s. The Go trace records admission and session load, then discovery-started
at 03:18:23.572847492 UTC; the provider accepts discovery at
03:18:23.654219392 and sends headers at 03:18:28.906263931, after the client deadline.
Go discovery-finished occurs at 03:18:28.575132371 because client cancellation
ends the upstream request: that phase means the call returned, not success.
There is no authorize or token exchange in this controlled attempt. This
localizes the intentionally induced stall to provider discovery across the real
fixture, TLS, Go service and HTTPX boundary; it does not prove discovery caused
the historical timeout. The injection was restored before committing.

Both experiments use effective source `4415ac2544`; unrelated diagnostics/test
patches were outside this target's inputs. No login retry, timeout or normal
serving behavior changed. A bounded 24-hour canonical-repository history query
returned 114 passed target entries and no failed entry, but includes cache reuse
and omits some PR/manual runs; it is an index observation, not physical exposure
or a failure-rate reduction.

[Issue #16032](https://github.com/agentydragon/ducktape/issues/16032) remains open.
For the next natural recurrence, join the client trace ID to the Go phases and
provider path/port: first distinguish missing app admission/session-load from
provider discovery, authorize, exchange or session commit. At seven days,
compare non-cached physical target exposure and matched setup failures; do not
use cached passes as post-change evidence. No scheduled monitor is claimed.
