# MCP edge extraction

Go Actions, generated callers, Python retirement and legacy schema cleanup are complete;
[retained-state evidence](../debug/actions_schema_retirement_20261023.md) lives outside the plan.
Runtime, schema/transfer tooling, scoped RBAC and Flux image automation are merged. The edge
Deployment configuration is one replica in testing (#11727 merged), zero in staging; testing's
schema Job has applied migration 1. Testing activation has not yet reconciled. Public routes
and app Connection calls still use Actions (verified 2026-10-23).

## Remaining work

1. **Testing activation and callers.** Verify the merged testing edge rollout, its health and
   caller informer. Land the app's independent Connection endpoint setting with both
   tiers still pointing at Actions. Switch testing's Connection endpoint and admit app-to-edge
   Cilium egress; retire the integrated ConnectionService after its consumers have moved.
   Testing's six source tables were empty at preflight, so no copy is needed if they remain empty.
   Recheck both databases at handoff and preserve any new records. Do not stop the old endpoint
   while its consumers still depend on it.
2. **Staging handoff.** Initialize the edge schema, fence legacy writers and preserve the state found at preflight
   (one Connection, three grants and four enrollments) using the
   [transfer procedure](../mcp_edge/README.md#schema-and-data-handoff). OAuth client, authorization
   and token tables were empty at preflight; recheck before execution. Move the app's Connection
   calls and external MCP/OAuth routes, adding app egress and Gateway ingress explicitly.
   Preserve public issuer/resource URLs, Actions' `/oauth/client-metadata.json` and the app's
   `/mcp-linkage/callback`. Verify current bindings, operator authorization and actual edge-to-Actions
   calls; external-client reauthorization is still unverified.
3. **Retire integrated ownership.** After both tiers use the edge, remove Actions' integrated
   Connection/OAuth runtime and configuration. Drop its six old tables through a separate new
   migration after the handoff is verified; retain Actions history, decisions, executions,
   policies and Web Push state.

## Cutover constraints

- Preserve Connection/grant identities, requests/history, Sandboxes, suspended state and volumes.
  The operator waived restoration testing and accepts database-stored secrets without application
  encryption; issued opaque tokens remain hashed.
- Keep one writer for transferred records. `integrated_connection_service=false` removes Actions'
  Connection gRPC service and facade and rejects OAuth configuration; removing OAuth alone does
  not fence Connection writes. Never restart the legacy owner after a data transfer.
- Use published, Flux-selected images. Verify testing before staging. Images that read removed
  Actions columns cannot run against its current schema.
- Keep operator OIDC independent of external MCP OAuth. The edge acts only as an explicitly
  permitted, currently bound ServiceAccount; Actions still checks namespace, audience, UID and
  caller label. No all-owner reader identity. See the [edge authority contract](../mcp_edge/README.md#authority).
