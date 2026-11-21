# Executor placement and resource boundaries

Status: proposal, 2026-11-20. This document compares execution affordances and proposes
experiments; it does not change the [execution contract](../SPEC.md) or the
[current isolation behavior](../ISOLATION.md).

## Problem and goals

The native executor owns environment-level execution sessions, process handles, output, stdin,
signals and file operations. Both the typed API and Codex-compatible WebSocket API reach that
owner. Today its commands share its memory cgroup: an allocating command can destroy the owner
and every retained handle. The incident on 2026-11-20 showed why the distinction matters: in
`public-coder-1` two Python processes had about 1.35 GiB and 552 MiB of anonymous RSS when their
2 GiB executor container hit its limit. The kernel selected a Python process and, because
`memory.oom.group` was set, killed the container's companion and Codex server as well. This
identifies the memory consumers and group-kill mechanism, not the Python command lines or the
cause of a separate later session loss.

We want a configurable execution boundary that keeps the owner available when command memory is
exhausted, while retaining a persistent workspace, useful long-running/background processes, and
honest execution outcomes. Commands within one executor may share a resource budget. Separate
per-command Pods are not a target: starting each command in a new Pod does not preserve the same
process and environment state and adds scheduling and storage costs. User-authored files are never
made disposable by this design.

## Axes and contract

These are independent choices, not a single hierarchy of isolation modes:

| Axis           | Questions                                                                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Process owner  | Who starts, observes, signals, and eventually reaps a process, including its descendants?                                                   |
| Placement      | Which VM, container, PID namespace and cgroup contain the owner and commands?                                                               |
| Resource scope | Are memory, CPU, PID and I/O budgets shared across all commands, per session, or per process? What ancestor limits still apply?             |
| State          | Which filesystem paths and background processes persist between calls and across a container, VM, or Sandbox restart?                       |
| Transport      | How do callers and the owner reach the process, and what happens when an API or stream disconnects?                                         |
| Security       | Which identity can start commands, call the Kubernetes API, create or move cgroups, and signal the owner? Can commands escape their limits? |
| Recovery       | Which component establishes that old work is fenced before replacement, and what can be reported without guessing or replaying effects?     |
| Configuration  | Which placements and limits are supported per environment, and what happens if requested isolation cannot be established?                   |

The user-facing API should state capabilities and actual behavior rather than imply that every
backend has identical guarantees. A placement is acceptable only if it defines process identity,
output and cursor retention, stdin, TTY, signals, exit reporting, file paths, and cleanup. A
transport loss is not a process exit, and an uncertain command must never be automatically
replayed. Record `not dispatched`, observed exit, and unknown outcome distinctly. A reconnect
promise requires evidence that the same process and retained output can actually be recovered;
it cannot be inferred from a live network connection. Existing typed and Codex APIs both need
to observe the same owner and the same placement policy.

## Options

| Option                                                 | Owner and resource boundary                                                                             | Strengths                                                                                                                     | Costs and unresolved behavior                                                                                                                                                                                                   |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Current direct child                                   | Native executor owns child; commands and owner share a cgroup                                           | Existing process, pipe and reconnect behavior; no additional placement service                                                | A command OOM can kill the owner. Changing a limit or `oom_score_adj` alone does not establish isolation.                                                                                                                       |
| Shared VM guest cgroup                                 | One workload cgroup per executor, separate from the owner's service cgroup                              | Commands share state and one configurable budget; protects owner from a workload-local OOM when ancestor headroom is adequate | Trusted creation, atomic pre-exec placement, accounting, fencing and cleanup need implementation. Parent or guest OOM can still affect both.                                                                                    |
| Systemd service per command                            | systemd owns each command in a bounded service or shared workload slice                                 | Reuses guest manager and existing Runtime supervision mechanisms                                                              | Different parent/wait model; adapt streams, PTY, status, background descendants and reconnect. Per-command units do not require per-command limits, but add lifecycle work.                                                     |
| Kubernetes `pods/exec` into current executor container | Kubernetes starts commands in the executor container's cgroup                                           | No new container                                                                                                              | Does not separate command OOM from the owner; unsuitable for the main goal.                                                                                                                                                     |
| Kubernetes `pods/exec` into persistent tools container | Native executor owns API-side bookkeeping; Kubernetes starts commands in a separately limited container | Container budgets separate owner and commands; shared workspace and a persistent tool environment                             | A stream failure may leave process state or output unknown; Kubernetes API, RBAC, restarts, signals, stdin and reattachment need proof. Container-level isolation does not prevent Pod/node OOM.                                |
| Delegated cgroup within a Pod                          | Trusted manager places direct children under a bounded cgroup inside the container                      | Could preserve direct-child ownership and the current API semantics                                                           | Current Pod security has read-only cgroupfs and no delegation. Writable control, escape resistance, runtime support and same-UID behavior need a security design and real tests. The outer container limit remains an ancestor. |
| Tool container with a separate execution service       | A second owner in the tools container supervises commands and serves an API                             | Could implement richer retained process semantics there                                                                       | Adds a service and another authority for the same state; a tool OOM can kill that owner. Do not add it by default merely to hide stream limitations.                                                                            |

