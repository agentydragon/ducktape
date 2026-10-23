# MCP edge extraction

Go Actions, generated callers and Goose are deployed in staging and testing. Python runtime and
Alembic source retirement is complete. Goose migration 00005 is applied in both tiers; the final
legacy grant column is absent. The [schema receipt](../debug/actions_schema_retirement_20261023.md)
records retained-state checks. External-user MCP reauthorization remains manual and unverified.

Actions keeps lifecycle, policies, executors, upstream MCP linkage and Web Push. The external
Connection/OAuth authority and `/mcp` move to `mcp-edge`.

## Cutover requirements

- **Preserve data and one writer.** Retain Connection and grant identities, current OAuth state,
  requests/history, Sandboxes and their volumes. No drop/recreate shortcut. The operator waived
  restoration testing and accepts database-stored secrets without application-level encryption.
  Issued opaque tokens remain stored only as hashes.
- **Images before activation.** Publish runtime and migrator images and let Flux select their tags
  before starting the edge. Deploy and verify testing before staging. Keep published images, but
  images that read the dropped legacy columns cannot run against the current Actions schema.
- **Preserve authority.** The edge acts as the current bound ServiceAccount, with restricted
  TokenRequest permissions and grant/binding rechecks on calls and watch frames. Connection API
  calls retain operator OIDC independently of external MCP OAuth. Do not grant the edge all-owner
  `reader_accounts` access.
- **Keep sandbox identities and storage.** CLI updates live in `agentplane-runner-disk`. Reconcile
  templates before resumes; preserve suspended state. Idle Sandbox resume uses the app's wake
  generation operation, not merely `running: true`.
- **Validate the actual route.** Check deployed image digests, health, retained identities/history,
  CLI calls through egress, operator decisions, public MCP/OAuth and Connection reconnect behavior.
  No restoration drill is required. External clients reauthorize once for the Go transition;
  preserve issuer/resource URLs and copied token state so extraction does not require another.

## Remaining work

The edge Deployment, Service, migrator and scoped RBAC are staged with **zero replicas in both
environments**. Testing initializes its empty edge schema through the Flux-managed
`mcp-edge-schema-migrate` Job before data transfer; staging still has no edge migration Job.
No serving Pod starts or route changes while scaled down: public MCP/OAuth paths
and the app's Connection calls still target Actions. Actions-owned CIMD metadata and the app's
upstream OAuth callbacks remain on their current routes. Activation is a separate change after the edge
images are published, its database is populated from the preserved Action Service records, and the
deployed Actions image accepts the edge's account-only tokens. Verify Connection and grant identities,
request records and external MCP authorization before moving the public route and app target.
Flux checks the schema Job for completion and recreates it when the automated migrator image
changes. It only has the edge database credential; it cannot copy source records.
That route change must add the edge's Gateway ingress policy; the staged policy admits only the app
and metrics scrapers.

1. **Edge database and data copy (database before code).** The edge owns the independent `mcp_edge`
   database, with Goose migrations from `//agentplane/mcp_edge/migrations:image` and
   `AGENTPLANE_MCP_EDGE_DATABASE_URL`. The operator copy command is
   `//agentplane/mcp_edge/schema_transfer/cmd:mcp-edge-transfer`; it is deliberately limited to
   `external_connection`, `external_connection_grant`, `connection_enrollment`,
   `oauth_registered_client`, `oauth_authorization`, and `oauth_token`. It preserves row identities,
   grant history, and OAuth values. It rejects an unrecognized source schema/history, an unexpected
   destination relation, or any row in the destination tables. The retired grant
   `activation_deadline` column is omitted if it still exists in Actions; all other table columns
   must match the edge schema. No ActionRequest, Decision, Execution, policy, or Push state moves.

   Run it only as one cutover window: migrate the empty destination first; stop Actions and all
   Actions-side migrators; confirm the edge has no destination writers; then run the command with
   `AGENTPLANE_ACTIONS_DATABASE_URL` and `AGENTPLANE_MCP_EDGE_DATABASE_URL` set in the operator's
   environment and `--confirm-single-writer-cutover`. The transfer holds source table `SHARE` locks
   while reading, copies and compares every supported field inside one destination transaction,
   and reports only per-table row counts. Lock waits fail after five seconds; SIGINT/SIGTERM cancels
   the operation. Any pre-commit failure rolls the destination back. Keep the legacy Actions runtime
   stopped until an Actions image with `integrated_connection_service=false` no longer registers or
   serves the in-process ConnectionService and HTTP facade and cannot write these six tables. That
   gate also rejects OAuth being enabled; unsetting OAuth configuration alone does not disable the
   ConnectionService. The edge must be the only writer for the six tables; once that gate is verified,
   Actions core may resume independently.
   Database locks only fence writes during the copy and cannot prevent a process from restarting.
   The source copy remains in place in this PR; do not start both old and new edge owners or clean
   up the old tables here.
   This preparation does not execute the transfer or change live data.

2. Actions accepts ServiceAccount tokens without a Pod binding through the explicit account-only
   resolver; its reader image is deployed in both tiers. Exercise the edge-to-Actions path after cutover.
   Caller namespace, audience, ServiceAccount UID and live label checks still apply. This is token acceptance
   before token issuance; the edge acts as an owner-scoped caller, not an all-owner `reader_accounts` identity.
3. `mcp-edge`: `main`, Deployment, restricted TokenRequest rights and token minting for the currently bound
   accounts; the in-process `ActionService` call becomes gRPC. Its minted audience must match the Actions
   verifier (currently `agentplane-egress`; a dedicated audience is a separate reader-before-writer change).
   Preserve grant/binding rechecks on every MCP call and while forwarding watches. The Connection API keeps
   operator-OIDC verification and the app's request-bound operator bearer independently of MCP client OAuth.
4. Move the external MCP/OAuth routes and the app's Connection calls to the edge while preserving the
   public issuer/resource URLs. Keep Actions' upstream CIMD at `/oauth/client-metadata.json` and the
   app's upstream OAuth callback at `/mcp-linkage/callback`.
5. The tables leave the `actions` database, in a separate data change.
