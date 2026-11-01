# Environment Runtime

The Environment Runtime is the authority for one Sandbox environment's identity, bootstrap,
legacy environment-owner lock, durable quiesce marker, Gateway control listener, and lifecycle of
its Harness Host and standalone Execution service. It serves `EnvironmentRuntime.Connect` to the
Host on a private Unix socket and the existing Runner API to Runner Gateway. The Host owns Session
records, journals, native harnesses and its separate owner lock.

## Retained state

Runtime opens `StateDir/.agentplane-runner-owner` and keeps that same file inode locked for its
lifetime. It does not rename or replace the file during the service split. This prevents a queued
legacy Runner scope or an older Runtime from overlapping the new owner. The Host lock is separate
at `StateDir/harness-host/.agentplane-runner-owner`.

The durable seal remains `StateDir/sessions/sealed`, with the existing incarnation UID bytes as
its payload. Runtime reads, removes, and writes this marker using the old filename, directory,
payload and durability semantics. Session records, journals, native continuations and History ACK
positions remain under their existing `StateDir/sessions` paths; Runtime does not move them.

## Lifecycle boundary

Each Host connection begins with `HostHello` and then reports the exact Runtime boot ID, Host boot
ID and monotonically increasing environment generation it applied. Runtime sends a
`LifecycleIntent` containing the Sandbox UID, incarnation UID, mode and seal UID. The initial mode
is `INACTIVE`. Session RPCs are forwarded only after the Host reports `ready` for the exact binding.
Requests carry the three binding values as metadata; Host checks them inside its admission barrier.

For quiesce and replacement, Runtime first asks Host to stop admissions and drain native work.
`native_stopped` is reported only after admitted setup, Session calls, shared app-server work and
native-process finalizers settle. Runtime then stops and fences the Execution service and publishes
`execution_fenced` for that same generation. Host can seal and flush Session journals only after
both facts match the requested binding. Runtime treats the History seal acknowledgement as a
separate required boundary. It never infers a seal or flush from process exit or an Execution fence.

Runtime tracks in-flight public admissions synchronously so a newly admitted Session invalidates
an idle-quiesce token before Host facts are sampled. A canceled Gateway waiter does not cancel
accepted bootstrap work or leave a canceled drain half-applied: a reconnect must finish the
current Host native stop and Execution fence before reopening, even when the incarnation UID is
unchanged. Replacement never starts a successor Execution service before the previous one is
fenced.

SIGTERM follows the same Host drain and Execution fence path. When shutdown does not have an
incarnation seal request, Runtime sends `CLOSING` with an empty seal UID; this drains local work
without claiming a durable History seal. Runtime waits for accepted bootstrap work to settle before
releasing its legacy owner lock. A failed bootstrap is a settled durable failure and does not by
itself make process cleanup fail.
While bootstrap is running, Runtime reports the `Bootstrap` autosuspend blocker; it clears when the
bootstrap reaches a terminal state.

VM Host and Execution units, their transient process units, and their resource slices do not receive
implicit shutdown-target stop jobs. Runtime owns their stop order, draining Host and fencing
Execution before stopping either service; each transient process is bound to its owning service. The
Runtime unit holds state and workspace mounts until those stops finish. Its systemd stop timeout is
unlimited because accepted bootstrap work is joined to durable settlement; the lifecycle fence itself
has a 150-second deadline.

## Process topology and fence proof

The executable names below are fixed package contracts. Their systemd definitions live in
`../runner/systemd/` until the image and Nix assets move to `env/images`.

| Mode                              | Runtime and Host                                                                                                                                     | Execution service                                                                                                                                    | Fence evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Production Pod (`pid1`)           | Runtime is container PID 1 and supervises Harness Host as an explicit subprocess.                                                                    | Runtime supervises the standalone Execution API process. The Codex companion is a separate Kubernetes container running as PID 1.                    | Normal replacement requires Host `native_stopped`, Execution service's clean fenced exit, and Runtime's process waits. An unexpected clean Execution exit triggers a same-incarnation replacement only after the old child is joined; nonzero exits fail Runtime. An abnormal Host exit fails Runtime without seal authorization; Runtime exits so the container runtime fences the whole Pod process cgroup before replacement.                                                      |
| VM (`systemd`)                    | Runtime, Host and Execution service are separate systemd units. Runtime starts them over D-Bus and retains each exact invocation ID and cgroup path. | Execution service's own unit uses the configured systemd backend for Codex and has `Restart=no`; Runtime monitors its named Executor Health service. | A health loss starts recovery only after the exact old unit invocation is inactive, its cgroup is empty, and its main process exited successfully. Runtime then starts a replacement and waits for named Health to be `SERVING` before admitting new calls. A failed/nonzero invocation, changed or unobservable invocation, or fence error fails Runtime and retains the old owner. The 150-second Runtime fence deadline covers the Execution Service unit's 120-second stop bound. |
| Explicit local/RBE (`subprocess`) | Runtime starts Host as a child process. It is a test/development mode and does not imply PID 1 containment.                                          | Runtime starts the Execution API as a child process; companion topology remains externally configured.                                               | Only normal child waits and reported native-stop facts are available. This mode does not claim production Pod cgroup containment.                                                                                                                                                                                                                                                                                                                                                     |

VM units are `agentplane-environment-runtime.service`,
`agentplane-harness-host.service`, and `agentplane-execution-service.service`. The Runtime keeps the
legacy `agentplane-runner` scope identity during cutover; Host and Execution use the explicit scope
IDs `harness-host` and `execution-service`. Their lifetime directories are respectively
`/run/agentplane-environment-runtime-lifetime`, `/run/agentplane-harness-host-lifetime`, and
`/run/agentplane-execution-lifetime`. Runtime does not enable or start the Codex companion unit;
Pod orchestration owns that separate container.

The fixed VM service commands use the mounted configuration at
`/run/agentplane/config/runner.json` and launch identity at
`/run/agentplane/sandbox/launch.json`. Runtime also accepts optional
`--harness-host-binary` and `--execution-service-binary` overrides for tests; production resolves
the sibling binaries from its mounted disk. `--vm-resource-properties <runner.json>` remains the
systemd prestart interface for calculating the environment resource envelope.

## Configuration and startup

Runtime accepts `--config <runner.json> --launch-file <launch.json>`. Resolved supervision is
explicit: `pid1` requires a Pod PID namespace, `systemd` requires the VM `SystemdLauncher`, and
`subprocess` is selected explicitly for local/integration runs. Runtime never silently falls back
between them. The same immutable config files are passed to Host; Runtime writes the per-incarnation
Execution service config with mode `0600`.

For Pod and explicit subprocess modes, Runtime and Host use short private sockets directly under
`StateDir` (`r.sock`, `h.sock`, `e.sock`) so test and deployment state paths remain within the
Unix-domain socket path limit. Execution keeps its ownership lock under `StateDir/execution` and
binds the configured `e.sock` path. VM mode retains the existing explicit `/state/runtime`,
`/state/harness-host` and `/state/execution` socket locations used by its systemd unit arguments.

On startup Runtime locks the legacy owner inode before opening the old Session directory, fences
previous Host and Execution owners, binds its private lifecycle UDS with mode `0600`, starts Host,
and waits for an exact-binding initial `INACTIVE`, `ready`, `native_stopped` report before opening
Gateway control. A Gateway incarnation is not admitted until the previous incarnation has completed
its lifecycle and process fences.
