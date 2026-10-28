# Codex executor isolation

Runner supports local Codex tools and an isolated WebSocket `codex exec-server`. All Codex Sessions
on a Runner share one app-server and, in isolated mode, one executor. Runner sets
`CODEX_EXEC_SERVER_URL` before starting the app-server; an executor failure never enables local
execution. Claude continues to execute in its own harness process.

The executor is a resource boundary within the same Sandbox principal. It shares workspace, home,
temporary files and native state paths with the app-server. It is not an authorization boundary
between Sessions or a safe place for another principal's credentials.

## Pods

The template starts `agentplane-executor` from the Runner disk in a separate container on the same
tools image. That standalone binary does not include the Runner runtime or SQLite storage.
Kubernetes `resources` supplies its memory and CPU limits; kubelet restarts the container. Its PID
namespace must be private (`shareProcessNamespace: false`), and the companion must be PID 1.
Both containers mount the same state, workspace, home, temporary directory, runner binary disk,
proxy trust bundle and kubeconfig. Projected workload tokens remain confined to the egress sidecar.

Runner holds a Unix connection to the companion through an `emptyDir`. Claiming it starts
`exec-server`; EOF, a fence request, termination, or executor exit ends that whole container
generation. Exiting PID 1 lets the runtime kill detached descendants as well as the process group.
An idle successor's fresh generation ID proves cleanup completed. A socket close alone does not.

A claim whose `exec-server` cannot be spawned at all (a missing or unexecutable Codex binary) starts
nothing, so it ends no generation and fences nothing: the companion refuses the claim with the reason,
stays PID 1 and serving, and answers the next claim the same way. Exiting instead would restart the
container into the identical failure, and a companion in restart backoff leaves no control socket for
Runner to fence — a Sandbox could then neither run Codex nor prove it may stop. Runner records the
reason, surfaces it to every Session waiting on isolated execution instead of its own startup deadline,
and keeps retrying at its backoff until a new container generation answers.
The companion also reaps orphaned children while running. It serves HTTP `GET /healthz` at the
template's `--health-listen` address (`:4502` in Pods). Kubernetes liveness and readiness probes
call that endpoint without launching a probe process. Each request checks the existing Unix control
socket with a one-second deadline, including while waiting for Runner to claim it; an unavailable
control socket returns HTTP 503. The endpoint exposes no ownership or execution commands. Pod OOM evidence remains in
`status.containerStatuses[].lastState.terminated` and `restartCount`; an ownership disconnect alone
does not establish that the container was OOM-killed.

The common Runner configuration selects this mode with `environment.kind: "pod"` with
`environment.executor.url` and `environment.executor.socket`. The endpoint
is loopback-only. The health listener accepts kubelet HTTP probes on the Pod address; it has no
Service or inbound network grant. Runner needs no Kubernetes credentials or cgroup privileges.
The template owns the endpoint and socket paths used on both sides.

## VMs

The template's `vm_resources` value supplies `environment.resources` under `environment.kind: "vm"`.
Its optional executor budget and `environment.executor` endpoint must be configured together.
Runner applies that budget to `agentplane-executors.slice`; harnesses use the sibling
`agentplane-workloads.slice`. Both live with Runner under the capped `agentplane.slice`.
It fences previous owned services before opening Session stores.
The harness aggregate and executor aggregate have independent limits; their configured memory caps
must fit together below the service's memory high watermark. The service's maximum leaves the
configured OS reserve below guest RAM. Aggregate groups and native leaves disable swap and set
`memory.oom.group=0`, allowing the kernel to kill individual processes under memory pressure. A tool
can be killed while the exec-server survives, but the kernel can also select the server itself.
Native leader exit still fences the entire leaf; OOM policy does not weaken that cleanup.

Runner starts `exec-server` as a systemd transient service with `Restart=no`, just like its other
native launches. The service binds to the Runner unit; Runner death stops its whole cgroup, with
five seconds of graceful termination before forced cleanup. Runner also fences previous owned
units at startup before opening journals, and proves the execution domain empty before allowing
a successor. COSI restarts the executor independently of the app-server. The guest supplies systemd,
agent UID/GID and block devices; templates supply resource policy.
See [native supervision](supervision.md) for the backend and ownership boundary.

## Startup, loss and suspend

1. Runner acquires state ownership and fences previous native work. In Pods it waits for an idle
   companion generation before opening Session stores. In VMs it fences old owned systemd services in the expected slices.
2. The first Codex startup starts the executor restart loop and waits for a bounded RPC probe.
   The probe initializes a WebSocket session and reads `environment/status`. The app-server then
   starts with the configured executor URL.
3. Runner probes RPC progress every five seconds with a three-second deadline. Three consecutive
   failed probes fence that generation; a successful probe resets the failure count. Server exit
   fences without waiting for further probes. Restart uses exponential backoff capped at ten seconds.
   Pod cleanup also waits for kubelet to restart the companion. Commands are never replayed by Runner.
4. Quiesce halts Sessions, stops harnesses, fences the executor and only then seals journals.
   Ordinary Runner shutdown follows the same cleanup before releasing state ownership. Failure to
   prove executor cleanup prevents sealing and starting a successor executor.

No Sandbox Controller lifecycle API changes are required: its existing quiesce/seal fence covers
executor cleanup. A replacement Runner performs the startup fence even after an ungraceful exit.
The bounded cleanup wait can fail during container restart backoff; that is a failed fence, not
permission to proceed. A later Runner startup can retry once kubelet has produced an idle companion.

## Scope in Codex 0.162.1

The remote environment covers the shell/filesystem operations Codex routes to its exec-server.
Codex can reconnect after a transient WebSocket loss while the server retains its session registry.
A restarted server loses that registry and running commands; an interrupted tool can return an
error with uncertain side effects. Later tool calls can use the new server. Runner neither retries
the command nor treats an executor exit as an app-server exit.

Code-mode workers and MCP helpers remain children of the app-server in this release and consume
the harness budget. Work requested from a separate daemon, such as a system Nix daemon, consumes
that daemon's cgroup rather than the caller's. An RPC probe detects an unresponsive dispatcher;
it cannot establish progress of every individual command. Host OOM, node eviction and a breached
outer service/container limit can still stop Runner itself.

Central hosting of the Codex app-server with Sandbox executors remains future work. Local tools
and Sandbox-hosted app-server with isolated tools remain explicit transitional operating modes.
