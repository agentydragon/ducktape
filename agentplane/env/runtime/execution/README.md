# Environment execution service

Environment Runtime embeds the typed [`Executor` API](executor.proto) and owns backend readiness,
health and fencing. Native execution is independent of a harness, model provider or integration
app. The native backend serves typed gRPC and the Codex WebSocket protocol. Generated Go and Python
clients live with the typed protocol. [SPEC.md](SPEC.md) defines admission, lifetime and recovery.

## Backend configuration

`environment.executor.implementation` defaults to `native`, which launches
`agentplane-execution-service serve`. Selecting `codex` explicitly launches upstream `codex exec-server`
and adapts the typed API to its WebSocket protocol. Both owners support that compatibility option
with direct command placement. Omitting `environment.executor` selects local tools at configuration
time. An executor failure cannot switch a running environment to local tools.

Native execution limits are optional overrides in the Runtime configuration, for example:

```json
"executor": {
  "url": "ws://127.0.0.1:4501",
  "owner": { "kind": "companion", "socket": "/run/agentplane/executor/control.sock" },
  "implementation": "native",
  "placement": "direct",
  "limits": {
    "native": { "max_sessions": 128, "max_output_bytes": 67108864 },
    "codex": { "max_message_bytes": 16777216, "max_concurrent_requests": 64 }
  }
}
```

Unspecified limits use `native.DefaultLimits` and `codexshim.DefaultLimits`. Values are counts or
bytes, except native retention settings, which are seconds. Unknown fields and invalid values are
rejected. The `codex` limit block configures the native backend's WebSocket shim. Neither limit
block applies to the upstream backend. Changes take effect on backend restart.

See the [owner and placement rules](../docs/config.md#file-fields) and the
[operator guide](docs/codex_executor.md) for the Pod companion command, VM placement and recovery.
[Isolation and resources](ISOLATION.md) explains the shared resource and trust boundaries.

`//agentplane/env/e2e:test_executor_container` exercises native and upstream Codex backend selection,
process output, Session resume, filesystem access, incarnation fencing and shutdown through Runtime.

TODO: Reconsider embedding the native execution server in Environment Runtime once the ownership,
resource isolation, and failure behavior of that arrangement have been evaluated.

## Source ownership

- `native/`: process, session and file ownership.
- `typedbackend/` and `typedproxy/`: internal typed operations and Runtime admission.
- `adapter/`: typed Executor RPCs and health probing over the Codex exec-server protocol (the `codex` implementation).
- `codexshim/`: Codex WebSocket compatibility surface.
- `backend/`: shared backend configuration, launch, health monitoring and fencing.
- `companion/`: Pod PID-1 ownership protocol and its client, shared reaper integration.
- `service/`: environment identity and embedded adapter lifetime under Runtime ownership.
- `settings/`: native and Codex limit overrides, parsed for Runtime and the server.
- `cmd/`: `agentplane-execution-service` entrypoint (`companion`, `serve`, `command-helper`); `testing/`: shared companion container fixtures.

**Deviation:** the proto keeps the published `agentplane.executor` wire package while source and
client import paths live under `env/runtime/execution`. Relocation changes neither RPC method names nor
message fields, so current Gateway, Runtime and sampler versions still agree during deployment.

## Restarting an executor

Native sessions and processes are in-memory state. Before restarting an executor, inventory the retained
sampler execution bindings and reconcile unfinished operations. Preserve authored files, Session journals and
volumes, and read [staging data preservation](../../../docs/staging_data_preservation.md) before a deployment
rollback.
