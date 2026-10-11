# Runner Gateway

The Sandbox runners' side of Agentplane: Sessions, command admission and delivery, and the
[runner control stream](../docs/runner_control.md) runners dial. Its own server (`:server`, image `:image`)
serves the `RunnerGateway` gRPC service ([`protocol.proto`](protocol.proto), described in the
Sandbox Service's [API](../sandbox_service/API.md)) with replicas, apart from the Sandbox Service's
single reconciler and its RBAC. It resolves runners in the Sandbox
Service's inventory (`agentplane.sandbox_service.inventory`, `destinations`) and does not
provision or delete Sandboxes.

- `runner_control.py`, `command_delivery.py`, `lifecycle_control.py`, `connection_records.py`: the
  runner connection, routing commands and lifecycle requests to the replica holding it.
- `session_registry.py`, `session_lifecycle.py`, `command_relay.py`:
  Session identities, opening and resuming Sessions, and command admission.
- `commands/`: the `sandbox_commands` database that holds them.
- `sealing.py`: sealing a Sandbox's runner for teardown, on the Sandbox Service's request.
- `grpc_api.py`, `settings.py`, `main.py`: the server.

Until the manifests run its Deployment, the Sandbox Service's one Deployment serves this API too
(`Launch.gateway_target` unset).
