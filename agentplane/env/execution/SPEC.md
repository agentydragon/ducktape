# Execution service contract

The execution service provides typed process and filesystem operations for one execution
environment. The public contract is [`executor.proto`](executor.proto). Its initial backend is
Codex exec-server; running a native agent harness or app-server is not required.

## Ownership and admission

One standalone service owns one immutable Sandbox UID and incarnation and one native backend
lifetime. Access to its private Unix socket grants that environment's execution authority. Every
call carries the destination, incarnation and attributing Session; a foreign destination or stale
incarnation is rejected before native dispatch. The service cannot adopt another environment or
advance a Runner watch epoch. Harness Host supplies the current Session and incarnation context over
its private connection; Environment Runtime owns service health and lifecycle fencing. Gateway
retains remote authorization and routing. When the expected incarnation is stale, Gateway or
Environment Runtime may attach the typed `SandboxIncarnationChanged` status detail only after
verifying the same destination Sandbox UID and observing a nonempty current incarnation. Invalid or
foreign destinations and unavailable incarnations carry no rebind detail.

The state-directory owner lock precedes socket replacement and native startup. Pod ownership is
PID 1 plus its private process namespace. On ordinary release, the companion stops exec-server,
fences every remaining process in that namespace, and publishes a new idle generation only after
the process table contains no workload process. It keeps PID 1 alive for the next owner. If
exec-server exits unexpectedly, the companion performs the same process-domain fence and closes the
ownership lease so Runtime can recover the backend. If the in-container fence fails, the companion
exits and the outer runtime's whole-namespace cleanup supplies the stronger fence. VM ownership is
a systemd unit, its lifetime marker and explicit scope, with startup fencing and stop proof from the
shared supervisor. No second owner may start native work while the prior owner remains live. A
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

The standalone service closes admission and held connections, drains gRPC and stops its native
backend before releasing ownership. Pod cleanup fences all remaining processes in the companion's
private namespace, including descendants that escaped a process group; if that proof fails, Pod
exit invokes the runtime's whole-namespace fence. VM cleanup proves its execution cgroup empty;
service death also removes its lifetime marker and stops bound transient units. A backend exit ends
the service, without starting a replacement inside the same service lifetime.

Authored workspace files survive service shutdown. This contract promises no persistence for the
native backend's in-memory session registry; its loss must not be reported as successful execution
or permission to replay an uncertain operation.
