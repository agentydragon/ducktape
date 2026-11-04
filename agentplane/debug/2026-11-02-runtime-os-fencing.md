# Runtime OS fencing and COSI

> Historical investigation: the subprocess backend and its owner descriptor were subsequently removed; see the [current supervision contract](../env/runtime/supervision/docs/supervision.md).

Review of `2b9b7763675af4e3a1cfe8d8f3efe85e6fa82f27`: COSI already owns native-attempt
finalization, but none of the external fences can be replaced by an in-process finalizer.
The removable exception is the legacy Runner hash for Runtime's systemd scope. Use the
same explicit scope identity for Runtime, harness and Execution.

## Scope-identity cutover

**Deploy this hash change only into fresh Sandbox VMs.** Destroy and confirm termination of
the previous VM before creating its replacement. Do not update the Runtime executable inside
an existing guest: the new Runtime scope prefix will not inventory old-prefix native units.
The same fresh-VM requirement applies when reverting this change. Retained user-authored
storage need not be deleted; this precondition concerns the old guest and its processes.
There is no compatibility scan or old-format test.

Within the new version, restarts derive the same scope from state directory, Sandbox UID and
scope ID. `PrepareForOwner` inventories that prefix, checks the expected slice and fences each
unit before returning a supervisor. The invocation marker additionally rejects old queued
starts after a replacement's inventory. Harness and Execution hashes do not change.

## Ownership and counterexamples

| Mechanism                                            | Owner and purpose                                                                                                                                                                                                                                      | Failure if replaced by COSI finalization                                                                                                                                                       |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fixed-inode `flock`                                  | [`environmentOwner`](../env/runtime/environment_owner.go) excludes overlapping Runtime processes before opening state.                                                                                                                                 | Two independent Runtime processes have separate COSI engines; each can install its own finalizer and open the same state.                                                                      |
| Trusted inherited owner descriptor and liveness pipe | `subprocessBackend` and `SupervisorMain` retain ownership and initiate cleanup after management-process loss.                                                                                                                                          | Runtime SIGKILL destroys its finalizers while native children still execute. A native may close its descriptor; the trusted supervisor must retain its own copy.                               |
| PID1 reaper and outer container fence                | [`pid1Backend`](../env/runtime/supervision/pid1.go) owns child waits and same-group cleanup; the container runtime fences escaped groups on init death.                                                                                                | A child can call `setsid` and retain a pipe. A process-group kill or released state lock cannot prove the namespace empty. PID1's files close before namespace exit cleanup finishes.          |
| Systemd scope inventory and cgroup fence             | [`SystemdLauncher`](../env/runtime/supervision/systemd_launcher.go) and [`FenceSystemdUnit`](../env/runtime/supervision/systemd_units.go) reconcile old units and require manager/kernel cleanup evidence.                                             | Runtime dies and loses its process registry. A leader's exit, successful stop request or absent unit on a replacement connection does not establish descendant death or settle an old request. |
| Runtime unit binding and invocation marker           | [`systemdBackend`](../env/runtime/supervision/systemd.go) sets `BindsTo`, `After` and `ConditionPathExists`; the packaged [Runtime unit](../env/images/systemd/agentplane-environment-runtime.service) removes its runtime directory at service death. | A delayed old start arrives after successor inventory. Binding only to the service name can bind it to the successor invocation; the old invocation's missing marker prevents exec.            |
| Attempt finalizer                                    | [`Controller`](../env/runtime/supervision/controller.go) holds teardown until its registered backend reports a proven fence.                                                                                                                           | Removing the finalizer permits resource teardown and successor admission while a live backend is still cleaning up. Conversely, retaining it does not kill any process by itself.              |

`Controller` checks the finalizer again before starting, joins late launch completion during
teardown, and keeps failed fences in `Stopping`. Its Host registry owns the single start/stop
slots. These are local coordination guarantees. Persisting a resource would not make an
unknown start safe to replay or make an unavailable OS manager prove its domain empty.

## Existing checks and limits

- `supervision_test`: scope separation, finalizer retention on failed cleanup, late launch
  cleanup, single-start behavior and systemd ordered/unordered fence rules.
- `runtime_test`: `TestHostSubprocessRetainsRuntimeFenceAfterManagementProcessDeath` freezes the
  trusted supervisor, kills Runtime, verifies lock exclusion while children live, then resumes cleanup.
- `env/e2e:test_pid1_container`: private PID namespaces, native descriptor closure,
  TERM-resistant descendants and escaped groups; whole-container exit precedes replacement.
- `system_tests/suites:test_vm`: boots the real-systemd fixture, including
  `startup_fences_owner_scope_only`, uncertain starts, missing invocation markers and parent
  death. Its public VM setup scenarios additionally check detached-child cleanup.

These are source-reviewed coverage boundaries, not fresh execution results. CI results belong
to the PR head. No live deployment, forced node loss, old-guest upgrade, or command-effect replay
is validated by this review. A future external-fence removal needs an independently surviving
owner with equivalent exclusion, cleanup and late-request rejection; COSI alone supplies none.
