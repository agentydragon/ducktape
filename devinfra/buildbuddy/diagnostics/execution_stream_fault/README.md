# Interrupt an accepted execution stream

This opt-in loopback proxy forwards native Bazel's execution and capability RPCs to
`remote.buildbuddy.io` with system TLS trust and the caller's incoming metadata.
It sends the first nonterminal Operation before interrupting that stream with
`INTERNAL`. `--interruptions=2` also interrupts the first subsequent nonterminal
WaitExecution response. It never calls Execute or retries upstream on its own.
It records only RPC/operation identities and terminal flags; headers, action
contents and response payloads are never written.

Use it for a bounded diagnostic invocation, never as a normal executor endpoint.
Build first with `bbr build //devinfra/buildbuddy/diagnostics/execution_stream_fault
--remote_build_event_upload=all`, then run the downloaded executable from `bb-out`
with `--listen=127.0.0.1:17951`. Keep the proxy alive through the observation.

From a separate shell in the same devshell, use native Bazel so the outer client
can reach this loopback listener:

```bash
umask 077
bazelisk test //devinfra/buildbuddy/bbapi:bbapi_test \
  --remote_executor=grpc://127.0.0.1:17951 --remote_retries=1 \
  --nocache_test_results --test_arg=-test.v \
  --test_arg=-test.run=TestExecutionWaitCommandCredentialsAndOutput \
  --remote_grpc_log="$scratch/native-remote-grpc.bin"
```

`$scratch` is the task scratch directory. The remote cache and actual test
execution remain on BuildBuddy. This test exercises read-only API behavior, so
a deliberate observer failure does not replay an application mutation. Preserve
an accepted operation's full identity when recovering its result read-only with
`bb execution get`; a recovered test action does not make a failed Bazel gate green.

The native gRPC log contains request metadata and protocol payloads. Keep it
private with mode 0600 and never attach it unreviewed to CI artifacts, a PR or an
issue. Publish only reviewed operation/status/count summaries. The proxy's
`INTERNAL` status exercises native retry classification; it is not a literal
HTTP/2 RST_STREAM injection or proof of a hosted failure's cause. See the
[measured recovery investigation](../../debug/2026_11_12_exact_execution_result_recovery.md).
