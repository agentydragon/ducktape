# Runner Gateway

The Runner Gateway holds the runners' outbound
[RunnerControl stream](../docs/runner_control.md), publishes which replica holds each connection,
and proxies the runner's `Runner` RPCs to that connection. The app uses the per-Sandbox proxy for
live inventory and explicit native resume; Session openings and durable command admission go through
the Session Dispatcher. The Gateway reads Sandbox and Pod state and writes connection Leases and
EndpointSlices; the Sandbox Controller owns `Sandbox.status`.

The Gateway has no database, migration init container or Session registry. Other services retain
their databases on the shared PostgreSQL cluster.

The Gateway has no History Service client. Its proxy can serve live runner calls while History is
unavailable, but it does not replace Dispatcher admission or History's retained conversation.

- `runner_control.py`, `lifecycle_control.py`, `connection_records.py`: runner connections, the
  control requests sent over them, and per-Sandbox connection ownership.
- `runner_proxy.py`: the runner's `Runner` service, forwarded through the connection held by this
  replica ([runner proxy](../docs/runner_control.md#runner-proxy)).
- `destinations.py`: validates the Sandbox incarnation and ready runner Pod before proxying.
- `commands/`: legacy migration artifacts pending removal after database-drop reconciliation.
- `settings.py`, `main.py`: server settings and listener wiring.
