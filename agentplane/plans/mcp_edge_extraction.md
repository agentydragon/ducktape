# MCP edge extraction

Go Actions, generated callers, Python retirement and legacy schema cleanup are complete;
[retained-state evidence](../debug/actions_schema_retirement_20261023.md) lives outside the plan.
Staging's handoff configuration is merged (#11729, #11734): one edge replica serves the public
MCP/OAuth routes and the app's Connection calls, and Actions runs with
`integrated_connection_service: false`. The k3s system tests run the app against the standalone edge.

## Remaining work

1. **Retire integrated ownership.** Remove Actions' integrated Connection/OAuth runtime, its
   ConnectionService and configuration. Drop its six old tables through a separate new migration
   after the handoff is verified; retain Actions history, decisions, executions, policies and Web
   Push state. Actions keeps its upstream `/oauth/client-metadata.json` and the app its
   `/mcp-linkage/callback`.

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
