# Execution service contract

The execution service provides typed process and filesystem operations for one execution
environment. The public contract is [`executor.proto`](executor.proto). Its initial backend is
Codex exec-server; running a native agent harness or app-server is not required.

## Ownership and admission

One Execution API lifetime owns one immutable Sandbox UID and incarnation and one native backend
lifetime. In the normal topology it runs inside Environment Runtime; a standalone binary remains
available for isolated use. Embedded Runtime calls the adapter directly after its backend readiness probe;
its private Unix socket serves health only for Runtime's startup verification and remains bound for
the embedded service lifetime. Host asks Runtime for in-process readiness. Runtime publishes the adapter
only after a backend readiness probe and a successful health check on that socket. Standalone mode serves Executor RPCs
on that socket, where filesystem access grants the environment's execution authority. Every
call carries the destination, incarnation and attributing Session; a foreign destination or stale
incarnation is rejected before native dispatch. The service cannot adopt another environment or
advance a Runner watch epoch. Callers supply Session and incarnation context with each request;
Environment Runtime owns service health and lifecycle fencing. Gateway
retains remote authorization and routing. When the expected incarnation is stale, Gateway or
Environment Runtime may attach the typed `SandboxIncarnationChanged` status detail only after
verifying the same destination Sandbox UID and observing a nonempty current incarnation. Invalid or
foreign destinations and unavailable incarnations carry no rebind detail.

After caller authorization and exact Sandbox UID resolution, Gateway may attach
`ExecutionEnvironmentFailure` when it refuses a request before forwarding it to Runner. A scoped
Runtime may attach a local readiness state only when it refuses before native dispatch. The state
distinguishes suspended, starting, not-ready, quiescing and unavailable control routing; the
outcome is `NOT_DISPATCHED`. The detail contains no replacement identity and grants no wake, resume
or provisioning authority. Invalid, foreign or stale Sandbox identities, an unsynchronized
inventory and uncertain Kubernetes reads receive no Gateway environment detail. Legacy Runner
proxy calls keep their existing status behavior and do not receive this execution detail.

The state-directory owner lock precedes socket replacement and native startup. Pod ownership is
PID 1 plus its private process namespace. On ordinary release, the companion stops exec-server,
fences every remaining process in that namespace, and publishes a new idle generation only after
the process table contains no workload process. It keeps PID 1 alive for the next owner. If
exec-server exits unexpectedly, the companion performs the same process-domain fence and closes the
ownership lease so Runtime can recover the backend. If the in-container fence fails, the companion
exits and the outer runtime's whole-namespace cleanup supplies the stronger fence. VM ownership is the Runtime systemd unit, its lifetime marker and the explicit `execution-service`
scope in `agentplane-executors.slice`, with startup fencing and stop proof from the shared supervisor.
Standalone systemd use requires an explicit owner unit and lifetime directory. No second owner may start native work while the prior owner remains live. A
failed fence is not permission to launch a successor. Before taking the lock, the service opens its
state directory without following symlinks and enforces mode `0700`; this restores private access
when a persistent-volume remount adds `fsGroup` permissions.

## Sessions and effects

`OpenSession` owns one adapter connection and native WebSocket. It initializes a native session or
resumes the requested identity. All connections in an environment share one exec-server. An
execution session is separate from a persisted sampler conversation. Eight concurrent open or
opening connections are admitted; active connections remain production autosuspend blockers.
Native frames are limited to 1 MiB, with bounded notification backpressure.

Closing the stream releases adapter connections and workers; it does not send `process/terminate`.
Codex 0.162.1 retains a detached native session for 30 seconds; expiry removes it and terminates
remaining processes. File handles belong to the connection. Resuming a retained session preserves
process IDs and output sequence cursors. Restarting the backend loses its native session registry,
processes and handles; no reconstruction replays prior commands.

A canceled unary RPC ends that caller's wait. An admitted native operation still belongs to the
held session and may have taken effect. Explicit process cancellation uses the typed terminate or
signal operation. Native replies, including errors, remain evidence in returned frames; clients own
retaining that evidence. Read calls preserve native cursor semantics. Process `cwd` and file `path` fields are absolute
`file:` URIs; `argv` entries remain ordinary process arguments.

Request IDs correlate calls and evidence; they provide neither deduplication nor admission receipts.
Lost mutation acknowledgements remain outcome-unknown. No transport or service layer automatically
retries starts, writes, signals or other mutations. The adapter has no durable mutation journal.

## Shutdown

Embedded Runtime publishes its desired incarnation to the Execution COSI controller. The
controller owns each embedded API start, readiness, backend-loss transition, and completed fence
generation. A lost backend closes admission and joins forwarded calls before the old service's
native process-domain fence is accepted. It may then start another lifetime for the same running
incarnation; quiescing and closing suppress that recovery. A failed or unknown fence retains the
old owner and blocks every successor. Each admitted startup belongs to one caller epoch and
desired-state demand. If that epoch is canceled or a newer demand arrives before readiness is
published, the old startup is fenced before any successor is admitted. A proven fence returns
the startup's readiness error to its caller; an uncertain startup fence remains terminal.

The Execution API closes admission and held connections, joins direct adapter calls (or drains
standalone gRPC calls), and stops its native
backend before releasing ownership. Pod cleanup fences all remaining processes in the companion's
private namespace, including descendants that escaped a process group; if that proof fails, Pod
exit invokes the runtime's whole-namespace fence. VM cleanup proves its execution cgroup empty;
Runtime death also removes its lifetime marker and stops bound transient units. A backend exit ends
the API lifetime; the controller may create a replacement only after the old lifetime returns fenced proof.

Authored workspace files survive service shutdown. This contract promises no persistence for the
native backend's in-memory session registry; its loss must not be reported as successful execution
or permission to replay an uncertain operation.
