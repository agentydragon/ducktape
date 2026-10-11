# Runner Gateway

The Sandbox runners' side of Agentplane: the [runner control stream](../docs/runner_control.md)
runners dial, the Session registry, authorized access to runner sessions, command admission and
delivery, and event following. It is the only Agentplane service that reaches a runner. Its own
server (`:server`, image `:image`) serves the `RunnerGateway` gRPC service
([`runner_gateway.proto`](../proto/runner_gateway.proto), described in the Sandbox Service's
[API](../sandbox_service/API.md)) with replicas, apart from the Sandbox Service's single reconciler
and its RBAC. It resolves runners in the Sandbox Service's inventory
(`agentplane.sandbox_service.inventory`, `destinations`) and does not provision or delete Sandboxes.

- `runner_control.py`, `command_delivery.py`, `lifecycle_control.py`, `connection_records.py`: the
  runner connection, routing commands and lifecycle requests to the replica holding it.
- `session_registry.py`, `session_lifecycle.py`, `command_relay.py`:
  Session identities, opening and resuming Sessions, and command admission.
- `commands/`: the `sandbox_commands` database holding the Session registry, command admission and
  runner connections.
- `sealing.py`: sealing a Sandbox's runner for teardown, on the Sandbox Service's request.
- `grpc_api.py`, `settings.py`, `main.py`: the server.