A tools container is persistent only for the lifetime of that container incarnation; Kubernetes
may restart it. Mounted persistent workspace files can survive, but process memory and in-container
state do not. A shared writable workspace does not automatically give the owner the tools
container's filesystem, cwd, environment, user, process namespace, or executable paths.

## Proposed VM and Firecracker direction

Start with one **shared workload cgroup per executor**, alongside the owner's cgroup under an
appropriately budgeted parent. VM and Firecracker both use the guest Runtime and systemd
supervision, but each deployment and guest must be tested. The existing executor slice limits
apply to the combined executor service and its current descendants; they do not currently create
a separate workload boundary. Existing Runtime supervision already launches systemd transient
services, applies CPU/memory/PID/I/O properties, owns their streams, and fences stale units.

**Initial implementation: systemd-owned command services under a shared workload slice.** Runtime
launches commands through the existing transient-unit supervision path and puts them under one
aggregate command budget. Each command has a unit for lifecycle management; the slice, not a
per-command limit, enforces the shared budget. This reuses manager ownership and fencing, but
systemd becomes the parent: adapt the executor's direct-child wait, signal and stream semantics
rather than assuming the existing launcher preserves them. Measure launch overhead and verify that
background processes behave as specified.

**Future alternative: executor-owned direct children.** A trusted Runtime-owned mechanism could
create the workload cgroup and provide the executor an authorized way to start children in it,
for example using Linux cgroup-v2
[`CLONE_INTO_CGROUP`](https://man7.org/linux/man-pages/man2/clone3.2.html) through
Go's [`CgroupFD`](https://pkg.go.dev/syscall#SysProcAttr) support. This could preserve the existing
direct-child wait and pipe model. It needs actual guest kernel and permission checks, trusted FD
handoff, fork/exec error handling, TTY support, and proof that non-root commands cannot escape.
It remains a future choice, not an implementation prerequisite.

VM and Firecracker guests currently depend on systemd for Runtime and native process supervision
beyond this proposed workload boundary. Starting with systemd does not make that dependency an
execution API or environment requirement: keep the command-placement contract and lifecycle
semantics independent of systemd unit names and D-Bus. Reuse the existing supervision boundaries
where they fit; do not add a speculative abstraction solely for a hypothetical backend. A future
systemd-free guest would need an alternative owner for Runtime startup, resource placement,
process observation, cleanup and stale-work fencing, not merely a different executor flag.

Do not move a PID into a cgroup only after the target begins executing: a fast allocator can win
the race and already charged pages need not move. Avoid granting the untrusted tool the ability
to move itself into the owner's cgroup or raise limits. Cgroup membership limits resources; it
does not itself isolate filesystem access or same-UID signals. A shared budget also means one
command can kill other commands in that budget; the goal is to keep the owner and future work
available, not guarantee every other command survives.

Budget the **parent**, owner and workload together. [Cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html)
accounts resources hierarchically, and [systemd resource control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)
enforces the effective limits of ancestor slices as well. A child `memory.max` does not protect the
owner against exhaustion or an OOM at a shared ancestor or host. Reserve sufficient owner and
Runtime headroom; validate limits against the aggregate, and test guest-level pressure separately.
Decide explicitly whether to set `memory.oom.group` for the workload and how to report all
processes affected by a group kill. A configured isolation mode must fail closed if setup or
placement fails, before accepting the process as started. Define ownership on Runtime shutdown,
executor replacement, partial startup and orphan cleanup; do not create a parallel, unfenced
lifecycle controller.

## Kubernetes direction

Investigate a persistent tools container in the Sandbox Pod with its own configured requests and
limits and the required persistent mounts. Keep the native execution service as the single owner
of session and process bookkeeping. Try Kubernetes [`pods/exec`](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_exec/)
as a launch mechanism, with the executor targeting that tools container rather than its own. Do not assume an API stream is a
persistent process handle or that stream loss means either process exit or continued execution.

The spike must establish whether this model can deliver the execution contract, including:

- arguments, cwd, environment, user, TTY and stdin; Kubernetes exec options alone may not carry
  every native launch attribute, so account for any needed launch wrapper and its semantics;
- stdout/stderr retention and bounded buffering, exit status, signal/termination and descendant
  fencing, especially if the stream, API server or kubelet disconnects;
- behavior when the tools container OOMs or restarts: detect its incarnation change, classify
  prior process outcomes without replay, retain the executor session when safe, and admit new
  commands only after the replacement is ready;
- workspace and home/tmp mounts, file API behavior, network identity, persistent state and image
  compatibility between the executor and tools container;
- the exact Kubernetes credentials, API reachability and RBAC needed to exec only into the intended
  Sandbox incarnation; evaluate confused-deputy and broader `pods/exec` authority, including a Pod
  replacement between resolution and use;
- resource requests and limits of both containers and their Pod/node ancestors, and the impact of
  a failed tools-container health check or resource pressure on the executor.

If Kubernetes streams cannot meet a required reconnect or ownership guarantee, expose and evaluate
that gap. A helper, tool-container process owner, delegated cgroup, or different boundary may be
justified by a demonstrated need, but each adds authority or failure coupling and is not assumed
by this proposal. Per-command Pods and their different state and scheduling model are out of scope.

## Configuration and compatibility

Put deployment settings in the existing typed environment configuration, not a separate limits
file or an environment-variable override. Placement is an environment/runtime policy applied to
both protocol surfaces; a request must not be able to weaken the operator's budget. Illustrative
shape only, not a wire-format decision:

```yaml
execution:
  placement:
    type: systemd
    memory_max_bytes: 1073741824
    pids_max: 128
    cpu_quota_micros: 100000
```

The discriminator names the placement mechanism actually required by the deployment. A future
direct-cgroup implementation could use its own type without changing the execution API. A
Kubernetes placement would select a tools container and its deployment-owned resource settings.
Avoid duplicating container or VM budgets in two independent authorities. Specify supported
modes and defaults per environment, validation of parent/headroom and disabled settings, restart
requirements, unsupported-mode errors, and rollout/rollback behavior. The selectable
Codex-backed executor must remain supported; the native-only placement mechanism must not imply
that upstream Codex commands receive isolation when they do not. An execution service restart
still loses its in-memory handles and cannot authorize replay.

## Staged work and acceptance

1. **Guest proof.** Implement the smallest shared-cgroup prototype in a disposable VM; verify the
   systemd-owned command services under one shared workload slice. Verify placement and
   owner/command cgroup identities, privilege boundary, cleanup, aggregate accounting, and
   process-launch overhead. Keep direct-child placement available for later evaluation.
2. **Real OOM and lifecycle proof.** Set a small workload budget, trigger a kernel memcg OOM, and
   record `memory.events` and process/cgroup evidence. Show the owner remains alive, the same
   execution session handles a new command, and killed work is reported without replay. Cover
   concurrent commands, stdin and PTY, disconnect/resume, descendant cleanup, failed placement,
   Runtime/executor shutdown, restart, and stale-cgroup fencing. Exercise both VM and Firecracker.
3. **Kubernetes comparison.** Prototype a persistent tools container plus `pods/exec`; run a real
   OOM and stream-loss/restart tests with scoped credentials. Compare its guarantees and costs
   with an explicitly threat-modeled delegated-cgroup prototype. Select a design only after
   measuring the gaps; do not introduce a second execution owner as an assumed requirement.
4. **Production rollout.** Add validated settings and diagnostics, choose deployment-specific
   defaults, test old/new configuration and image ordering, and roll out without losing authored
   workspace state. Document which failures lose active handles and how operators verify the
   selected boundary. Keep the existing protocol-gap and Sandbox lifecycle tracking separate.

Open decisions include the final systemd unit topology, parent budget/headroom, shared-cgroup OOM
policy, treatment of unsupported environments and the Kubernetes API's acceptable reconnect
semantics. These need proof and review; this document does not commit to a particular cgroup
helper or Pod privilege change. A non-systemd guest remains a possible later direction.
