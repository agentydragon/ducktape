# External MCP connections: remaining delivery

Configured static Identities, PostgreSQL Connection/grant authority, generic FastMCP tools,
OAuth/DCR, transaction-bound integration-app consent with naming/Identity selection, reconnect/rebind,
Action provenance display, and Connection list/rename/unbind UI are implemented. Their contracts live in the
[Action Service specification](../action_service/SPEC.md),
[service README](../action_service/README.md#external-oauth), and
[app README](../app/README.md). This plan tracks unfinished delivery, not another authority contract.

**Operator priority:** working deployed Claude.ai access (`CLAUDEAI`) is the current priority.
Transcript search (`T3`) is deliberately deferred product work and is not part of this execution
sequence. Independently running Claude Code (for example on wyrm2) extends the client evidence to
`EXTERNALMCP`; the [task DAG](task_dag.md) owns dependencies and status.

The first external requests require human approval. Configurable
[Action policies](action_policies.md), backend-account OAuth, and the broader Thread model do not
gate this delivery. Identity means configured authority, Connection means runtime client enrollment,
and Thread means execution/conversation state. This remains single-operator, with no multi-operator
management. Sandbox callers use the same MCP frontend with workload bearers, without DCR.

## Staging rollout (`MCPDEPLOY`)

Staging carries the reviewed GitHub and Kubernetes MCP server configuration, reflected GitHub
client credentials, the staging OAuth callback route, and Action Service egress. Do not enable
enrollment against images older than the merged OAuth/consent code or infer rollout from merged
source.

After operator-approved rollout, verify migration completion, required reflected configuration,
canonical discovery/resource/callback URLs, and external MCP reachability. Check the workload path
in parallel, without gating Claude.ai acceptance on it. Do not expose operator REST or
enrollment-management routes through the public MCP route. A healthy deployment is intermediate
evidence, not real-client acceptance.

## Real-client acceptance (`CLAUDEAI`, then `EXTERNALMCP`)

Use a harmless credentialless Action and independently inspect canonical requests, Decisions,
Executions, events, and results; neither model prose nor signed protocol fixtures prove deployment.
Record the deployed revisions, client/version, exact scenario, and redacted evidence. Reuse existing
acceptance helpers before adding duplicates.

The protocol-side acceptance should use the pinned FastMCP client rather than a second hand-rolled
MCP implementation. Build a small live/manual Bazel target that performs DCR, drives the browser
authorization/consent handoff, retains the resulting access/refresh token family only in an
ephemeral in-process provider, and gives that provider to `fastmcp.Client` for MCP initialization,
discovery, and the Action call. The test client owns no Agentplane authority: it proves the public
OAuth/MCP client contract while the Action Service remains the authority for grants and Decisions.
An access-token-only pass is sufficient for the first call; refresh/reconnect acceptance must also
exercise the provider's refresh path. Do not print or persist token values.

1. Connect Claude.ai through public discovery and DCR. Complete the real integration-app login,
   Connection naming, Identity picker, and consent. Verify return to the client's validated callback,
   code exchange, and authenticated generic-tool discovery. Registration alone grants no authority;
   denying consent creates no active grant.
2. Discover compact Action metadata, opt into the needed input schema/full description, and submit
   with a retained idempotency key. Get a durable pending receipt without automatic execution.
   In the deployed app, inspect exact arguments and Identity/client/Connection provenance; exercise
   both Allow and Deny browser controls. Allow yields one Execution and the expected safe result;
   deny yields none. Claude.ai must recover those receipts/results.
3. Exercise bounded waits and interrupted/retried tool responses against the real client. Reuse the
   request ID, event cursor, and original submission key rather than creating another execution.
   Refresh must retain the grant; service/client restart must not lose pending requests or attribution.
   Do not assume Claude will autonomously poll or wake once its conversation stops.
4. Verify wrong-resource/invalid tokens, disabled Identities, unbound Connections, and another
   Identity cannot acquire caller or operator authority. Same-Identity clients share receipt and
   idempotency scope but retain distinct exact submission provenance; a retry cannot rewrite it.
   After unbind/revocation, old tokens fail and unclaimed work cannot borrow replacement authority.
   Already-claimed execution is not killed.

`APPROVALUI` tracks the real operator federation/browser proof; the Authentik reachability defect
that blocked it is fixed cluster-wide
([root cause and rollout](../../../cluster/debug/agentplane_oidc/local_gateway_tls_rca.md)).
Provenance presentation is already implemented, not another UI task.

Record Claude.ai success separately as `CLAUDEAI`. Then repeat the client flow with Claude Code
running on an operator machine, not an Agentplane-hosted harness, to establish `EXTERNALMCP`.
Its native callback, registration, refresh, and fresh authorization need their own evidence.
Existing-Connection reconnect acceptance is part of the real-client connection evidence, not a separate implementation gate.

**Parallel Sandbox proof, not a Claude.ai prerequisite:** verify the same generic workflow from a
real Sandbox through workload bearer substitution, retaining per-Sandbox ownership without OAuth
enrollment. Service token validation alone does not establish staging egress usability.

### Compatibility and context budget

Fix compatibility from actual client evidence using the pinned FastMCP/Authlib and shared
`mcp_infra` stack. Do not reopen a preimplementation DCR research gate or replace the framework's
protocol machinery. Preserve DCR support and keep authority independent of registration mechanism.

The generic-tool wire schemas are implemented. Remaining ergonomics are empirical: can each client
discover only needed metadata, retain submission keys across ambiguity, and resume pending work?
Default discovery must stay compact; do not flood generic tool schemas or receipts with the catalog.
Per-Action MCP projection is optional and may never be needed. New metadata, including output
schemas, is deferred and is not an acceptance requirement. Notification-driven wait internals and
race tests already exist; fix regressions found in delivery rather than planning another wait loop.

## Later policy and lifecycle work

Policy definitions and Identity/Sandbox-to-policy assignments remain the separate `POLICYBIND`
decision. Runtime Connection/grant/enrollment storage is already PostgreSQL, with encrypted
PostgreSQL storage for SDK OAuth state; do not reopen it as part of policy selection. Reusable
policy references must allow an external Identity and a class of Sandboxes to share permissions
without sharing caller ownership or copying rules. SandboxPreset remains an app-only recipe.

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
