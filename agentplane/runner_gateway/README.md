# Runner Gateway

The Sandbox runners' side of the Sandbox Service: Sessions, command admission and delivery, and
the [runner channel](../docs/runner_channel.md) runners dial. It resolves runners in the Sandbox
Service's inventory (`agentplane.sandbox_service.inventory`, `destinations`) and does not
provision or delete Sandboxes.

- `runner_channel.py`, `channel_delivery.py`, `channel_control.py`, `connection_records.py`: the
  runner connection, routing commands and lifecycle requests to the replica holding it.
- `session_registry.py`, `session_lifecycle.py`, `command_relay.py`:
  Session identities, opening and resuming Sessions, and command admission.
- `commands/`: the `sandbox_commands` database that holds them.

The Sandbox Service still serves this code's gRPC API from its one Deployment; the
[API](../sandbox_service/API.md) describes it.
