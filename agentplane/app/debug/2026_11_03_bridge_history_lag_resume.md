# Bridge continuation while History ingress is fenced

The failed `//agentplane/app:test_bridge` shard in BuildBuddy invocation
[`de4bf7aa-72d3-5056-8586-d2f163c458a8`](https://app.buildbuddy.io/invocation/de4bf7aa-72d3-5056-8586-d2f163c458a8)
contained `test_open_and_inputs_reach_the_runner_while_history_lags[claude]`.
The case timed out after 629.2 seconds; its enclosing shard action lasted 839.6 seconds.

The asyncio cancellation stack localizes the stalled await to the **second** scripted
model request, after the seed turn, harness kill and admission of the continuation.
It was waiting in `ModelEndpoint._pending.get`, while the test intentionally held
History's ingress row lock. Runtime logs show a replacement Claude process launched
and exited about one second later. They do not establish its exit reason.

The retained History archive was empty because ingress was fenced at cursor zero.
The native transcript contained only session metadata. Neither artifact contains the
Runtime's committed local events, including native stderr and launch-failure records.
Those missing records are the next discriminating evidence; a resume-file flush race
or an execution failure must not be inferred from the empty History archive alone.

The test now uses its existing 30-second model-request helper for both requests.
Failure capture exports the last 512 committed events from each local SQLite journal,
in cursor order, using a read-only connection. This preserves evidence independently
of the History service and lets normal pytest teardown run before the target timeout.
The test still requires the continuation to reach the model while ingress is fenced,
two distinct completed turns, confirmed inputs and eventual archive catch-up.

Three focused fresh RBE repetitions passed in invocation
[`1c9c7065-b9eb-4a94-b7ac-a33c083a11a6`](https://app.buildbuddy.io/invocation/1c9c7065-b9eb-4a94-b7ac-a33c083a11a6)
on devel `c4aefe302d8966b2a122c8e73488f161547c23a7` with the diagnostic patch.
The command selected only the affected Claude case, disabled sharding and set
`--runs_per_test=3 --nocache_test_results`. Action durations were 55.9–72.4 seconds.
This does not reproduce or repair the original resume failure and does not measure
a fleet latency improvement. The next recurrence should retain the missing local
journal before a mechanism-specific repair is proposed.
