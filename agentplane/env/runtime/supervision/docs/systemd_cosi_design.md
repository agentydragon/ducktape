# Systemd supervision through COSI: design for review

**Direction approved, 2026-11-01.** This compares the existing generic process controller with a
separate systemd-unit controller. The first implementation registers the embedded VM exec-server
with the process controller, publishes attempt-scoped observations through it, moves workspace
diagnostics to that status, and removes an unused independent unit-reference API. The implemented
contract is described in the [supervision documentation](supervision.md).

## Recommendation

Keep one COSI lifecycle owner per native launch: extend the existing process controller and its
systemd backend to expose the observations that its callers actually need. Keep systemd-specific
manager operations behind that backend. Do not introduce a second COSI lifecycle controller until
there is a consumer that owns units independently of a supervised process, or a demonstrated
reduction in code and failure modes that outweighs the added coordination.

This is a design direction, not a claim that moving all systemd logic into `Reconcile` is simpler.
Start, stop and observation can block; the controller must remain responsive to other resources.
The backend may retain bounded workers and process-owned handles, and publish facts that wake the
existing controller. The benefit must be measured as deleted duplicated lifecycle/bridge logic,
not as the number of types converted to COSI.

### Alternatives

| Option                                                        | Ownership and what it could remove                                                                                                                                                                                                                          | Cost and limits                                                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Extend the process controller **(recommended)**               | The existing controller owns attempt phase and finalization; the systemd backend owns manager interaction and projects typed facts. Consolidate duplicated observations and job/connection bookkeeping only where a single owner can actually replace them. | Does not eliminate platform-specific fencing or live handle registries. A single backend still has to solve disconnects, uncertain starts and retained exit evidence.                                                                                                                                                                            |
| Add a `SystemdUnit` resource and controller beneath `Process` | A unit controller could own reusable D-Bus operations and expose unit state to consumers that do not own native processes. Generic process reconciliation would depend on that unit's observed attempt and finalizer.                                       | Adds a second desired state, status and teardown barrier for every VM launch. It must define who opens streams, which owner may start or stop, how attempts are correlated, and which finalizer waits for the other. A unit status alone does not prove process cleanup or effects. No current independent consumer justifies this coordination. |
| Keep current design                                           | No migration or new lifecycle layer. Existing systemd fencing and generic controller already cover the critical containment boundary.                                                                                                                       | Systemd facts and native process facts remain split between a registry, callbacks and a backend; bespoke job and reconnect handling remains difficult to inspect. This is preferable if a focused prototype cannot reduce that complexity safely.                                                                                                |

## Current ownership and scope

The [existing supervision contract](supervision.md) is the authority for current behavior. Runtime,
Host and Execution share a management process, COSI engine, namespace, and process controller.
Each retains its own process domain and lifecycle policy. Embedded VM Execution registers its exec-server with Runtime's process
controller; standalone Execution retains its own supervisor. The generic [process controller](../controller.go) owns
`ProcessDesiredStates`, `ProcessStatuses` and a finalizer per attempt; [Host](../host.go) owns
the in-memory launch callback, start/exit/stop slots and process handle. [`ManagedProcess`](../managed_process.go)
provides the native streams and waits for that controller's cleanup barrier.

On VMs, [`SystemdLauncher`](../systemd_launcher.go) prepares each owner's scope and fences stale
units before admission. [`systemdBackend`](../systemd.go) owns each transient unit's manager
connection, name, streams, observed invocation and cgroup, job tracking, exit observation and
fence. Systemd owns the actual unit, configured signal/escalation behavior and cgroup. Runtime,
Host and Execution own readiness, successor admission, journals and restart policy above this
boundary. COSI does not make the backend's file descriptors, D-Bus references or state durable.

There is one packaged management unit, `agentplane-environment-runtime.service`, not separate
packaged Host and Execution units. Systemd starts and restarts that Runtime unit outside the
process it owns; Runtime cannot supervise its own service through an in-process controller. Its
unit and owner marker remain the outer lifetime boundary. The proposed controller covers the native
transient workloads in the workload and executor slices, including harnesses, setup helpers and
the exec-server. It does not change slice or identity policy. In Pods the existing PID 1 or
subprocess backend remains responsible for its process domain; the executor companion remains a
separate container and PID namespace.

## Proposed resource and handle boundary

These are logical shapes to review, not an API or a promise to store every field in a COSI
resource. Preserve the current process-domain and owner boundaries and use the existing desired/status
resources unless an actual caller needs a different projection.

| State            | Owner and contents                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Attempt intent   | The caller requests one immutable attempt ID, owner and generation, start deadline and stop grace. Launch configuration is fixed for that attempt: executable/arguments, working directory, explicit environment, identity, slice, limits and stream setup. Do not copy secrets or live descriptors into broadly readable resources; hold sensitive configuration and stream handles in the process-owned registry. |
| Systemd identity | The backend allocates a unique unit name before `StartTransientUnit`. It correlates that name to the generic attempt ID and owner scope. Systemd's `InvocationID` is an observed execution identity, not the attempt ID or proof of acceptance by itself.                                                                                                                                                           |
| Observed status  | The backend reports attempt-correlated start acceptance/uncertainty, unit state and job observation, exact invocation and captured cgroup where known, PID, and exit result or an explicit unknown. The process controller publishes the facts callers need and preserves a terminal outcome until intent is retired. Do not report success from an absent unit or a stop request alone.                            |
| Cleanup status   | Track stop requested, manager/job observation, and a separate domain-fenced fact with its evidence. Successful application shutdown and empty cgroup are distinct facts. An unknown or failed fence leaves teardown and successor admission blocked.                                                                                                                                                                |
| Live state       | The backend owns connections, subscriptions, `AddRef`/replacement references, job handles, stream file descriptors and workers. None is serialized into a resource; losing a Runtime loses those handles. The controller's finalizer is a barrier within that Runtime lifetime, not a durable distributed lock.                                                                                                     |

