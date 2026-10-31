# Pod message notice dispatch latency, 2026-10-31

The post-routing Pod suite in BuildBuddy invocation
`856dbe85-5d71-594c-acb0-ad16366407aa` passed 35 tests but its two
real `agent_messaging.notice_wait` spans were 6.93 and 6.75 seconds.
The fixture intentionally debounces notices for a one-second quiet
window, capped at two seconds. The message service's `SendMessage` RPC
itself took 52 ms in the Claude case. It sent the notice to Dispatcher
at 10:28:51.790 UTC, about a second after the message commit; Gateway
enqueued at 51.793 but did not deliver to Runner until 56.282. Runner's
Claude harness then spent 1.277 seconds starting and handshaking. The
sender's 4.562-second `send` test span includes opening its native
harness and CLI; the service send RPC is a small fraction of it.

A focused uncached real Pod messaging case at
[`eff8adb3`](https://app.buildbuddy.io/invocation/eff8adb3-2b47-4b47-b39a-257cd78a59aa)
also passed both harnesses' send, covered-but-unacknowledged, authenticated
sender, inbox read, UID, and acknowledgement assertions. Its notice waits
were 5.89 and 5.21 seconds. Gateway child spans locate the queue delay
in `OwnershipLeases.Acquire`: successive acquisitions took 0.918, 1.170,
1.797, and 2.184 seconds, then roughly 1.8–3.1 seconds as more work
arrived. Each long `Dispatcher Reconcile` span consists almost entirely
of one or two `Dispatcher Reconcile Acquire Lease` spans. A pending
recipient's `Dispatcher Process Session` can run before the ownership
scan finishes and return without an owner; it is retried once the scan
enqueues its head.

`runner_gateway.kubernetesConfig` previously left `rest.Config.QPS` at
zero. In the pinned client-go code, zero selects a per-client token
bucket of five requests per second with burst ten. One Lease acquisition
reads the DB generation, then makes multiple Kubernetes Get/Create/Update
requests and verifies the written Lease. Gateway also uses that client
for Sandbox observation. The Kubernetes HTTP spans inside the slow
acquisitions are milliseconds; the seconds are spent before those HTTP
requests. The default client-side limiter is therefore the inferred
source of the ownership queue. Other Agentplane control services already
set `QPS = -1` on their Kubernetes clients. The Gateway now does the
same, preserving the Lease identity, generation, transition, and DB
claim checks.

The uncached post-change real Pod case passed at
[`86613339`](https://app.buildbuddy.io/invocation/86613339-7cfd-4783-9906-dfd290d17019)
on commit `5695d2df9c`. Its 12 Lease-acquire spans ranged from 8 to 19 ms
(median 10 ms). The two recipient notice commands went from Gateway
enqueue to delivery in about 33 and 31 ms; the `notice_wait` spans were
2.20 and 2.89 seconds. The preceding traced case had 5.89 and 5.21
second notice waits, and the original suite had 6.93 and 6.75. These
single-worker runs are not a population latency estimate, but the
mechanistic Lease-acquire and enqueue-to-deliver contrasts directly
verify the throttling diagnosis. The remaining wait includes the
intentional one-second notice quiet window and native harness turn
startup. Sender-state reads also fell from 8.58/7.19 to 0.77/0.66
seconds, consistent with the same dispatcher path serving those CLI
commands.

The exact source revisions and trace files can be retrieved with
`bbapi artifact download <invocation>
//agentplane/system_tests/suites:test_pod/test.outputs/{test,process}-spans.jsonl
-o <local-path>`; use separate requests for each filename. The focused
`//agentplane/runner_gateway:config_test`, dispatcher `methods_test`, and
dispatcher `store_integration_test` all passed at
`5d0b7e2d-3d0e-4cc8-bfa6-51d2a6b6959c`.
