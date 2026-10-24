# MCP edge extraction

Go Actions, generated callers, Python retirement and legacy schema cleanup are complete;
[retained-state evidence](../debug/actions_schema_retirement_20261023.md) lives outside the plan.
Runtime, schema/transfer tooling, scoped RBAC and Flux image automation are merged. The edge
Deployment is configured with zero replicas in staging. The operator dropped the testing
rehearsal (2026-10-24) and testing is deleted, so staging is the first cluster the edge runs on;
the k3s system tests already run the app against the standalone edge. Public routes and app
Connection calls still use Actions (verified 2026-10-23).

## Remaining work

1. **App Connection endpoint.** Land the app's independent Connection endpoint setting, still
   pointing at Actions. Do not stop the old endpoint while its consumers still depend on it.
2. **Staging handoff.** Initialize the edge schema, fence legacy writers and preserve the state found at preflight
   (one Connection, three grants and four enrollments) using the
   [transfer procedure](../mcp_edge/README.md#schema-and-data-handoff). OAuth client, authorization
   and token tables were empty at preflight; recheck before execution. Move the app's Connection
   calls and external MCP/OAuth routes, adding app egress and Gateway ingress explicitly.
   Preserve public issuer/resource URLs, Actions' `/oauth/client-metadata.json` and the app's
   `/mcp-linkage/callback`. Verify current bindings, operator authorization and actual edge-to-Actions
   calls; external-client reauthorization is still unverified.
3. **Retire integrated ownership.** After staging uses the edge, remove Actions' integrated
   Connection/OAuth runtime, its ConnectionService and configuration. Drop its six old tables through a separate new
   migration after the handoff is verified; retain Actions history, decisions, executions,
   policies and Web Push state.

## Cutover constraints

- Preserve Connection/grant identities, requests/history, Sandboxes, suspended state and volumes.
  The operator waived restoration testing and accepts database-stored secrets without application
  encryption; issued opaque tokens remain hashed.
- Keep one writer for transferred records. `integrated_connection_service=false` removes Actions'
  Connection gRPC service and facade and rejects OAuth configuration; removing OAuth alone does
  not fence Connection writes. Never restart the legacy owner after a data transfer.
- Use published, Flux-selected images, after the system tests that exercise the change pass, with
  the rollback in each PR ([changes that can break staging](../AGENTS.md#changes-that-can-break-staging)).
  Images that read removed Actions columns cannot run against its current schema.
- Keep operator OIDC independent of external MCP OAuth. The edge acts only as an explicitly
  permitted, currently bound ServiceAccount; Actions still checks namespace, audience, UID and
  caller label. No all-owner reader identity. See the [edge authority contract](../mcp_edge/README.md#authority).
