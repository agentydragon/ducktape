# Codex executor isolation

Environment Runtime embeds the Execution Service and owns its executor lifetime. Harness Host
runs the shared Codex app-server and sets `CODEX_EXEC_SERVER_URL` to connect directly to the native
exec-server's WebSocket endpoint. A standalone Execution Service is also available. An
executor failure never enables local execution. Claude continues to execute in its own harness
process.

The executor is a resource boundary within the same Sandbox principal. It shares workspace, home,
temporary files and native state paths with the app-server. It is not an authorization boundary
between Sessions or a safe place for another principal's credentials.

## Pods

The template starts `/run/agentplane/environment/bin/agentplane-execution-service companion` in a separate
container on the same tools image. This process mode runs the companion as PID 1 without starting
Environment Runtime, Harness Host, COSI or telemetry. Kubernetes `resources` supplies its memory and
CPU limits; kubelet restarts the container. Its PID namespace must be private
(`shareProcessNamespace: false`). Both containers mount the same state, workspace, home, temporary
directory, environment binary disk,
proxy trust bundle and kubeconfig. Projected workload tokens remain confined to the egress sidecar.

The embedded Execution Service claims the companion over its ownership socket. A claim's explicit
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
`agentplane-workloads.slice`. Runtime and the native slices live under the capped `agentplane.slice`.
Runtime fences previous owned services before admitting an incarnation.
The harness aggregate and executor aggregate have independent limits; their configured memory caps
must fit together below the service's memory high watermark. The service's maximum leaves the
configured OS reserve below guest RAM. Aggregate groups and native leaves disable swap and set
`memory.oom.group=0`, allowing the kernel to kill individual processes under memory pressure. A tool
can be killed while the exec-server survives, but the kernel can also select the server itself.
Native leader exit still fences the entire leaf; OOM policy does not weaken that cleanup.

Execution Service starts `exec-server` as a systemd transient service with `Restart=no`, bound to the
Runtime service unit. Runtime fences prior native scopes before admitting an incarnation. During
orderly shutdown Runtime drains Host and proves Execution fenced before joining the components.
The guest supplies
systemd, agent UID/GID and block devices; templates supply resource limits.
See [native supervision](../../supervision/docs/supervision.md) for the backend and ownership boundary.

## Startup, loss and suspend

1. Runtime acquires environment ownership and fences previous native work. In Pods it waits for a
   fresh idle companion generation before admitting an incarnation. In VMs it fences old owned
   systemd services in the expected slices.
2. When adopting an incarnation with Codex execution configured, Runtime starts the embedded
   Execution Service and waits for a bounded native probe. The probe initializes a WebSocket session
   and reads `environment/status`; Harness Host starts the app-server when requested, with the
   configured executor URL.
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

## Codex Session integration

### Isolated Codex execution

- Deployment selects local tools or one isolated executor shared by every Codex Session. Runner
  configures the app-server's `CODEX_EXEC_SERVER_URL`; failed isolated execution never falls back to
  local tools. Claude execution is unchanged.
- In VMs the executor and native harnesses have separate sibling slice budgets under the shared
  `agentplane.slice`. In Pods the executor companion is PID 1 in a separate container with a private PID namespace
  and Kubernetes resource limits. It starts `agentplane-execution-service companion`. Workspace and runtime paths are shared
  under the same Sandbox principal.
- Runner may launch and initialize the shared app-server while executor startup is still in
  progress. It admits no Codex Session to that app-server until the executor is ready; a refusal
  or readiness deadline drains the app-server before the waiting Session receives the failure.
  Execution Service probes native executor progress and exits on terminal backend loss. Runtime owns
  its process lifetime and fence proof; Host cannot start or restart the executor. Failed tool side
  effects remain uncertain and are never replayed. Host asks Runtime for in-process Execution
  readiness to gate the shared app-server. Runtime verifies native protocol readiness before
  publishing the in-process adapter.
- Startup fences previous executor work before opening Session stores. Quiesce fences the executor
  before journal seals; shutdown fences it before releasing state ownership. VM startup fences prior
  owned systemd units before admitting work. Runtime coordinates component shutdown so Host native
  drain and the Execution fence complete before Host stops. Pod ownership
  loss normally fences the companion's workload process domain and publishes a fresh idle generation
  while PID 1 stays alive; if that fence fails, the companion exits and the container runtime must
  fence the whole namespace. Failed cleanup prevents sealing and starting a successor. If a Pod
  companion cannot spawn Codex, it refuses the claim with its reason and stays available to answer a
  later claim; no workload generation was started to fence.
- Code-mode workers, MCP helpers and external daemons are outside the executor budget in the pinned
  Codex release. Details and configuration: [Codex executor isolation](codex_executor.md).
- Central sampler execution sessions wait for controller-owned readiness and retries within the
  Gateway's readiness deadline. Closing the session stream cancels its pending readiness wait or connection
  attempt without blocking the Environment Runtime control stream. A refused launch or terminal fencing failure
  rejects readiness; it never enables local execution.
