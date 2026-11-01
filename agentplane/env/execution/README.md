# Environment execution service

`//agentplane/env/execution/cmd:agentplane-execution-service` serves the typed
[`Executor` API](executor.proto) independently of a native harness, app-server, Runner journal,
model provider or integration app. It owns one Codex exec-server and a private Unix gRPC socket.
The generated Go and Python clients live with this protocol. [SPEC.md](SPEC.md) defines lifetime,
admission and recovery guarantees.

[Isolation and resources](ISOLATION.md) describes protection goals, the current shared Codex
backend, process placement, collective resource limits and failure scope.

Environment Runtime starts this service as a child in Pods and as a systemd unit in VMs. Harness
Host calls its private Unix gRPC API; Runtime owns service startup, health, shutdown and fencing. In
Pods, the `companion` subcommand runs as PID 1 in the isolated Codex container and owns the native
exec-server namespace. Gateway and the central sampler continue to use the unchanged `Executor` wire
protocol. The independent service is exercised by
`//agentplane/env/execution/testing:test_service` using its actual executable as container PID 1,
the pinned native exec-server and generated gRPC client, without a Runner or harness process.

## Standalone invocation

Run `agentplane-execution-service --config /path/to/execution.json`. Configuration rejects unknown
keys. A Pod configuration is:

```json
{
  "namespace": "example-namespace",
  "sandbox_name": "example-sandbox",
  "sandbox_uid": "example-sandbox-uid",
  "incarnation_uid": "example-incarnation-uid",
  "state_dir": "/run/agentplane-execution",
  "working_directory": "/workspace",
  "binary": "/bin/codex",
  "executor_url": "ws://127.0.0.1:4501",
  "environment": { "HOME": "/home/runner", "PATH": "/bin:/usr/bin" },
  "backend": "pod"
}
```

`state_dir` is a private `0700` service directory. The listener is `state_dir/execution.sock`, mode
`0600`; clients with access to it have the environment's execution authority. It is not exposed over
TCP. Gateway remains the remote caller authorization and routing authority. The environment owner
supplies immutable Sandbox and incarnation identity and must stop this process before changing them.
Only the service may replace its socket: a nonblocking lifetime lock on
`state_dir/.agentplane-execution-owner` prevents another service process from taking over.
An environment owner may provide an absolute `socket_path` when the state directory is too deep for
the Unix-domain socket path limit; omitting it preserves `state_dir/execution.sock`.

`backend: "pod"` requires PID 1 in a private container PID namespace, with resource limits enforced
by the outer runtime. `backend: "systemd"` requires a `systemd` object containing `agent_uid`,
`agent_gid`, `block_devices`, `aggregate_limits` and `process_limits` (the shared
`supervision.ResourceLimits` shape). It runs under `agentplane-execution-service.service` with
`RuntimeDirectory=agentplane-execution-lifetime`. The systemd owner, runtime directory and
`execution-service` scope fences transient services independently from Harness Host's workload
scope.
`backend: "companion"` lets Runtime supervise the service as an ordinary child while a separate
container owns the native PID namespace. Set `container_socket` to the companion's absolute ownership
socket, omit `binary`, `working_directory` and `systemd`, and keep the same `executor_url` on both
sides. The environment owner starts the companion as PID 1 with:

```sh
agentplane-execution-service companion --socket /run/agentplane/executor/control.sock \
  --listen ws://127.0.0.1:4501 --binary /bin/codex --health-listen 0.0.0.0:4502
```

The service fences a prior owned companion before claiming an idle generation. Closing its ownership
lease stops the executor, kills every remaining process in the private companion PID namespace and
waits for the namespace to contain only PID 1 before publishing a fresh idle generation. The
companion stays available for the next claim. An unexpected executor exit triggers the same
process-domain fence and closes the ownership lease so Runtime can recover the backend. If the
in-container proof fails, the companion exits so the outer runtime can fence the whole namespace.
Runtime launches `--config`, waits for standard gRPC Health on the private socket, and stops the
service with SIGTERM, allowing its 90-second backend fence before checking process exit and outer
containment. A nonzero exit is failed cleanup, never an execution success receipt.

All backends launch only `codex exec-server`; no harness configuration or provider credentials are
required. The subprocess working directory is distinct from the private listener directory.

Standard gRPC health becomes serving after a bounded native protocol readiness probe. The service
probes native dispatch every five seconds with a three-second deadline; three consecutive failures
end the service lifetime and fence the backend. Shutdown closes held sessions, drains gRPC and stops
the backend. Backend exit ends this service lifetime; Environment Runtime proves the old service
fenced before attempting one replacement while the incarnation remains RUNNING. A failed fence or
service start fails Runtime rather than starting an unfenced successor. Neither startup nor shutdown
replays execution requests.

## Source ownership

- `adapter/`: typed process/file RPCs, native session and WebSocket lifetime, bounded frames and
  cancellation, used by the standalone service.
- `codexexecserver/`: native launch/probe/fence backends and the COSI lifecycle used by Execution Service.
- `companion/`: Pod PID-1 ownership protocol and its client, shared reaper integration.
- `service/`: standalone environment identity, state-directory ownership and serving lifetime.
- `cmd/`: executable entrypoint; `testing/`: real container service acceptance.

**Deviation:** the proto keeps the published `agentplane.executor` wire package while source and
client import paths live under `env/execution`. Relocation changes neither RPC method names nor
message fields, so current Gateway, Harness Host and sampler versions still agree during deployment.

## Cutover and rollback

The cutover changes process entrypoints and adds private Runtime-to-Host and Runtime-to-Execution
sockets. The public Gateway control listener and `Executor` RPC schema remain unchanged. Existing
Session journals, state locks, workspaces, output cursors and native session identities stay in place.
Reverting requires restoring the previous image pin and Pod command, then the old executable-bearing
image; no persisted-data migration is involved.

Runtime fences the previous Execution Service owner before starting a replacement. Its scope is
distinct from Harness Host's process scope. Inventory retained sampler execution bindings and
reconcile unfinished operations before restarting an executor: native sessions/processes are
in-memory state. Preserve authored files and existing Session journals and volumes. See
[staging data preservation](../../docs/staging_data_preservation.md) before a deployment rollback.
