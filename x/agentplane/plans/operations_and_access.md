# Operations and access: remaining adapter work

The canonical Action lifecycle, catalog, Decisions, caller/operator projections, cancellation, and
notification-driven waits are implemented. Their contracts live in the
[Action Service specification](../action_service/SPEC.md) and [README](../action_service/README.md).
Claims, leases, unknown outcomes, and reconciliation live in
[executor liveness](../docs/executor_liveness.md); browser/operator authority lives in
[operator federation](../docs/operator_federation.md). Do not reopen these as first-adapter design gates.

The [task DAG](task_dag.md) tracks the remaining work. The existing credentialless MCP runtime and
review UI need deployed acceptance (`MCP0` / `APPROVALUI`), not reimplementation; use the
[acceptance instructions](../acceptance/README.md).

## Credentialed upstream accounts (`MCPAUTH` / `CRED`)

Choose the account/credential owner and binding lifecycle before implementing a credentialed adapter.
A broker or existing account authority should own browser OAuth state, token exchange/refresh, and
durable account association; the harness must not receive those credentials. Action execution uses
a reviewed opaque account binding. Keep this outbound account connection separate from inbound
Claude.ai/Claude Code enrollment in [external MCP connections](external_mcp_connections.md).

Prove account linkage, discovery refresh, one safe read, refresh/reconnect, revocation, and isolation
from unbound/different accounts. If standalone operation requires new OAuth support, reuse existing
protocol machinery and implement only the separately tested missing seam. The credential owner and
static binding model require operator design confirmation; they do not gate the credentialless path.

## Concrete long-running adapters (`HOSTEXEC`)

A concrete long-running consumer must define the backend's supported, authenticated and bounded
progress/status/output-so-far observations for the existing Execution. Use authoritative
reconciliation only where the backend supports it; otherwise retain an unknown outcome. A missed
heartbeat cannot prove an external effect stopped. Status reads never create another dispatch or
blind retry. Preserve backend machine/user authorization and keep reusable privileged credentials
outside the harness.

Prove exactly one invocation, authorized/redacted observations, safe terminal or unknown results,
and duplicate-start refusal against the concrete backend. Do not build a generic worker transport
without a consumer requiring it. Broader delegated-versus-brokered policy is in
[external access](external_access.md); standing grants remain separate access objects.

## Delivery and policy

[Asynchronous approvals](async_approvals.md) owns remaining human notification and Thread delivery.
[Action policies](action_policies.md) owns future mandatory bounds and reusable auto-approval policy
bindings. Neither adds another Action lifecycle, event store, or approval authority.
