# Runner Gateway

The Sandbox runners' side of Agentplane: Sessions, command admission and delivery, and the
[runner control stream](../docs/runner_control.md) runners dial. Its own server (`:server`, image `:image`)
serves the `RunnerGateway` gRPC service ([API](API.md), [`runner_gateway.proto`](../proto/runner_gateway.proto)) with
replicas, apart from the Sandbox Service and its RBAC. It reads Sandboxes
(`agentplane/sandbox_resource.py`) through the Sandbox Service's read-only `SandboxReader`
(`agentplane.sandboxes.reader`), and does not create or delete them.

It does not call the History Service, so Sessions, commands and lifecycle requests keep working
while that service is down or rolling.

- `runner_control.py`, `lifecycle_control.py`, `connection_records.py`: the runner connection, and
  the commands and lifecycle requests the replica holding it sends on it.
- `runner_proxy.py`: the runner's `Runner` service, forwarded unchanged to the runner through the
  connection this replica holds ([runner proxy](../docs/runner_control.md#runner-proxy)).
- `session_registry.py`, `session_lifecycle.py`: Session identities, and opening and resuming
  Sessions.
- `commands/`: the `sandbox_commands` database that holds the Session identities.
- `destinations.py`: checking that a request's Sandbox incarnation has a ready runner Pod.
- `grpc_api.py`, `settings.py`, `main.py`: the server.
