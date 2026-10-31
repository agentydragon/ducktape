# VM native Runner startup spans, 2026-10-31

The Runner now records spans at the native harness boundary: `agentplane.runner.harness.start`
encloses workspace preparation, native process or shared Codex app-server acquisition, and the
thread handshake. `agentplane.runner.harness.handshake` covers the protocol handshake. Codex's
shared `agentplane.codex.app_server.start` encloses process `launch` and JSON-RPC `initialize`;
`agentplane.codex.thread.handshake` covers `thread/start` or `thread/resume` on that server.
`agentplane.codex.exec_server.launch` covers starting the exec-server, while `ready` covers each
readiness check. `agentplane.codex.tool.start` covers publication of a newly observed tool item,
not the tool's execution or time to first tool. These spans use the Runner's existing OTLP exporter;
the k3s fixture's test-owned receiver can retain them as `process-spans.jsonl`.

The first integrated RBE VM run passed all six scenarios with the 96 MiB tool-OOM profile:
[`test_vm` child invocation](https://app.buildbuddy.io/invocation/e7793336-ed58-47eb-b193-cb56463198a1),
outer `004de109-5cb6-48f1-aec4-38222a1a64eb`. Effective source was local
`3c16fe96e9` atop fixture collector `3efbbc0f89` and OOM budget `3af9e37afb`; BuildBuddy's
`86f590ec` commit metadata is the `bbr` sync base, not the effective source tree. The retained
`test.outputs/process-spans.jsonl` contains 12,648 spans, including 193 Runner spans with
`service.name=agentplane-runner` and reported `process.pid` values 328–331. Guest PIDs can
repeat across VMs, so those values do not count distinct processes. The downloaded artifact's
SHA-256 is `f9207eedbe28a60145aa569ecc3dc5a6275571c07ebdae8ed40709b26757138f`.

| Boundary                           | Count | Durations in this run |
| ---------------------------------- | ----: | --------------------- |
| Harness start, Claude and Codex    |     5 | 22.0–28.5 s           |
| Harness handshake                  |     5 | 5.7–24.8 s            |
| Shared Codex app-server start      |     3 | 9.2–10.9 s            |
| Codex app-server launch            |     3 | 1.3–1.4 s             |
| Codex app-server initialize        |     3 | 7.6–9.5 s             |
| Codex thread handshake             |     3 | 5.5–7.8 s             |
| Codex exec-server launch           |     4 | 0.9–1.1 s             |
| Codex tool-start event publication |     5 | 0.011–0.048 s         |

These are nested, overlapping spans from concurrent VMs; their durations cannot be added to
derive suite time. The `ready` span also appears on 152 probes and represents each readiness
check, not the entire exec-server initialization. Each Codex thread handshake is a child of its
Runner harness handshake. The native harness-start and app-server-start roots have separate trace
IDs from the Python test's `vm.case` span, so trace ID alone does not join the guest process to
the driver. Use the safe `session.id`/`thread.id` attributes and the test artifact's timestamps
for that boundary. The driver's `vm.case` trace ID joins 3,964 app, history-service, and
runner-gateway spans in the same artifact; it does not establish a parent link to the guest.

The OOM scenario's `vm-codex-oom.json` names Session
`4b78e3ba-c054-4c65-b69a-c5b9a24c578a`. Its Runner spans share one export source
(`ipv4:10.42.0.34:51694`) and Runner PID 331; the handshake and tool spans share thread
`01a190d7-de2f-75c0-9b2a-06287c0539ab`. Relative to its harness-start timestamp, the
exec-server launched at 2.058–3.146 s. Its first successful readiness probe ended at 10.730 s;
earlier probes had error status. App-server startup ran at 10.873–21.120 s: launch ended at
12.217 s and initialize ended at 21.120 s. The thread/start handshake ran at 21.133–27.392 s,
inside the harness handshake which ended at 27.614 s. The first observed tool-start publication
began at 37.736 s, 10.344 s after thread handshake ended. These are timestamps from one Runner
clock; gaps include work without these named spans. The exec-server launch, app-server startup,
and tool publication have their own trace roots, so their sequence is joined by source/session/
thread identity and timestamps, not by parent IDs. This observed first-tool interval is not a
measurement of the later OOM command's execution time.

This run establishes observable Runner startup and protocol boundaries, not a CPU profile of
Claude, Codex, the guest OS, or image unpacking. For repeat runs, download the test's
`process-spans.jsonl` and `vm-phases.jsonl` undeclared outputs and inspect the process span's
`trace_id`, `parent_span_id`, resource and attributes. See [phase tracing](../README.md#phase-tracing)
for collection and propagation details.

## App-server initialization overlap experiment

The [candidate VM suite](https://app.buildbuddy.io/invocation/4e6904ae-544c-457a-8517-e1a73c2b5282)
passed all six scenarios on a fresh test action (`--noremote_accept_cached`, 346.4 s). Its
`process-spans.jsonl` contains 14,258 spans, including 162 Runner spans (SHA-256
`5931da01bd9d13112730c30f9436b369a08f187f1683872377f1729a39bf76fb`). The OOM
scenario artifact names Session `e2517eff-aa24-4bac-90b3-23c4bc2733be` and native thread
`01a19148-9cd8-7321-8488-250d4860afc3`. Joining Runner spans by their export source and
process PID, then using that Session and thread identity, gives these boundaries relative to the
OOM guest's app-server-start span:

| Boundary                              | Candidate interval |
| ------------------------------------- | -----------------: |
| Exec-server launch                    |      0.019–1.801 s |
| First successful exec readiness probe |      9.064–9.995 s |
| App-server launch                     |      0.006–2.960 s |
| App-server initialize                 |     2.961–17.238 s |
| Thread handshake                      |    17.289–23.183 s |
| First tool-start event publication    |    35.436–35.489 s |

The other two Codex guests also started app-server initialization before their first successful
exec readiness probe: app-server start to first ready probe end was 10.262 s and 9.884 s; their
app-server-start spans ended at 17.501 s and 18.001 s. This proves the intended concurrent
initialization occurred in the real guests while readiness still gated thread startup. The OOM
guest's app-server initialization lasted 17.2 s versus 10.2 s in the earlier run, so the two
samples do not show an end-to-end latency improvement. Tool-start spans mark event publication,
not tool execution or time to first tool.

The earlier test action ran on BuildBuddy worker `029a3c6b-4967-4cb3-a827-17bcb12bca1a`
(327.6 s); the candidate ran on `d8f4681f-863b-4341-a222-9fafa0b33bef` (346.4 s). Both
test actions executed, but their worker and underlying cache states were not controlled. The
earlier effective source was `3c16fe96e9` atop the collector and OOM fixture changes described
above. The candidate was the overlap worktree before its later Host stop-error message refinement;
BuildBuddy recorded sync base `a7685d0f`, which is not the complete mirrored source. Rebase onto
`origin/devel` changed no Runner files. These differences preclude a causal before/after wall-time
claim from the two suite runs.

The first candidate `runner_test` attempt [timed out at Bazel's 300 s limit](https://app.buildbuddy.io/invocation/bdee64f2-ce30-4605-9cd7-ffd2d1ccebff)
without a useful test log. A focused Go 45 s timeout
[captured the stack](https://app.buildbuddy.io/invocation/786743a4-5353-4196-93f3-866152e16b29):
the deadline test was blocked in `synctest.Wait` while its newly real app-server subprocess had
active stdout and stderr pipe reads inside the fake-clock bubble. The test was changed to use a
short real deadline while preserving the assertion that joined demand keeps the original stored
deadline. The focused Codex suite then passed in
[BuildBuddy 71d30ca2](https://app.buildbuddy.io/invocation/71d30ca2-7b2e-478c-9c8b-50c28f393648),
all 35 runner package targets passed in
[BuildBuddy 4f7aa34b](https://app.buildbuddy.io/invocation/4f7aa34b-d5b2-40d8-aa6d-341327fad310),
and the two lifecycle targets passed after the Host error-report refinement in
[BuildBuddy e033a1b3](https://app.buildbuddy.io/invocation/e033a1b3-2ddf-46db-ae95-d0a3631e4ae8).
