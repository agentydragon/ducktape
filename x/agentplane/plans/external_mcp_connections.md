# External MCP connections: remaining delivery

The Identity, Connection, OAuth/DCR, consent, and generic MCP contracts live in the
[Action Service specification](../action_service/SPEC.md),
[service README](../action_service/README.md#external-oauth), and
[app README](../app/README.md); the [task DAG](task_dag.md) records what staging has proven and
owns dependencies and status. This plan keeps the client-compatibility notes and the Haku
migration, not another authority contract. Identity means configured authority, Connection means
runtime client enrollment, and Thread means execution/conversation state. This remains
single-operator, with no multi-operator management. Sandbox callers use the same MCP frontend with
workload bearers, without DCR; that path is proven by the
[acceptance suite](../acceptance/README.md#mcp-integration).

## Compatibility and context budget

Operator REST and enrollment-management routes must not be exposed through the public MCP route;
this is unconfirmed on the deployed route.

Fix compatibility from actual client evidence using the pinned FastMCP/Authlib and shared
`mcp_infra` stack. Do not reopen a preimplementation DCR research gate or replace the framework's
protocol machinery. Preserve DCR support and keep authority independent of registration mechanism.

The generic-tool wire schemas are implemented. Remaining ergonomics are empirical: can each client
discover only needed metadata, retain submission keys across ambiguity, and resume pending work?
Default discovery must stay compact; do not flood generic tool schemas or receipts with the catalog.
Per-Action MCP projection is optional and may never be needed. New metadata, including output
schemas, is deferred and is not an acceptance requirement. Notification-driven wait internals and
race tests already exist; fix regressions found in delivery rather than planning another wait loop.

## Enrollment retention cleanup

Before adding registration/enrollment retention cleanup, identify actual growth and choose bounded
expiry/cleanup behavior that preserves immutable historical attribution and replay tombstones.
Do not turn cleanup into a gate for first client use or delete grant history opportunistically.

## Deferred: Identity authority beyond Actions

The broader use case is access to Agentplane services from both hosted and external harnesses.
Hosted agents are intended to access multiple services using Sandbox-token authentication;
external harnesses should also be eligible for explicitly granted access to services beyond Actions,
without becoming Agentplane-managed Sandboxes or Threads. Conversation search and reading other
agents' conversations are examples, not the boundary of this work.

Discuss the common identity/authentication boundary and each service's authorization contract for
both caller types. Decide whether to move the MCP frontend, Connection-to-Identity binding
authority, or both into a shared component, and whether external access uses a shared facade,
direct service endpoints, or a combination. Current in-process placement is the first delivery
slice, not an irrevocable ownership decision; other services need not become Action executors to
be accessible.

Keep each resource-owning component responsible for its authorization. Shared identity resolution
would not imply that Action permissions or same-Identity Action reads grant conversation access.
Settle resource-specific permissions/selectors, credential audiences and service boundaries,
trusted identity propagation, revocation consistency, and immutable caller/client provenance before
that cross-service extension. Neither a Sandbox token nor an external Identity implies unrestricted
access to every service; sharing authentication infrastructure does not require one bearer accepted
everywhere. SandboxPreset remains
integration-app-owned; this possibility does not make downstream services interpret its recipes.

This is the deferred `IDENTITY_SCOPE` discussion in [the DAG](task_dag.md), independent of the
first deployed client proof. Do not preempt that delivery with extraction or speculative abstractions.

## Haku migration

After real-client proof, inventory and migrate Haku's required mounted MCP servers, account
bindings, policy assignments, human approvals, and result workflows individually through `MCPAGG`.
The deterministic fixture is not tool parity. Outbound credentialed-account OAuth remains
`MCPAUTH`; it is separate from these inbound client credentials.

External Claude Code needs no Agentplane Sandbox/Thread or upstream credential. Only calls routed
through Actions receive its governance; its local tools remain governed by the harness and host.
Do not couple initial client delivery to hosted credentialless MCP acceptance or complete Haku retirement.
`RETIRE_TOOLS` and `RETIRE_AGENT` keep separate export, rollback, and decommissioning gates.
