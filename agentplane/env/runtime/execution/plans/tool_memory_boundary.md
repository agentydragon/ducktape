# Codex command memory boundary

Status: design prerequisite. This is not an implemented resource boundary.

## Failure and required behavior

The `codex_tool_oom` VM test has a retained run in which the 256 MiB executor
cgroup first killed the allocating `awk` process, then killed `codex` before
the first victim was reaped. The command's exit and same-native-Session follow-up
were therefore lost. The executor's `memory.oom.group=0` chooses individual
victims; it does not reserve memory for the server. Raising the shared limit,
raising `oom_score_adj`, or restarting the server would not provide that reserve.

The target is a kernel-enforced command budget below a separate native-server
budget in both VM and Pod execution. An allocating command must receive a real
OOM exit (normally 137); the exec-server must retain its process handles and
native Session; a subsequent command on that Session must run. If an outer OOM
or server loss still occurs, the outcome remains unknown and no layer replays
the command. The VM test must verify the command's cgroup and OOM counters, the
unchanged server PID/native Session, and the same-Session follow-up. The Pod
test must do the equivalent and verify that the executor container did not
restart. Normal termination, signals, stdin, PTY, `arg0`, environment policy,
working directory, and detached descendants need coverage too.

## Native launch point

Agentplane sends `process/start` directly to pinned Codex `rust-v0.162.1`.
Its `ExecParams` has `argv`, `cwd`, `env`, `tty`, `pipeStdin`, `arg0`, and
filesystem sandbox intent, but no resource domain. `LocalProcess` calls
`codex_sandboxing::spawn_process`; its `SpawnRequest` has no cgroup input. The
Linux PTY and pipe paths can use `codex_utils_pty`'s spawn helper. That helper
performs setup in a fresh single-threaded image, then `exec`s the target in
the same PID. A cgroup move there, before the target's `exec`, can preserve
Codex's native process ownership and IO semantics. Rewriting request `argv`
in Agentplane would bypass that prepared-command boundary and would need to
reimplement `arg0`, shell snapshots, sandbox wrappers and signal behavior.
The shared Codex app-server also connects to exec-server directly, bypassing
Agentplane's typed adapter. An adapter-only wrapper would miss those commands.

A pinned Codex patch needs a trusted, server-startup command-cgroup setting,
not a client-supplied cgroup path. For each accepted `process/start`, the
server should reserve a bounded cgroup beneath that root and pass a cgroup FD
through `SpawnRequest` to both PTY and pipe helper launches. The helper must
move itself before target `exec` and fail the start if that move fails. It must
not fall back to an uncontained launch. The server must retain the cgroup until
all descendants are gone; an exited direct child alone does not prove that.
Session expiry, explicit termination and backend fencing keep their current
no-replay ownership and must also remove the cgroup after proving it empty.
Tests against the patched native binary should cover all three spawn paths
(PTY, pipe with stdin, pipe without stdin), `arg0`, signals, startup failure,
detached descendants, and native Session continuity after real memcg OOM.

## Cgroup ownership in Agentplane

The VM currently starts exec-server and commands in one systemd transient
service under `agentplane-executors.slice`. Its launcher does not delegate a
subtree. A VM implementation would delegate that service's cgroup to the agent
UID, move the server into a `server` leaf, enable `memory` on the now-empty
parent, and create a capped `commands` subtree with a child per command. The
server and commands caps plus a measured margin must fit under the existing
executor slice and guest budgets. The root service still owns shutdown and
must prove its entire subtree empty before replacement.

The Pod executor companion is PID 1 and launches Codex in its own container.
The template runs as UID 1000 with all capabilities dropped and privilege
escalation disabled. Ordinary container cgroupfs is read-only. Neither Codex
nor the companion can create the command subtree under that contract. A
privileged cgroup manager with a writable host cgroup mount would enlarge the
Sandbox's host privilege boundary, so it is not an incidental implementation
detail. A runtime providing delegated writable cgroups, or moving this
execution to a VM boundary, is a separate platform choice. The chosen path
must prove that command, server and companion caps remain below the outer
container limit; otherwise an ancestor OOM can still kill the container.

Do not set a Codex tool-cgroup setting until startup has proved the delegated
root and all required controllers writable. On either backend, failure of
that proof prevents native admission. Silent fallback to the current shared
budget would misreport the requested isolation.

## Deployment choices and sequence

The VM can proceed independently: its existing systemd manager owns the
executor service and can delegate a subtree within that service without
changing Pod privileges. Ship the native hook and VM cgroup layout behind
explicit VM configuration, with VM tests proving the server's cgroup and
native Session survive a real command OOM. This is useful on its own, but it
must not be described as Pod coverage. The current Pod contract and tests stay
unchanged until a Pod mechanism is selected.

For Pod execution, the preferred direction is a runtime that delegates a
writable cgroup subtree to the unprivileged companion while preserving its
current capability and UID boundary. It has the smallest security expansion,
but needs a platform proof and may not be available in the current runtime.
An executor inside a VM also gives a writable guest cgroup hierarchy and a
clear privilege boundary, but changes Pod execution latency and deployment
shape. A privileged cgroup manager is technically possible but exposes host
cgroup control to a Sandbox-adjacent component; it needs a separate security
design, admission policy and host containment proof before implementation.
Simply adding a writable host cgroup mount to the current Pod is not an
acceptable default.

1. Patch and pin Codex with a trusted command-cgroup launch hook, preserving
   native process and Session semantics. Validate the patched binary directly.
2. Add VM subtree ownership, exact limits, startup validation and cleanup.
   Turn the existing VM OOM scenario into a per-command test. The VM change
   may merge once its own system tests pass, independent of the Pod choice.
3. Select and validate a Pod cgroup delegation mechanism without expanding
   host privilege unintentionally. Keep its security and ownership review
   separate from the VM change.
4. Add Pod subtree ownership and an OOM scenario that proves executor
   continuity. Publish CI and exact-head results before claiming Pod behavior
   is fixed. Update the isolation contract as each backend ships.

## Source anchors

- [Pinned protocol `ExecParams`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server-protocol/src/protocol.rs#L305-L345)
- [Pinned native launch](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server/src/local_process.rs#L347-L410)
- [Pinned spawn helper](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/utils/pty/src/spawn_helper_main.rs#L60-L105)
- [VM launcher](../../supervision/systemd_launcher.go)
- [Pod template](../../../../../cluster/cdk8s/agentplane/environment_sandbox.py)
- [Current isolation and Pod constraints](../ISOLATION.md)
