# Native Codex command output loss

The VM `codex_tool_oom` scenario failed before allocating memory: `memory-before` returned exit 0
with empty output, so `_MemoryEvidence.from_output` found no `VM_MEMORY` record. This is an upstream
native command-result boundary; the central sampler's JavaScript bridge does not participate in
this native Session.

## Recorded boundary

[BuildBuddy invocation 09492775](https://app.buildbuddy.io/invocation/09492775-dbce-5619-be01-9c459c29be1d)
contains both sides for the same command:

- `test.outputs/vm-artifacts/native/1b325382-f538-4905-94a0-6fa84adf70e4/c2875632-8df2-4749-a784-1673977f73eb.jsonl`,
  native cursor 22: completed command `exec-2921d1f0-3ae6-4088-989a-a452abf1c784` has exit 0 and
  `aggregatedOutput` containing the complete `VM_MEMORY` JSON.
- `test.outputs/scripted_upstream/003-virt-launcher-systest-vm-codex-oom-ptbhb.json`:
  `memory-before-wait-1` reports
  `VM_EXEC_RESULT {"chunk_id":"49972c","wall_time_seconds":17.78094171,"exit_code":0,"original_token_count":0,"output":""}`.
  There is no `session_id`, so the script cannot poll for remaining output.

The JSON was emitted intact by JavaScript. Its zero token count and empty nested output place the
loss before `text()` and cell wait delivery, rather than in the VM evidence parser.

## Exact upstream ownership

The Bazel native harness uses the prebuilt npm `@openai/codex` Linux x64 archive, originally
0.162.1. Its app-server and exec-server come from the same archive. The independent `@codex` source
archive builds execpolicy audit libraries; patching that archive does not change the native binary.

In [0.162.1's command manager](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/unified_exec/process_manager.rs),
`exec_command` and `write_stdin` drain output before calling `refresh_process_state`. That function
removes the handle when the foreground has exited, without requiring output closure or an empty
pending buffer. Output arriving after collection can become inaccessible to the model. The recorded
artifacts do not expose the exact scheduling interleaving inside these operations.

The correct native completion and empty model result can coexist because
[`OutputBuffers`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/unified_exec/process.rs)
appends each chunk to both `pending` and `transcript`: polling drains the former, while
[`resolve_aggregated_output`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/unified_exec/async_watcher.rs)
reads the retained latter for the native completed item.

[0.166.0's command manager](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/unified_exec/process_manager.rs)
adds `OutputRetention::UntilCollected`: for remote execution it retains the handle while output is
not closed or pending bytes remain. `CODEX_EXEC_SERVER_URL`, used by the VM harness, selects that
remote path. This is the upstream repair adopted by the candidate archive update.

## Narrow reproduction and limits

`//agentplane/native/codex:test_code_mode_output` runs the real app-server and remote exec-server.
A foreground shell exits while a descendant holds stdout open and waits on a FIFO. The test releases
the FIFO only after receiving the first nested Code Mode result. No scheduling sleep or command
replay is needed.

On 0.162.1, [baseline c961a4eb](https://app.buildbuddy.io/invocation/c961a4eb-4183-45d3-8c5e-1d0528830459)
fails with `_CommandResult(output='', session_id=None, exit_code=0)`. The required behavior is to
retain a pollable handle, then return the tail once with exit 0 after releasing the FIFO.

This deterministically exercises foreground exit before output closure. It does not force bytes
between the manager's first drain and status refresh, and does not reproduce the VM's exact
scheduler interleaving. The original VM/OOM scenario remains a separate CI acceptance boundary.

With 0.166.0, [candidate 6399e4ae](https://app.buildbuddy.io/invocation/6399e4ae-bead-4486-9534-4945bbc8dc38)
passes this regression and `//agentplane/native/codex:test_tools`. The candidate also changes the
central sampler's JavaScript host because that executable shares the archive; its stdio protocol
changes are additive. The existing nested-command service check also passes in
[865b92cb](https://app.buildbuddy.io/invocation/865b92cb-36ee-45bb-9e98-d861955f6032)
(`test_service -k test_nested_command_inherits_executor_environment`). Full service coverage and
the VM scenario still require their CI results.
