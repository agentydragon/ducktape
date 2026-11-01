# Codex executor isolation

Environment Runtime owns the standalone Execution Service and its executor lifetime. Harness Host
runs the shared Codex app-server and sets `CODEX_EXEC_SERVER_URL` to connect to that service. An
executor failure never enables local execution. Claude continues to execute in its own harness
process.

The executor is a resource boundary within the same Sandbox principal. It shares workspace, home,
temporary files and native state paths with the app-server. It is not an authorization boundary
between Sessions or a safe place for another principal's credentials.

## Pods

The template starts `/run/agentplane/runner/bin/agentplane-execution-service companion` in a separate
container on the same tools image. This process mode runs the companion as PID 1 without starting
Environment Runtime, Harness Host, COSI or telemetry. Kubernetes `resources` supplies its memory and
CPU limits; kubelet restarts the container. Its PID namespace must be private
(`shareProcessNamespace: false`). Both containers mount the same state, workspace, home, temporary
directory, environment binary disk,
proxy trust bundle and kubeconfig. Projected workload tokens remain confined to the egress sidecar.

The standalone Execution Service claims the companion over its ownership socket. A claim's explicit
environment reaches `exec-server` through `supervision.Process`; PID 1's process-wide reaper owns
native and orphan waits. The claim connection and companion lifetime remain the executor's
ownership protocol. The companion does not construct Runtime's COSI lifecycle or Host's journals.
Stdout/stderr readers are not joined before PID 1 exits, since escaped descendants can retain them.

The Sandbox template command and environment disk image pin are separate manifest inputs. A template
using `companion` mode requires a disk image that includes that command; roll them out together. To
roll back, restore both the previous template command and the previous environment disk image pin.
After Flux applies the rollback, verify a fresh Pod's executor health endpoint and Execution Service
ownership claim.

Execution Service holds a Unix connection to the companion through an `emptyDir`. On ownership loss
or executor exit, the companion normally fences its workload process domain and publishes a fresh
idle generation while keeping PID 1 alive. If that in-container fence fails, the companion exits so
the container runtime can fence the whole namespace; only a new container generation then proves
that outer cleanup completed. A socket close alone does not prove either fence.

A claim whose `exec-server` cannot be spawned at all (a missing or unexecutable Codex binary) starts
nothing, so there is no workload generation to fence. The companion refuses the claim with the reason,
stays PID 1 and serving, and answers a later claim the same way. Runtime reports a failed service
startup instead of retrying through a companion restart loop.
The companion also reaps orphaned children while running. It serves HTTP `GET /healthz` at the
template's `--health-listen` address (`:4502` in Pods). Kubernetes liveness and readiness probes
call that endpoint without launching a probe process. Each request checks the existing Unix control
socket with a one-second deadline, including while waiting for Execution Service to claim it; an unavailable
control socket returns HTTP 503. The endpoint exposes no ownership or execution commands. Pod OOM evidence remains in
`status.containerStatuses[].lastState.terminated` and `restartCount`; an ownership disconnect alone
does not establish that the container was OOM-killed.

The common environment configuration selects this mode with `environment.kind: "pod"` and
`environment.executor.url` and `environment.executor.socket`. The endpoint
is loopback-only. The health listener accepts kubelet HTTP probes on the Pod address; it has no
Service or inbound network grant. Runtime needs no Kubernetes credentials or cgroup privileges.
The template owns the endpoint and socket paths used on both sides.

## VMs

The template's `vm_resources` value supplies `environment.resources` under `environment.kind: "vm"`.
Its optional executor budget and `environment.executor` endpoint must be configured together.
Execution Service applies that budget to `agentplane-executors.slice`; Harness Host uses the sibling
`agentplane-workloads.slice`. Runtime and both service units live under the capped `agentplane.slice`.
Runtime fences previous owned services before admitting an incarnation.
The harness aggregate and executor aggregate have independent limits; their configured memory caps
must fit together below the service's memory high watermark. The service's maximum leaves the
configured OS reserve below guest RAM. Aggregate groups and native leaves disable swap and set
`memory.oom.group=0`, allowing the kernel to kill individual processes under memory pressure. A tool
can be killed while the exec-server survives, but the kernel can also select the server itself.
Native leader exit still fences the entire leaf; OOM policy does not weaken that cleanup.

Execution Service starts `exec-server` as a systemd transient service with `Restart=no`, bound to the
Execution Service unit. Runtime fences prior Host and Execution invocations before admitting an
incarnation. During orderly shutdown Runtime drains Host, proves Execution fenced, and then stops
both units; the units do not independently stop ahead of that lifecycle protocol. The guest supplies
systemd, agent UID/GID and block devices; templates supply resource limits.
See [native supervision](supervision.md) for the backend and ownership boundary.

## Startup, loss and suspend

1. Runtime acquires environment ownership and fences previous native work. In Pods it waits for a
   fresh idle companion generation before admitting an incarnation. In VMs it fences old owned
   systemd services in the expected slices.
2. On first Codex startup Runtime starts Execution Service and waits for a bounded health probe. The
   service's native probe initializes a WebSocket session and reads `environment/status`; Harness
   Host then starts the app-server with the configured executor URL.
3. Execution Service probes native executor progress every five seconds with a three-second deadline.
   Three consecutive failures end its lifetime and fence the backend. Runtime proves the old service
   invocation fenced, then attempts one replacement while the same incarnation remains RUNNING. A
   failed fence or replacement fails Runtime; there is no internal restart loop or backoff. Commands
   are never replayed.
4. Quiesce drains Host Sessions and native work, fences Execution, and only then seals journals.
   Ordinary Runtime shutdown uses the same order before releasing environment ownership. Failure to
   prove executor cleanup prevents sealing and starting a successor.

No Sandbox Controller lifecycle API changes are required: its existing quiesce/seal fence covers
executor cleanup. A replacement Environment Runtime performs the startup fence after an ungraceful
exit. Its bounded wait may fail while kubelet is replacing the companion; that is a failed fence,
not permission to proceed. Runtime can retry only after a new idle companion generation proves the
container runtime cleaned up the prior namespace.

## Scope in Codex 0.162.1

The remote environment covers the shell/filesystem operations Codex routes to its exec-server.
Codex can reconnect after a transient WebSocket loss while the server retains its session registry.
A restarted server loses that registry and running commands; an interrupted tool can return an
error with uncertain side effects. Later tool calls can use the new server. Execution Service never
replays a command, and Runtime does not treat executor loss as an app-server exit.

Code-mode workers and MCP helpers remain children of the app-server in this release and consume
the harness budget. Work requested from a separate daemon, such as a system Nix daemon, consumes
that daemon's cgroup rather than the caller's. An RPC probe detects an unresponsive dispatcher;
it cannot establish progress of every individual command. Host OOM, node eviction and a breached
outer service/container limit can still stop the environment.

Central hosting of the Codex app-server with Sandbox executors remains future work. Local tools
and Sandbox-hosted app-server with isolated tools remain explicit transitional operating modes.
