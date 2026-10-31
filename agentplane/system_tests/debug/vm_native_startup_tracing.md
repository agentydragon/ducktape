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
