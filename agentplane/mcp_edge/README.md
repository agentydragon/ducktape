# MCP edge

The standalone `mcp-edge` binary is the next caller of the Action Service's gRPC API. It owns
external Connections, grants and OAuth tokens in its PostgreSQL database, serves the operator
[`ConnectionService`](connection_service.proto) over gRPC and HTTP/JSON, and, when OAuth is
configured, serves `/mcp` and the OAuth protocol routes. Testing sends operator Connection calls to the standalone edge. Staging Connection calls and
external MCP/OAuth still use the integrated edge in Actions until their separate handoff.
The cutover sets Actions' `integrated_connection_service` to false and removes its OAuth
configuration before transferring Connection and OAuth table ownership. Actions then registers
neither the Connection gRPC service nor its generated HTTP facade.

## Authority

The Connection API verifies the app's request bound operator OIDC bearer independently of MCP
OAuth. Each MCP request resolves its issued bearer through the current grant and Connection
binding. The edge offers only ServiceAccounts both labeled as Action callers and named in
`mintable_accounts`, its explicit TokenRequest scope. It reads the account's current UID and label,
requests a token for that account with the Action Service's `agentplane-egress` audience and a
requested ten minute lifetime, and sends it to Actions over gRPC. Actions independently reviews the
token and current caller label. A watch rechecks the grant, binding, label and UID on a five second
ticker and before forwarding each frame. Tool arguments never name the caller identity.

## Deployment contract

`AGENTPLANE_MCP_EDGE_CONFIG_FILE` names the YAML settings file; `AGENTPLANE_MCP_EDGE_*`
environment variables override scalar keys, with `__` for nested keys. `database_url` is required
and can be supplied through `AGENTPLANE_MCP_EDGE_DATABASE_URL`. `actions_target` is the internal
Action Service gRPC endpoint. `caller_service_account_namespaces` and `mintable_accounts` take JSON
arrays in the environment; `mintable_accounts` entries are `namespace/name`. `operator_oidc`
requires the issuer, audience, JWKS URI and token profile used by the app's operator tokens.
`oauth` is an optional JSON object in the environment, using the current OAuth secret's keys; when
absent, the Connection API remains available and the external protocol routes are absent. The
secret's `upstream_client_secret_file` must be mounted at the path it names. The listener defaults
to `0.0.0.0:8080` and multiplexes gRPC and HTTP. gRPC health uses the empty service for readiness
and `livez` for liveness. Readiness requires PostgreSQL, its notification listener and a fresh
caller label watch.

The published image is `agentplane-mcp-edge`, built from `//agentplane/mcp_edge:service_image`.
The independent Goose migrator's image is `agentplane-mcp-edge-migrate`; both read
`AGENTPLANE_MCP_EDGE_DATABASE_URL`.

## Schema and data handoff

Initialize the destination with `//agentplane/mcp_edge/migrations:image` before copying or serving.
The operator command `//agentplane/mcp_edge/schema_transfer/cmd:mcp-edge-transfer` copies only:

- `external_connection`, `external_connection_grant`, `connection_enrollment`
- `oauth_registered_client`, `oauth_authorization`, `oauth_token`

Set `AGENTPLANE_ACTIONS_DATABASE_URL` and `AGENTPLANE_MCP_EDGE_DATABASE_URL` in the operator's
environment and pass `--confirm-single-writer-cutover`. Stop legacy Actions and its migrators,
and keep destination writers stopped throughout the copy. The command requires an empty destination
and recognized schema histories, holds source table `SHARE` locks, copies and compares every
supported field inside one destination transaction, and prints only row counts. Lock waits fail
after five seconds; SIGINT/SIGTERM cancels the operation. Failures before commit roll back the
destination. Action requests, decisions, executions, policies and Web Push state do not move.

Locks fence writes only while copying; they cannot prevent a legacy process from restarting.
Keep the legacy owner stopped until the deployed Actions image has
`integrated_connection_service=false` and no OAuth configuration. Verify that its Connection gRPC
service and HTTP facade are absent before resuming Actions core. The edge becomes the only writer;
retain source tables until a later verified cleanup migration. An empty-source cutover needs no
copy, but recheck state at handoff rather than relying on an earlier inventory.

Rollout and remaining cleanup: [MCP edge extraction](../plans/mcp_edge_extraction.md).
