# MCP edge

The standalone `mcp-edge` binary is the next caller of the Action Service's gRPC API. It owns
external Connections, grants and OAuth tokens in its PostgreSQL database, serves the operator
[`ConnectionService`](connection_service.proto) over gRPC and HTTP/JSON, and, when OAuth is
configured, serves `/mcp` and the OAuth protocol routes. The integrated edge in the Action Service
remains the live endpoint until the separate deployment and traffic cutover.
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

<!-- CLEANUP(added 2026-10-23): Remove the integrated Action Service edge after the public
     route and app Connection callers have moved, source image has replaced every Actions Pod,
     and the edge table transfer has been verified. -->