The backend remains the only systemd operation owner for an attempt. The process controller owns
its COSI phase and finalizer; higher-level callers never independently stop the same unit. For
example, publish `Starting`, `Running`, `Stopping` and retained `Exited` through the existing
process status; represent systemd detail as typed attempt-scoped facts rather than parallel
independent desired state. The generic status contains a zero-valued, absent observation for Pod
processes; it does not invent systemd state for them.

## Lifecycle sequences

1. **Launch.** The higher-level owner admits a new attempt and registers its immutable launch
   configuration and callback. The process controller installs its finalizer, rechecks teardown,
   and claims exactly one start slot. The VM backend allocates the name and sends `StartTransientUnit`
   over the system API bus with `AddRef`, `Restart=no`, the Runtime owner binding, invocation
   marker, configured slice, identity, limits and streams. Job acceptance,
   job completion and observed `InvocationID` are distinct. Publish started only after the existing
   start contract is satisfied; cancellation joins any late start and fences its domain.
2. **Normal exit.** Observe the exact invocation's terminal exit while its unit reference retains
   the result. A notification is a hint: re-read authoritative manager properties. Initiate the
   ordinary stop/domain fence even after leader exit. Retain exit outcome separately from cleanup;
   publish terminal process status and release the finalizer only once the owned domain is empty.
   Reconciliation of that terminal attempt never starts it again. A later attempt requires an
   explicit higher-level admission decision and a new identity.
3. **Stop and teardown.** Teardown joins any in-flight start, closes input as appropriate, and
   asks systemd to stop the same unit. Systemd owns TERM, escalation and cgroup kill. An accepted
   stop or a completed stop job is not the fence. Observe the expected invocation and prove the
   saved domain empty using the existing manager and kernel rules. Preserve unknown exit outcome
   even if cleanup succeeds; preserve the finalizer and block successor admission if cleanup is
   unproven. Runtime's Host/Execution quiesce, History seal and outer-container barriers remain
   separate from this native-unit result.
4. **Uncertain start.** If the start call loses its reply or the caller cancels, never submit a
   new start for that attempt. The original live manager connection can order a read/fence behind
   its request. On a replacement connection, absence alone does not prove an old request cannot
   arrive; a discovered matching loaded unit can reconcile acceptance, otherwise keep the attempt
   blocked until the existing ordered-fence or outer owner-lifetime proof establishes safety.
   A process-domain fence does not settle whether a command already produced external effects.
5. **Disconnect and reconnect.** Stop trusting lost subscriptions and re-read manager state on a
   replacement API-bus connection. A sender-bound `AddRef` is lost with its connection; acquire a
   replacement reference where possible before the unit is collected. Keep the exact invocation
   and captured cgroup; never adopt a different invocation as the old attempt. If exit evidence was
   collected, report it unknown. Retry observation/fencing within the backend's owned lifetime;
   no requeue, transient error or new connection authorizes replay.
6. **Runtime loss and reconstruction.** In-memory COSI state, finalizers, pipes and D-Bus handles
   disappear with the process. The systemd Runtime unit's lifetime binding and invocation marker
   prevent old owners from executing late starts. On restart, acquire the environment/Host/Execution
   owner locks and perform the existing scope inventory and fencing for each owner and expected
   slice before admitting new native work. Failure to establish that boundary blocks startup or
   admission. Reconstruct only facts supported by systemd/kernel and durable journals; do not
   recreate lost exit status, stream handles or uncertain command effects. A lock alone is not a
   process-domain fence.

## Review and evidence

The design replaces a proposed additional unit controller with a single attempt owner; it does
not promise to delete the code that does real systemd work. Review this change and later work against:

- Whether the status consumers need any systemd-specific facts beyond the existing generic process
  status, and whether those facts can be exposed without duplicating a lifecycle state machine.
- Which existing connection, subscription, stop-job and reaper paths a bounded prototype actually
  removes. In particular, a shared connection changes the failure and `AddRef` blast radius; do not
  assume pooling is safe or required.
- A race-tested transition matrix for normal exit, cancellation during start, accepted start with
  lost reply, disconnect before and after acceptance, unit collection, changed invocation, failed
  stop, populated descendants and Runtime restart. The old attempt must never be replayed and a
  successor must never cross an unresolved fence.
- The existing PID 1/subprocess process tests and the Pod and VM system suites for any later
  implementation that changes their shared controller or shutdown contract. Include the real
  systemd kernel fixture for changed manager/fence behavior; successful app shutdown must not stand
  in for cgroup emptiness.

The first implementation brings the embedded VM executor under the process controller, exposes the
VM backend's observations and removes the workspace helper's independent diagnostic query and an unused unit-reference API. The
manager backend remains the owner of real fencing and live handles. Further changes should delete
duplicate ownership and preserve reconstruction and fencing guarantees. A dedicated unit controller
remains an alternative for a later independently owned unit consumer, not an implicit follow-on.
