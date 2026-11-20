# Environment execution service

Environment Runtime embeds the typed [`Executor` API](executor.proto), independently of a native
harness, app-server, model provider or integration app. It forwards to the shared native execution
backend, which also exposes the Codex WebSocket API. Generated Go and Python clients live with this protocol.
[SPEC.md](SPEC.md) defines lifetime, admission and recovery guarantees.

[Isolation and resources](ISOLATION.md) describes protection goals, the current shared Codex
backend, process placement, collective resource limits and failure scope.

Environment Runtime runs the admission proxy in-process and forwards typed RPCs to the backend. Host asks Runtime
for in-process readiness before starting its shared Codex app-server; Runtime owns startup, health,
shutdown and fencing. Readiness checks both the typed service and Codex protocol endpoint. In Pods,
the `companion` subcommand runs as PID 1 in the isolated executor container and owns its process namespace. Gateway and the central sampler continue to use the unchanged `Executor` wire
protocol. `//agentplane/env/e2e:test_executor_container` exercises process output, session resume,
filesystem access, incarnation fencing and shutdown through Runtime with the real native executor.

## Pod companion invocation

`//agentplane/env/runtime/execution/cmd:agentplane-execution-service` supplies the Pod companion
entrypoint. The environment owner starts it as PID 1 in the execution container:

```sh
agentplane-execution-service companion --socket /run/agentplane/executor/control.sock \
  --listen ws://127.0.0.1:4501 --binary /run/agentplane/environment/bin/agentplane-execution-service --health-listen 0.0.0.0:4502
```

The service fences a prior owned companion before claiming an idle generation. Canceling its gRPC
ownership stream stops the executor, kills every remaining process in the private companion PID namespace and
waits for the namespace to contain only PID 1 before publishing a fresh idle generation. The
companion stays available for the next claim. An unexpected executor exit triggers the same
process-domain fence and closes the ownership lease so Runtime can recover the backend. If the
in-container proof fails, the companion exits so the outer runtime can fence the whole namespace.
Runtime starts the API, waits for native protocol readiness, and joins its shutdown
and 90-second backend fence before replacement. A failed cleanup is never an execution success receipt.

The companion and VM supervisor launch `agentplane-execution-service serve`; no harness configuration or provider credentials are required.

The `serve` and `companion` commands accept an optional `--limits-file` pointing to an absolute
path to a JSON file. A companion passes it to the supervised backend; on VMs set
`environment.executor.limits_file` in the Runtime configuration. Mount the file into the execution
container when setting the companion option. For example:

```json
{
  "native": { "max_sessions": 128, "max_output_bytes": 67108864 },
  "codex": { "max_message_bytes": 16777216, "max_concurrent_requests": 64 }
}
```

Unspecified fields use `native.DefaultLimits` and `codexshim.DefaultLimits`; unknown fields and
nonpositive values are rejected. Values are counts or bytes, except the native retention settings,
which are seconds. A new configuration takes effect when the backend is restarted; restarting
abandons its live process handles and never replays uncertain work.

Runtime publishes the proxy after bounded typed and Codex protocol readiness probes. The service
probes native dispatch every five seconds with a three-second deadline; three consecutive failures
end the service lifetime and fence the backend. Shutdown closes held sessions, joins direct calls and stops
the backend. Backend exit ends this service lifetime; Environment Runtime proves the old service
fenced before attempting one replacement while the incarnation remains RUNNING. A failed fence or
service start fails Runtime rather than starting an unfenced successor. Neither startup nor shutdown
replays execution requests.

## Source ownership

- `native/`: process, session and file ownership.
- `typedbackend/` and `typedproxy/`: internal typed operations and Runtime admission.
- `codexshim/`: Codex WebSocket compatibility surface.
- `codexexecserver/`: native launch/probe/fence backends.
- `companion/`: Pod PID-1 ownership protocol and its client, shared reaper integration.
- `service/`: environment identity and embedded adapter lifetime under Runtime ownership.
- `cmd/`: companion executable entrypoint; `testing/`: shared companion container fixtures.

**Deviation:** the proto keeps the published `agentplane.executor` wire package while source and
client import paths live under `env/runtime/execution`. Relocation changes neither RPC method names nor
message fields, so current Gateway, Runtime and sampler versions still agree during deployment.

## Restarting an executor

Native sessions and processes are in-memory state. Before restarting an executor, inventory the retained
sampler execution bindings and reconcile unfinished operations. Preserve authored files, Session journals and
volumes, and read [staging data preservation](../../../docs/staging_data_preservation.md) before a deployment
rollback.

## Pod companion container

Environment Pods can run Codex command execution through a companion container in the same Pod. The runner
retains the Codex app-server and journals; `agentplane-execution-service companion` runs as PID 1 and supervises
the native execution service in its private container PID namespace. The Execution Service claims the companion over gRPC on a Unix socket before the backend
starts and fences it before replacing a lost process. A companion exit or failed health probe lets
Kubernetes restart that container with a fresh PID namespace; the environment services never replay an in-flight
command. `Environment.isolated_codex_executor` controls this Pod-template policy and can be disabled
for environments that need execution inside the Runtime container.

Both containers run as the image's uid and gid 1000 and share the Sandbox state and workspace at the
same absolute paths, plus `/home/runner`, `/tmp`, and the read-only runner disk image. They use the
same proxy and CA settings. Processes in either container have the same filesystem identity and can
change the shared files, so the companion is a process and resource boundary, not a trust boundary.
Its Kubernetes requests and limits are independently configurable with `executor_resources` on
`EnvironmentSandboxTemplate`; Environment Runtime resource overrides continue to apply only to that container. The
companion receives any optional BuildBuddy diagnostics key. It gets no model credentials or
sidecar-projected tokens.
