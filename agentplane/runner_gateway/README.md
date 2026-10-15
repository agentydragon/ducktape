# Runner Gateway

The Sandbox runners' side of Agentplane: the
[runner control stream](../docs/runner_control.md), proxy and live runner access. Its server
(`:server`, image `:image`) also serves the `RunnerGateway` gRPC service
([API](API.md), [`runner_gateway.proto`](../proto/runner_gateway.proto)) with replicas. The app opens
Sessions and sends commands through Session Dispatcher; Gateway `CreateSession` and `SubmitCommand`
remain legacy direct operations with no production sender. It reads Sandboxes through the shared
read-only projection (`agentplane.sandboxes.reader`) and does not create or delete them.

It does not call the History Service. Its runner proxy can continue serving live calls while History
is unavailable, but this does not replace Dispatcher admission or History's retained conversation.

The Sandbox Controller owns `Sandbox.status`, including the `Quiesced` condition. The Gateway reads
Sandboxes and Pods, and writes only its per-Sandbox runner connection Leases and EndpointSlices; its
Role has no `sandboxes/status` write permission.

- `runner_control.py`, `lifecycle_control.py`, `connection_records.py`: the runner connection, and
  the commands and lifecycle requests the replica holding it sends on it.
- `runner_proxy.py`: the runner's `Runner` service, forwarded unchanged to the runner through the
  connection this replica holds ([runner proxy](../docs/runner_control.md#runner-proxy)).
- `session_registry.py`, `session_lifecycle.py`: the legacy direct API's Session-incarnation registry
  and opening/resume helpers. Only Gateway `CreateSession` writes the registry; Dispatcher-opened
  Sessions are absent, so it is not complete inventory.
- `commands/`: the `sandbox_commands` database that currently holds the Session registry; it does not
  hold current command admission rows.
- `destinations.py`: checking that a request's Sandbox incarnation has a ready runner Pod.
- `grpc_api.py`, `settings.py`, `main.py`: the server.
