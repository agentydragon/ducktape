# Execution service contract

The execution service provides typed process and filesystem operations for one execution
environment. The public contract is [`executor.proto`](executor.proto). The native backend owns execution state; a Codex-shaped WebSocket interface supports the Codex harness.

## Ownership and admission

One Execution API lifetime owns one immutable Sandbox UID and incarnation and one native backend
lifetime inside Environment Runtime. Runtime publishes the typed API after the selected backend's
readiness check: gRPC health for native, or the WebSocket protocol for Codex. Host asks Runtime
for in-process readiness. Every
call carries the destination, incarnation and attributing Session; a foreign destination or stale
incarnation is rejected before native dispatch. The service cannot adopt another environment or
advance a Runner watch epoch. Callers supply Session and incarnation context with each request;
Environment Runtime owns service health and lifecycle fencing. Gateway
retains remote authorization and routing. When the expected incarnation is stale, Gateway or
Environment Runtime may attach the typed `SandboxIncarnationChanged` status detail only after
verifying the same destination Sandbox UID and observing a nonempty current incarnation. Invalid or
foreign destinations and unavailable incarnations carry no rebind detail.

For a fresh `OpenSession` through Environments, a caller may omit the expected incarnation.
Environments binds that open to its verified current Runtime connection, supplies the explicit
incarnation to Runtime, and returns `incarnation_uid` in the first response. The caller must
persist that fence before using or resuming the execution session. Runtime itself still receives
and validates an explicit incarnation; resumes and unary operations cannot omit it.

After caller authorization and exact Sandbox UID resolution, Gateway may attach
`ExecutionEnvironmentFailure` when it refuses a request before forwarding it to Runner. A scoped
Runtime may attach a local readiness state only when it refuses before native dispatch. The state
distinguishes suspended, starting, not-ready, quiescing and unavailable control routing; the
outcome is `NOT_DISPATCHED`. The detail contains no replacement identity and grants no wake, resume
or provisioning authority. Invalid, foreign or stale Sandbox identities, an unsynchronized
inventory and uncertain Kubernetes reads receive no Gateway environment detail. Legacy Runner
proxy calls keep their existing status behavior and do not receive this execution detail.

Runtime holds the COSI database lock before constructing the embedded Execution service. Its
COSI Execution controller serializes service attempts and requires the previous service's joined
fence before launching a successor. Pod ownership is PID 1 plus its private process namespace.
The Execution Service holds a gRPC Claim stream over the companion's filesystem-protected Unix
socket; cancellation or transport loss requests a fence. Inspect and generation-scoped Fence are
unary RPCs. Stream closure or a Fence reply alone does not prove cleanup.
On ordinary release, the companion stops the backend,
fences every remaining process in that namespace, and publishes a new idle generation only after
the process table contains no workload process. Fence requests name the observed generation;
delayed duplicates cannot fence a successor. A changed generation proves the prior owner was
fenced even if a successor has already claimed it. Startup still requires an idle generation.
The companion keeps PID 1 alive for the next owner. If
the backend exits unexpectedly, the companion performs the same process-domain fence and closes the
ownership lease so Runtime can recover the backend. If the in-container fence fails, the companion
exits and the outer runtime's whole-namespace cleanup supplies the stronger fence. VM ownership is the Runtime systemd unit, its lifetime marker and the explicit `execution-service`
scope in `agentplane-executors.slice`, with startup fencing and stop proof from the shared supervisor.
The backend runs as a non-PID-1 child and exclusively owns waits for processes it starts; the
companion reaps orphans without stealing live direct-child waits. A lost child wait (including
`ECHILD`) is fatal to the backend, so the supervisor fences the process domain before replacement.
No second owner may start native work while the prior owner remains live. A
failed fence is not permission to launch a successor. Runtime retains ownership when cleanup is
uncertain; its process lifetime and external backend fences survive independently of COSI's
in-memory lifecycle state.

## Sessions and effects

`OpenSession` holds a typed backend connection. The typed API and Codex WebSocket share the
same in-memory session registry. An execution session is separate from a persisted sampler
conversation. Eight concurrent typed open or opening connections are admitted; active connections
remain production autosuspend blockers. Coalesced `process_changed` notifications wake
clients; cursor reads remain authoritative for output and exit state.

Closing the stream releases its connection-scoped file handles and workers; it does not terminate
processes. A detached session is retained for one hour, after which its processes are terminated.
Resuming a retained session preserves process IDs and output sequence cursors. Restarting the
backend loses its session registry, processes and handles; no reconstruction replays prior commands.
If file-handle cleanup cannot be confirmed, a replacement connection is refused.

A canceled unary RPC ends that caller's wait. An admitted operation still belongs to the held
session and may have taken effect. Explicit process cancellation uses terminate or signal. Typed
responses do not synthesize Codex native wire frames. Process `cwd` and file `path` fields are
absolute `file:` URIs; `argv` entries remain ordinary process arguments.

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

The Execution API closes admission and held connections, joins direct adapter calls, and stops its native
backend before releasing ownership. Pod cleanup fences all remaining processes in the companion's
private namespace, including descendants that escaped a process group; if that proof fails, Pod
exit invokes the runtime's whole-namespace fence. VM cleanup proves its execution cgroup empty;
Runtime death also removes its lifetime marker and stops bound transient units. A backend exit ends
the API lifetime; the controller may create a replacement only after the old lifetime returns fenced proof.

Authored workspace files survive service shutdown. This contract promises no persistence for the
native backend's in-memory session registry; its loss must not be reported as successful execution
or permission to replay an uncertain operation.
