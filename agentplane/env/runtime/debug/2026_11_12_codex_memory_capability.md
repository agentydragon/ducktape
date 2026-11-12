# Unmodified Codex command memory capability

Date: 2026-11-12. Scope: pinned upstream Codex `rust-v0.162.1`, Linux VM and Pod execution.

## Outcome

The bundled `codex exec-server` has no supported per-command memory/cgroup admission control
in this version. Agentplane cannot configure it to place each command in a separate bounded
memory domain before target execution while retaining the existing native process ownership.
The requested command-OOM survival guarantee remains unmet by the current bundled executor.
This is a valid capability limitation, not a reason to weaken the test.

This conclusion is narrower than saying an unmodified Codex CLI cannot use such isolation.
The CLI already supports an external exec-server transport. A separately implemented compatible
executor could own command placement, but adopting and maintaining it would be a new architecture.
It is not required follow-up work for this investigation.

The user declined maintaining a custom Codex patch. The proposed native-hook design
([PR #15800](https://github.com/agentydragon/ducktape/pull/15800)) and patched image producer
([PR #15869](https://github.com/agentydragon/ducktape/pull/15869)) are abandoned. No dependent Runtime
boundary or fixture activation was published. This note changes no executable code, limits,
Pod privileges, or test expectations.

## Failure that remains

The [reliability investigation](https://github.com/agentydragon/ducktape/pull/15795) retained a
256 MiB shared executor-memcg failure: the kernel killed the allocating `awk`, then killed
`codex` about 1.5 seconds later, before the first victim had been reaped. The command result and
same-native-Session follow-up were lost. The same fresh source
[failed](https://app.buildbuddy.io/invocation/d75c1d3d-d600-5aa6-af7c-fcb44190d3da) and
[passed](https://app.buildbuddy.io/invocation/771f2d2b-8ca3-5b0e-b445-c6ea88ad0840).
A passing rerun does not establish memory isolation.

`memory.oom.group=0` permits individual victim selection; it reserves no memory for the server.
The existing [VM OOM scenario](../../../system_tests/scenarios/vm_sandbox.py) deliberately requires
real memcg OOM evidence and a follow-up on the same native Session. The
[execution contract](../execution/SPEC.md) remains unchanged: native-server loss leaves an unknown
outcome, and neither reconnecting nor replacing the server authorizes replay. Raising a timeout
or shared cap, preferring a victim, or retrying the command would not establish the missing boundary.

## Pinned source audit

The audit read the unmodified source used by the pinned Nix package, not the experimental patch.
These are the relevant upstream surfaces:

| Surface                                                                                                                                                                                                                                  | Supported controls                                                                                                   | Memory-boundary consequence                                                  |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| [`ExecParams`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server-protocol/src/protocol.rs#L329)                                                                                                                    | argv, cwd, environment policy, shell snapshot, TTY, pipe stdin, argv0, filesystem sandbox and managed-network intent | No command memory limit, cgroup destination or placement acknowledgement     |
| [`ExecServerCommand`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/cli/src/exec_server_command.rs#L33)                                                                                                                    | Transport/authentication, request concurrency, Linux sandbox PID namespace, proxy routing and shutdown               | No per-command memory or pre-exec resource-admission option                  |
| [`ExecServerRuntimeOptions`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server/src/runtime_options.rs#L10)                                                                                                         | Executable paths, PID namespace and sandbox/network preferences                                                      | No resource-manager hook                                                     |
| [`LocalProcess` launch](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server/src/local_process.rs#L418) and [`SpawnRequest`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/sandboxing/src/spawn.rs#L33) | Prepared command, cwd, environment, argv0, sandbox, TTY/stdin and inherited internal FDs                             | No cgroup input is carried into native spawning                              |
| [Native setup helper](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/utils/pty/src/spawn_helper_main.rs#L35)                                                                                                                | Internal process setup followed by target exec in the same PID                                                       | A possible upstream implementation point, not an exposed placement interface |

Filesystem restrictions, network restrictions and PID namespaces do not create a separate memory
accounting domain. Moving a discovered child after it starts leaves an execution race, and moving
its PID does not transfer already charged pages. Delegating a writable subtree to the existing
same-UID server/tools also lets tools reparent out of their command limit. Neither is the requested
secure pre-exec boundary.

## What remains possible without a Codex fork

The upstream [`DefaultEnvironmentProvider`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server/src/environment_provider.rs#L60)
reads `CODEX_EXEC_SERVER_URL` and selects a WebSocket executor. Agentplane already sets that variable
in [the app-server launch](../codexappserver/host.go), while its typed
[execution adapter](../execution/adapter/protocol.go) also speaks the exec-server protocol.
Consequently, changing only the typed adapter's command argv would miss the app-server's direct
executor traffic.

There are two distinct future choices, neither selected here:

- Adopt an upstream release that supplies a trusted pre-exec resource-admission interface. That
  would still require platform-owned limits and lifecycle fencing, followed by real OOM/Session
  continuity tests. No such interface was found in the audited version; this note makes no claim
  about later releases or an upstream delivery date.
- Route both clients to an Agentplane-owned compatible executor. The supported external transport
  makes this plausible without changing Codex, but the owner would need to preserve process and
  session identity, buffered output, stdin, PTY, signals, argv0, cwd/environment, sandbox and
  filesystem behavior, reconnect semantics and no-replay fencing. The upstream
  [protocol documentation](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server/README.md)
  describes that broader surface. This option has not been implemented or validated here.

A separate VM/container for an entire existing exec-server still leaves that server sharing its
memory domain with its commands. Per-command platform placement needs a launch owner. The VM has
root/systemd authority for such an owner; current Pods run the companion and tools as UID 1000,
drop all capabilities, disallow privilege escalation and expose read-only cgroupfs. A Pod solution
must isolate manager authority from same-UID tools and is a separate platform/security decision.
The current [isolation contract](../execution/ISOLATION.md) remains in force.

## Experiment disposition

The abandoned prototype did establish a root-owned VM cgroup mechanism with an emulated helper
in [kernel control 87432cfd](https://app.buildbuddy.io/invocation/87432cfd-67e7-4ba7-8809-b15e8c61fcad).
That is not evidence that unmodified Codex provides this capability, or that the patched native
Session survived OOM. Its native startup subsequently failed a strict residual-memory accounting
guard, and the overall focused test failed.

The later [native validation 8012a21d](https://app.buildbuddy.io/invocation/8012a21d-ecbe-4f9e-82cc-5752ee58a52a)
was cancelled when the user rejected the patch route. `test_vm` ended with **NO STATUS**; the sibling
guest-layout test passed. There is no native OOM-survival result from that run. The experiment ended here.

Recommendation: retain the diagnosed bundled-executor limitation and the existing no-replay/OOM
assertions. Revisit only if an upstream capability or an explicit executor/platform decision
changes the available boundary.
