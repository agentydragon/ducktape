# External MCP connections: remaining delivery

Configured static Identities, PostgreSQL Connection/grant authority, generic FastMCP tools,
OAuth/DCR, transaction-bound integration-app consent with naming/Identity selection, and Action
provenance display, and Connection list/rename/unbind UI are implemented. Their contracts live in the
[Action Service specification](../action_service/SPEC.md),
[service README](../action_service/README.md#external-oauth), and
[app README](../app/README.md). This plan tracks unfinished delivery, not another authority contract.

**Operator priority:** working deployed Claude.ai access (`CLAUDEAI`) first, transcript search
(`T3`) next. This is ordering, not a technical dependency. Independently running Claude Code
(for example on wyrm2) extends the client evidence to `EXTERNALMCP`; it does not delay that search
priority condition. The [task DAG](task_dag.md) owns dependencies and status.

The first external requests require human approval. Configurable
[Action policies](action_policies.md), backend-account OAuth, and the broader Thread model do not
gate this delivery. Identity means configured authority, Connection means runtime client enrollment,
and Thread means execution/conversation state. This remains single-operator, with no multi-operator
management. Sandbox callers use the same MCP frontend with workload bearers, without DCR.

## Staging rollout (`MCPDEPLOY`)

[#5926](https://github.com/agentydragon/ducktape/pull/5926) prepares dedicated Authentik OAuth
configuration, persistent signing/encryption keys, a configured Identity, exact public protocol
routes, and the existing workload-token substitution path for `/mcp`. It is draft until published
Action Service, migration, and app images contain the merged OAuth/consent code and compatible pins
are ready. Do not enable configuration against older images or infer rollout from merged source.

After operator-approved rollout, verify migration completion, required reflected configuration,
canonical discovery/resource/callback URLs, and external MCP reachability. Check the workload path
in parallel, without gating Claude.ai acceptance on it. Follow the staging runbook in
that PR; do not expose operator REST or enrollment-management routes through the public MCP route.
A healthy deployment is intermediate evidence, not real-client acceptance.

## Connection reconnect and rebind (`RECONNECT`)

List/detail, rename, and confirmed unbind are implemented in the app/BFF. Deployed management
acceptance remains: verify rename preserves authority and history, and unbind refuses old-token
access. The UI distinguishes configured-Identity availability from current grant state.

**Next independent feature:** allow fresh OAuth consent to select an existing Connection for
reconnect or explicit Identity change. The authority already exposes version-checked
`ReconnectConnection`; enrollment currently always creates `NewConnection`. Extend that same
browser-bound enrollment and BFF with a new/existing choice, the reviewed Connection version,
and authority-change confirmation. Do not add an independent grant-changing endpoint or infer a
Connection from mutable names or a new DCR registration.

Reuse the settled lifecycle: binding the replacement pending revision ends the old grant, before
successful activation; failure does not restore it. Old tokens never inherit the replacement
Identity. Rename is presentation only; names need not be unique. Original Action ownership and
client/Connection/grant provenance remain immutable, including on duplicate submission.
Test same-Identity reconnect, A-to-B rebind, stale/concurrent consent, failed issuance, old-token
rejection, and original-grant dispatch checks. This follow-up does not gate initial new enrollment.

## Real-client acceptance (`CLAUDEAI`, then `EXTERNALMCP`)

Use a harmless credentialless Action and independently inspect canonical requests, Decisions,
Executions, events, and results; neither model prose nor signed protocol fixtures prove deployment.
Record the deployed revisions, client/version, exact scenario, and redacted evidence. Reuse existing
acceptance helpers and reconcile the open
[#5822](https://github.com/agentydragon/ducktape/pull/5822) evidence work before adding duplicates.

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

`APPROVALUI` tracks the real operator federation/browser proof. The login/mapping fixes in
[#5922](https://github.com/agentydragon/ducktape/pull/5922) did not complete that gate; check their
rollout before rerunning it. Provenance presentation is already implemented, not another UI task.

Record Claude.ai success separately as `CLAUDEAI`. Then repeat the client flow with Claude Code
running on an operator machine, not an Agentplane-hosted harness, to establish `EXTERNALMCP`.
Its native callback, registration, refresh, and fresh authorization need their own evidence.
Existing-Connection reconnect acceptance belongs to `RECONNECT`, not the initial connection gate.

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
Do not couple initial client delivery to hosted `MCP0` acceptance or complete Haku retirement.
`RETIRE_TOOLS` and `RETIRE_AGENT` keep separate export, rollback, and decommissioning gates.
