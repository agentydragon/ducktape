# Runner supervision backends

Use systemd for VM process supervision and retain container-native supervision for Pods. Runner's
COSI controllers keep lifecycle policy, native protocol handling, command admission and journals.
This document defines the next implementation slices; it does not describe an implemented backend.
The [task DAG](task_dag.md#runner_systemd_fixture--exercise-real-systemd-in-the-vm-fixture) owns status.

## Current seams

- [`Process`](../runner/process.go) launches the runner executable in supervisor mode. The
  [`supervisor`](../runner/supervisor.go) retains the state-owner descriptor and fences native work.
- Native launches occur in [`session_native.go`](../runner/session_native.go),
  [`codex_app_server.go`](../runner/codex_app_server.go),
  [`codex_exec_server.go`](../runner/codex_exec_server.go),
  [`runner.go`](../runner/runner.go) for setup/bootstrap scripts, and
  [`workspace.go`](../runner/workspace.go) for workspace creation under the agent identity.
- Production [`vm.nix`](../runner/vm.nix) already uses systemd. Runner currently manages delegated
  cgroups below its service, including separate harness and exec-server budgets.
- The [VM test guest](../system_tests/k3s/guest/guest_init) uses BusyBox as PID 1, builds those
  cgroups itself and restarts runner in a shell loop. It does not test systemd supervision.
- The [Pod executor](../runner/docs/codex_executor.md#pods) has a separate container and private PID
  namespace. Its PID 1 exits on ownership loss; a fresh, idle generation proves runtime cleanup.
  Harnesses sharing the runner container still use process-group supervision.

## Ownership and contract

Keep the shared process-control boundary small. It must represent the following operations without
exposing systemd units to Session code or teaching COSI controllers about cgroup files:

| Operation | Required meaning                                                                                                                                                               |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Launch    | Use a private launch identity, exact command/environment/cwd, identity, stdio and resource policy. Accept one launch attempt; distinguish not-started from an uncertain reply. |
| Observe   | Report native identity, running/exited state, exit code or signal, and diagnostic stderr. A PID is diagnostic, not durable ownership or a readiness signal.                    |
| Stop      | Request bounded graceful termination of the intended process. Caller cancellation does not establish termination.                                                              |
| Fence     | Prove the named execution domain contains no surviving work before allowing a successor or reporting cleanup complete. Failure is a barrier, not permission to continue.       |
| Release   | Consume final status and release backend references only after required cleanup and output draining.                                                                           |

Launch identities belong to the existing Sandbox/runner incarnation and one attempt. Assign identity
before the effect and retain it while an outcome is uncertain. A lost D-Bus reply must be resolved
by observing that same launch, not by allocating another unit and repeating the effect. A new runner
discovers and fences prior owned work before opening Session stores or admitting native execution.
Use existing ownership and journal evidence; introduce no second durable command store.

Preserve separate stdout/stderr pipes and verbatim native frames. Pass descriptors through the
backend; systemd's journal is not a replacement for native transport or Runner's Event journal.
Harness readiness remains the existing native handshake/RPC probe, after successful process launch.
Launch cancellation must clean up an accepted launch or leave an observable attempt that its owner
must resolve. Canceling an ordinary observation or wait does not cancel accepted work.

The fencing domain is explicit. A VM service cgroup can contain one harness and its descendants;
the Pod executor's strong fence covers its whole container. Process-group cleanup of one harness
inside a shared container does not prove that descendants which escaped the group are gone. Do not
silently widen a stop into killing unrelated Sessions or claim identical isolation across backends.
Work delegated to external daemons remains outside these process-tree guarantees.

COSI alone decides when to launch, stop or retry. Systemd native units have automatic restart
disabled. The existing outer runner restart policy remains a platform responsibility. A failed
systemd operation does not select the subprocess backend as a fallback.

## VM backend

Use systemd's D-Bus [StartTransientUnit](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)
API. Create transient `Type=exec` services with the configured agent UID/GID, explicit environment,
working directory, inherited stdio descriptors and limits. Transient describes runtime creation;
these are long-running services, not `Type=oneshot` jobs. Follow systemd unit/job observations in
place of `exec.Cmd.Wait`. Keep references long enough to consume exit evidence before unit garbage
collection; release them once the result and cleanup are accounted for.

Runner initially retains its existing trusted VM identity. Native services retain unprivileged
credentials, capability restrictions and no-new-privileges behavior. Neither harnesses nor tool
processes receive the manager connection or authority to create arbitrary system units. Unit names
identify owned launches; they do not grant ownership of unrelated units.

Tie owned native units to runner lifetime so runner death initiates bounded graceful cleanup, then
forces remaining descendants out. Preserve state-owner exclusion across that interval. A runner
lock becoming available, a closed pipe, a completed stop request or a vanished main PID alone is
not proof that the old execution domain is empty. The implementation must establish the ownership
descriptor/reference behavior and startup fence under real systemd before removing VM supervisors.

### Preserve the resource envelope

Today [`guestResourceProperties`](../runner/guest.go) applies the total budget to
`agentplane-runner.service`. A separately managed service would otherwise sit outside that budget.
Before moving the first exec-server, move the common envelope to an ancestor slice which contains
both runner and every managed native service. The target hierarchy is:

```text
agentplane.slice                         configured guest workload envelope
  agentplane-runner.service              control process
  agentplane-workloads.slice             configured harness/script aggregate
    agentplane-harness-<launch>.service   configured native-process budget
  agentplane-executors.slice             configured executor aggregate
    agentplane-exec-<launch>.service      exec-server and its tool descendants
```

Derive properties from the existing validated resource configuration. Preserve CPU, memory high/max,
PID and per-device I/O limits, swap policy and the OS/control-process headroom checks. Do not copy
the old total cap onto runner alone and leave new services unaccounted for. During the exec-server
slice, remaining delegated harness cgroups may stay under runner, but all work must already share
the common envelope and each budget must have one owner.

Preserve the tested OOM behavior: a tool may be killed while exec-server and app-server survive.
Systemd's unit OOM policy and cgroup OOM grouping must not turn that single tool failure into
automatic termination of the whole unit. Test resulting properties and actual kernel behavior;
do not promise that the kernel will always select a particular victim.

## Implementation PRs

1. **This contract and plan.** Record ownership, the resource hierarchy, fencing semantics and the
   proof matrix. Runtime behavior is unchanged.
2. **Real systemd in the VM fixture.** Keep the existing direct kernel boot and lightweight root
   image. Replace the guest's PID 1 supervision loop with real systemd and a working system bus;
   BusyBox can remain in the initramfs. Share production's runner unit/resource policy rather than
   reproducing it in another shell loop. Support the same API features in the pinned fixture and
   production systemd versions. Preserve mounts, network setup, the read-only cgroup observer,
   orderly shutdown, restart and workspace/state unmount ordering. Existing VM tests must pass
   before changing native supervision.
3. **VM exec-server backend.** Add the process-control seam with the existing subprocess backend
   and the real systemd implementation. Move the VM exec-server and common resource envelope in
   this change; retain container execution behavior. Remove the old VM exec-server launch path
   when replacing it. Startup, health/restart policy and quiesce remain COSI-owned.
4. **Remaining VM launches and cleanup.** Migrate Claude, the shared Codex app-server,
   setup/bootstrap scripts and workspace helpers. Account for short-lived helper semantics and
   credential changes as well as long-running harnesses. Remove superseded VM supervisor,
   delegated-cgroup and capability-clearing launch plumbing once it has no remaining VM caller.
   Retain code still used by the container backend. Update docs and delete this plan when complete.

PRs may stack; fixture availability is required for the backend's acceptance, not a reason to defer
independently specifiable code. No backend selector, protocol change or platform privilege is added
by this documentation PR.

## Proof matrix

Use real systemd and kernel processes for VM supervision claims, with the existing scripted model
fixtures for native commands. Unit tests can cover property projection and event handling, but
cannot replace the real-manager rows below.

| Scenario                                      | Evidence required                                                                                                                         |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Ordinary start/stop                           | Native handshake, correct UID/GID/cwd/environment, separate streams, exit result, empty execution domain and released references.         |
| Invalid launch or manager failure             | No readiness or unaccounted process; bounded error and no silent backend fallback.                                                        |
| Lost start reply/canceled start               | Resolve the original launch identity; no duplicate process or repeated native command.                                                    |
| Native leader exits with detached descendants | Entire owned domain is fenced before reporting completion or starting its successor.                                                      |
| Runner dies while native work is active       | Graceful opportunity to save resume state, bounded forced cleanup, and no successor execution or journal append before fencing.           |
| Stop/restart/Quiesce race                     | One lifecycle owner, no late readiness, output drained before sealing, and existing exact seal/ingress acknowledgement ordering retained. |
| Tool OOM and resource pressure                | Limits apply under the common envelope; existing single-tool OOM and follow-up-turn behavior remain covered.                              |
| Backend restart/recovery                      | Owned old units are found and fenced without touching unrelated units; ambiguous native effects are reconciled rather than replayed.      |
| Container regression                          | Existing private-namespace executor restart/fencing, local harness restart and Pod suspend/resume remain valid.                           |

The existing [VM coverage](../system_tests/k3s/README.md#vm-sandboxes) is a starting point, not evidence
for systemd until the guest is changed. Extend the relevant native restart and VM lifecycle targets;
keep final unit states, cgroup membership and journal evidence in test outputs. Automated coverage
is the acceptance path; this work adds no manual UI drive or production fault-injection gate.

## Boundaries and rollout

Central Codex placement is a [separate track](task_dag.md#central_codex_spike--a-codex-app-server-outside-the-sandbox-drives-its-exec-server).
Local harnesses remain supported throughout adoption. This work neither moves sampling/state out
of Sandboxes nor adds a supervision daemon or systemd to containers.

The backend contract permits a future direct-cgroup VM implementation. Making runner PID 1 would
also require guest boot, signal/reaping and VM recovery work; it is not a backend flag change.

Backend adoption must coordinate the guest's unit/bus support, runner disk and template resource
configuration. Publish prerequisites before selecting the backend. Keep ordinary Pod rollout
independent; do not revive or delete active Sandboxes or retained storage to complete a migration.
Each runtime PR must name matching rollback revisions and the automated startup/fencing proof.
