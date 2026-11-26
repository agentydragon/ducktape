# Codex executor isolation

Environment Runtime owns the executor and passes its WebSocket endpoint to the shared Codex
app-server through `CODEX_EXEC_SERVER_URL`. See [backend selection](../README.md#backend-configuration)
for the native default and upstream Codex option, and [execution isolation](../ISOLATION.md) for
the harness and command budgets.

## Pods

The Pod template enables its separate executor container by default. Setting
`Environment.isolated_codex_executor` to false omits both the container and the Runtime executor
configuration. In that mode Codex starts local tools in the Runtime container.

The companion runs `//agentplane/env/runtime/execution/cmd:agentplane-execution-service` as PID 1.
It runs only the companion control and supervision, separate from Runtime, COSI, Host journals and telemetry. The template owns matching
socket and endpoint paths on both sides. The companion uses direct command placement and requires
a private PID namespace (`shareProcessNamespace: false`).

```sh
agentplane-execution-service companion --socket /run/agentplane/executor/control.sock \
  --listen ws://127.0.0.1:4501 --binary /run/agentplane/environment/bin/agentplane-execution-service --health-listen 0.0.0.0:4502
```

`EnvironmentSandboxTemplate.executor_resources` overrides only the companion's Kubernetes
requests and limits. Runtime's overrides apply to its own container. Both run as UID and GID 1000
and share writable Sandbox state, workspace, home and temporary paths. They also mount the same
read-only runner disk and use the same proxy and trust settings. The companion shares the optional BuildBuddy key.
Model credentials are withheld from the executor, and projected workload tokens stay with the
egress sidecar. Both containers act as the same Sandbox principal, and files writable by the
Sandbox UID remain writable in either container. The companion separates their process and resource domains.

The embedded Execution Service claims the companion over gRPC on its ownership Unix socket. The claim
selects which backend the companion starts and supplies its environment and any configured native limits.
PID 1 reaps the executor and its orphans. An escaped descendant can keep stdout or stderr open,
so PID 1 exits without joining those readers.

The Sandbox template command and environment disk image pin are separate manifest inputs. A template
using `companion` mode requires a disk image that includes that command; roll them out together. To
roll back, restore both the previous template command and the previous environment disk image pin.
After Flux applies the rollback, verify a fresh Pod's executor health endpoint and Execution Service
ownership claim.

Execution Service holds a gRPC Claim stream to the companion through an `emptyDir` Unix socket
with mode `0600`. The local service trusts filesystem access by the Sandbox UID and has no TCP
listener. On ownership loss or executor exit, the companion normally fences its workload process domain and publishes a fresh
idle generation while keeping PID 1 alive. If that in-container fence fails, the companion exits so
the container runtime can fence the whole namespace; only a new container generation then proves
that outer cleanup completed. A stream close alone does not prove either fence.

A claim whose backend cannot be spawned at all (a missing or unexecutable backend binary) starts
nothing, so there is no workload generation to fence. The companion refuses the claim with the reason,
stays PID 1 and serving, and answers a later claim the same way. Runtime reports a failed service
startup instead of retrying through a companion restart loop.

The companion serves HTTP `GET /healthz` at the template's `--health-listen` address (`:4502` in Pods). Kubernetes liveness and readiness probes
call that endpoint without launching a probe process. Each request checks the existing Unix control
socket with a one-second deadline, including while waiting for Execution Service to claim it; an unavailable
control socket returns HTTP 503. The endpoint exposes no ownership or execution commands. Pod OOM evidence remains in
`status.containerStatuses[].lastState.terminated` and `restartCount`; an ownership disconnect alone
does not establish that the container was OOM-killed.

The executor endpoint is loopback-only. The kubelet health listener has no Service or inbound
network grant. Runtime needs no Kubernetes credentials or cgroup privileges.

## VMs

The template's `vm_resources` value supplies `environment.resources` under `environment.supervision: "systemd"`.
An executor with `owner: {"kind": "systemd"}` requires `resources.executor`. Execution Service
applies that budget to `agentplane-executors.slice`, and Runtime uses the sibling
`agentplane-workloads.slice`. Runtime and the native slices live under the capped `agentplane.slice`.
Runtime fences previous owned services before admitting an incarnation.
The harness aggregate, executor aggregate and optional command aggregate have independent limits.
Their configured memory caps must fit together below the service's memory high watermark. The service's maximum leaves the
configured OS reserve below guest RAM. Aggregate groups and native leaves disable swap and set
`memory.oom.group=0`, allowing the kernel to kill individual processes under memory pressure.
With `placement: "direct"`, the executor and its commands share the executor service cgroup.
With `placement: "systemd"`, each native command runs in a transient unit under
`agentplane-commands.slice`, using the distinct `resources.executor_workload` budget.
Native leader exit still fences the entire leaf.

Runtime starts `agentplane-execution-service serve` or `codex exec-server` as a systemd transient
service with `Restart=no`, bound to the Runtime service unit, with a 90-second process fence deadline.
The guest supplies systemd,
agent UID/GID and block devices. Templates supply resource limits.
See [native supervision](../../supervision/docs/supervision.md) for the backend and ownership boundary.

## Startup, loss and suspend

1. Runtime acquires environment ownership and fences previous native work. In Pods it waits for a
   fresh idle companion generation before admitting an incarnation. In VMs it fences old owned
   systemd services in the expected slices.
2. When adopting an incarnation with Codex execution configured, Runtime starts the embedded
   Execution Service and waits for the backend to be ready. The native backend uses a typed gRPC
   health check without opening an execution session. The Codex backend initializes a WebSocket
   session and reads `environment/status`.
   Runtime starts the app-server when requested, with the configured executor URL.
3. Execution Service repeats the applicable checks every five seconds with a three-second deadline.
   Three consecutive failures end its lifetime and fence the backend. Runtime proves the old service
   invocation fenced, then attempts one replacement while the same incarnation remains RUNNING. A
   failed fence or replacement fails Runtime. Runtime never replays commands.
4. Quiesce drains Host Sessions and native work, fences Execution, and only then seals journals.
   Ordinary Runtime shutdown uses the same order before releasing environment ownership. Failure to
   prove executor cleanup prevents sealing and starting a successor.

No Sandbox Controller lifecycle API changes are required: its existing quiesce/seal fence covers
executor cleanup. A replacement Environment Runtime performs the startup fence after an ungraceful
exit. Its bounded wait may fail while kubelet is replacing the companion; that is a failed fence,
not permission to proceed. Runtime can retry only after a new idle companion generation proves the
container runtime cleaned up the prior namespace.

## Scope in Codex 0.162.1

The native backend’s compatibility endpoint implements the Codex operations used by the harness; unsupported
sandbox policy requests are rejected. Codex can reconnect after a transient WebSocket loss while
the server retains its session registry.
A restarted server loses that registry and running commands. An interrupted tool can report
uncertain side effects, and later calls can use the replacement without an app-server restart.

Code-mode workers and MCP helpers remain children of the app-server in this release and consume
the harness budget. Work requested from a separate daemon, such as a system Nix daemon, consumes
that daemon's cgroup rather than the caller's. An RPC probe detects an unresponsive dispatcher;
it cannot establish progress of every individual command. Host OOM, node eviction and a breached
outer service/container limit can still stop the environment.

Central hosting of the Codex app-server with Sandbox executors remains future work. Local tools
and Sandbox-hosted app-server with isolated tools remain explicit transitional operating modes.

## Codex Session integration

### Isolated Codex execution

- Runtime may launch and initialize the shared app-server while executor startup is still in
  progress. It admits no Codex Session to that app-server until the executor is ready; a refusal
  or readiness deadline drains the app-server before the waiting Session receives the failure.
- Central sampler execution sessions wait for controller-owned readiness and retries within the
  Gateway's readiness deadline. Closing the session stream cancels its pending readiness wait or connection
  attempt without blocking the Environment Runtime control stream. A refused launch or terminal fencing failure
  rejects readiness; it never enables local execution.
