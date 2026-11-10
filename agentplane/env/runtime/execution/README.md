# Environment execution service

Environment Runtime embeds the typed [`Executor` API](executor.proto), independently of a native
harness, app-server, model provider or integration app. It owns one Codex exec-server and calls the
execution adapter directly. Generated Go and Python clients live with this protocol.
[SPEC.md](SPEC.md) defines lifetime, admission and recovery guarantees.

[Isolation and resources](ISOLATION.md) describes protection goals, the current shared Codex
backend, process placement, collective resource limits and failure scope.

Environment Runtime runs this service in-process and calls its adapter directly. Host asks Runtime
for in-process readiness before starting its shared Codex app-server; Runtime owns startup, health,
shutdown and fencing. Native protocol probing establishes readiness without a local listener. In Pods,
the `companion` subcommand runs as PID 1 in the isolated
Codex container and owns the native
exec-server namespace. Gateway and the central sampler continue to use the unchanged `Executor` wire
protocol. `//agentplane/env/e2e:test_executor_container` exercises process output, session resume,
filesystem access, incarnation fencing and shutdown through Runtime with the real native executor.

## Pod companion invocation

`//agentplane/env/runtime/execution/cmd:agentplane-execution-service` supplies the Pod companion
entrypoint. The environment owner starts it as PID 1 in the execution container:

```sh
agentplane-execution-service companion --socket /run/agentplane/executor/control.sock \
  --listen ws://127.0.0.1:4501 --binary /bin/codex --health-listen 0.0.0.0:4502
```

The service fences a prior owned companion before claiming an idle generation. Canceling its gRPC
ownership stream stops the executor, kills every remaining process in the private companion PID namespace and
waits for the namespace to contain only PID 1 before publishing a fresh idle generation. The
companion stays available for the next claim. An unexpected executor exit triggers the same
process-domain fence and closes the ownership lease so Runtime can recover the backend. If the
in-container proof fails, the companion exits so the outer runtime can fence the whole namespace.
Runtime starts the API, waits for native protocol readiness, and joins its shutdown
and 90-second backend fence before replacement. A failed cleanup is never an execution success receipt.

All backends launch only `codex exec-server`; no harness configuration or provider credentials are required.

Runtime publishes the adapter after a bounded native protocol readiness probe. The service
probes native dispatch every five seconds with a three-second deadline; three consecutive failures
end the service lifetime and fence the backend. Shutdown closes held sessions, joins direct calls and stops
the backend. Backend exit ends this service lifetime; Environment Runtime proves the old service
fenced before attempting one replacement while the incarnation remains RUNNING. A failed fence or
service start fails Runtime rather than starting an unfenced successor. Neither startup nor shutdown
replays execution requests.

## Source ownership

- `adapter/`: typed process/file operations, native session and WebSocket lifetime, bounded frames and
  cancellation.
- `codexexecserver/`: native launch/probe/fence backends.
- `companion/`: Pod PID-1 ownership protocol and its client, shared reaper integration.
- `service/`: environment identity and embedded adapter lifetime under Runtime ownership.
- `cmd/`: companion executable entrypoint; `testing/`: shared companion container fixtures.

**Deviation:** the proto keeps the published `agentplane.executor` wire package while source and
client import paths live under `env/runtime/execution`. Relocation changes neither RPC method names nor
message fields, so current Gateway, Runtime and sampler versions still agree during deployment.

## Cutover and rollback

The cutover combines the management processes while keeping the public Gateway control listener and
`Executor` RPC schema unchanged. Old Sandboxes must be stopped and deleted before the cutover;
preserve authored files and Session history before deleting their storage. Rollback requires stopping
the new Sandboxes before restoring the previous guest and runtime image pins, then recreating them.

Runtime fences the previous Execution Service owner before starting a replacement. Its scope is
distinct from native harness process scope. Inventory retained sampler execution bindings and
reconcile unfinished operations before restarting an executor: native sessions/processes are
in-memory state. Preserve authored files and existing Session journals and volumes. See
[staging data preservation](../../../docs/staging_data_preservation.md) before a deployment rollback.

## Pod companion container

Environment Pods can run Codex command execution through a companion container in the same Pod. The runner
retains the Codex app-server and journals; `agentplane-execution-service companion` runs as PID 1 and supervises
`codex exec-server` in its private container PID namespace. The Execution Service claims the companion over gRPC on a Unix socket before Codex
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

`../codex/protocol` generates Go models at build time with Quicktype from schemas
exported by the same pinned Codex executable used by the native tests. Requests,
responses, notifications and known item payloads use these models. Method and item
dispatch keep a raw JSON boundary for unknown variants; recorded native frames are
never reconstructed from decoded models. Codex reconciliation uses turn-scoped
`thread/items/list` RPCs on that connection, with the same generated protocol types; it does not
read Codex's private files or databases. Schema export and JavaScript code generation are
build dependencies only.

Claude stream events and assistant content blocks use the Anthropic Go SDK's
discriminated unions. Consumed variants are checked with strict JSON decoding
because the SDK's response decoder tolerates malformed fields. Tool inputs use
the SDK's raw-field metadata to preserve whitespace, numeric tokens and explicit
`null`. Claude Code control packets use the standard-library-only
`claude-agent-sdk-go/types` envelopes and matching subtype structs. Local
extensions cover `appendSystemPrompt`, interrupt metadata, model/effort changes,
raw tool inputs and explicit null reply fields. User tool results have their own
wire type.

The integration fixtures launch Runtime with its embedded Host and Execution components and drive its public control and ingress
protocols. `test_turns.py`, `test_subagents.py`, and `test_restart.py` exercise the real
native harnesses; App, History, Gateway, and Notification integration tests use the
same process topology.
