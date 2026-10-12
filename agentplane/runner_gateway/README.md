# Runner Gateway

The Sandbox runners' side of Agentplane: Sessions, command admission and delivery, and the
[runner control stream](../docs/runner_control.md) runners dial. Its own server (`:server`, image `:image`)
serves the `RunnerGateway` gRPC service ([API](API.md), [`runner_gateway.proto`](../proto/runner_gateway.proto)) with
replicas, apart from the Sandbox Service's
single reconciler and its RBAC. It reads Sandboxes through the Sandbox Service's read-only
`SandboxReader` (`agentplane.sandbox_service.reader`) and does not provision or delete them.

- `runner_control.py`, `lifecycle_control.py`, `connection_records.py`: the runner connection, and
  the commands and lifecycle requests the replica holding it sends on it.
- `session_registry.py`, `session_lifecycle.py`: Session identities, and opening and resuming
  Sessions.
- `commands/`: the `sandbox_commands` database that holds the Session identities.
- `destinations.py`: checking that a request's Sandbox incarnation has a ready runner Pod.
- `sealing.py`: sealing a Sandbox's runner for teardown, on the Sandbox Service's request.
- `grpc_api.py`, `settings.py`, `main.py`: the server.
