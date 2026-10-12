# Runner Gateway

The Sandbox runners' side of Agentplane: Sessions, command admission and delivery, and the
[runner control stream](../docs/runner_control.md) runners dial. Its own server (`:server`, image `:image`)
serves the `RunnerGateway` gRPC service ([API](API.md), [`runner_gateway.proto`](../proto/runner_gateway.proto)) with
replicas, apart from the Sandbox Service's
single reconciler and its RBAC. It reads Sandboxes through the Sandbox Service's read-only
`SandboxReader` (`agentplane.sandbox_service.reader`), which serves agent-sandbox's and Agentplane's
own, and does not provision or delete them.

It does not call the History Service, so Sessions, commands and lifecycle requests keep working
while that service is down or rolling. A deleting Sandbox of Agentplane's own kind gets no
`SessionsSealed` condition until the runner reports every seal acknowledged by its ingress
(`deletion_seals.py`), so it is not deleted until the History Service is back.

- `runner_control.py`, `lifecycle_control.py`, `connection_records.py`: the runner connection, and
  the commands and lifecycle requests the replica holding it sends on it.
- `session_registry.py`, `session_lifecycle.py`: Session identities, and opening and resuming
  Sessions.
- `commands/`: the `sandbox_commands` database that holds the Session identities.
- `destinations.py`: checking that a request's Sandbox incarnation has a ready runner Pod.
- `sealing.py`: sealing a Sandbox's runner for teardown, on the Sandbox Service's request.
- `deletion_seals.py`: the same seal for a deleting Sandbox of Agentplane's own kind
  (`agentplane/sandbox_resource.py`), run by the replica holding its runner connection, which then
  sets the Sandbox's `SessionsSealed` condition for the Sandbox Controller.
- `grpc_api.py`, `settings.py`, `main.py`: the server.
