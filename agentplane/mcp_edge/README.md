# MCP edge

The standalone `mcp-edge` binary is the external MCP endpoint and Connection/OAuth authority. It
owns its PostgreSQL schema, serves the operator [`ConnectionService`](connection_service.proto)
over gRPC and HTTP/JSON, and, when OAuth is configured, serves `/mcp` and the OAuth protocol routes.
It calls the Action Service over gRPC using the currently bound, labeled ServiceAccount identity.
The Action Service keeps upstream MCP linkage and CIMD, but does not serve external `/mcp`, OAuth,
or ConnectionService endpoints.

## Authority

The Connection API verifies the app's request-bound operator OIDC bearer independently of MCP
OAuth. Each MCP request resolves its issued bearer through the current grant and Connection
binding. The edge offers only ServiceAccounts both labeled as Action callers and named in
`mintable_accounts`, its explicit TokenRequest scope. It reads the account's current UID and label,
requests a token for that account with the Action Service's `agentplane-egress` audience and a
requested ten minute lifetime, and sends it to Actions over gRPC. Actions independently reviews the
token and current caller label. A watch rechecks the grant, binding, label and UID on a five second
ticker and before forwarding each frame. The operator watch bounds waiting for each stream write to
clear transport backpressure at five seconds; an exceeded bound ends `DEADLINE_EXCEEDED`. An idle
drain sends `reconnect_required`; a drain during a write ends that stream without another frame. This
bound does not guarantee when a queued frame reaches the peer. Tool arguments never name the caller
identity.

## Deployment contract

`AGENTPLANE_MCP_EDGE_CONFIG_FILE` names the YAML settings file; `AGENTPLANE_MCP_EDGE_*`
environment variables override scalar keys, with `__` for nested keys. `database_url` is required
and can be supplied through `AGENTPLANE_MCP_EDGE_DATABASE_URL`. `actions_target` is the internal
Action Service gRPC endpoint. `caller_service_account_namespaces` and `mintable_accounts` take JSON
arrays in the environment; `mintable_accounts` entries are `namespace/name`. `operator_oidc`
requires the issuer, audience, JWKS URI and token profile used by the app's operator tokens.
`oauth` is an optional JSON object in the environment, using the current OAuth Secret's keys; when
absent, the Connection API remains available and the external protocol routes are absent. The
secret's `upstream_client_secret_file` must be mounted at the path it names. The listener defaults
to `0.0.0.0:8080` and multiplexes gRPC and HTTP. gRPC health uses the empty service for readiness
and `livez` for liveness. Readiness requires PostgreSQL, its notification listener and a fresh
caller label watch.

The published image is `agentplane-mcp-edge`, built from `//agentplane/mcp_edge:service_image`.
The independent Goose migrator's image is `agentplane-mcp-edge-migrate`; both read
`AGENTPLANE_MCP_EDGE_DATABASE_URL`.

## Schema and data handoff

The Action Service's legacy Connection/OAuth rows and tables are retained until a separately
verified data handoff and retention decision. The operator command
`//agentplane/mcp_edge/schema_transfer/cmd:mcp-edge-transfer` copies only:

- `external_connection`, `external_connection_grant`, `connection_enrollment`
- `oauth_registered_client`, `oauth_authorization`, `oauth_token`

Set `AGENTPLANE_ACTIONS_DATABASE_URL` and `AGENTPLANE_MCP_EDGE_DATABASE_URL` in the operator's
environment and pass `--confirm-single-writer-cutover`. Stop legacy Actions and its migrators, and
keep destination writers stopped throughout the copy. The command requires an empty destination
and recognized schema histories, holds source table `SHARE` locks, copies and compares every
supported field inside one destination transaction, and prints only row counts. Lock waits fail
after five seconds; SIGINT/SIGTERM cancels the operation. Failures before commit roll back the
destination. Action requests, Decisions, Executions, policies and Web Push state do not move.

Locks fence writes only while copying; they cannot prevent a legacy process from restarting. Keep
the legacy owner stopped after transfer. Retain source tables until a separately reviewed cleanup
migration; do not use rollback as a reason to restart a writer after the edge accepts writes. An
empty-source transfer needs no copy, but recheck state at handoff rather than relying on an earlier
inventory.

## Configuration rollout sequencing

The generated Action Service settings still include `integrated_connection_service: false` and
`max_wait_seconds: 180` for compatibility with older pinned images. The new Action Service rejects
`integrated_connection_service: true` and registers no legacy endpoints even when the setting is
absent. Its `max_wait_seconds` value is now an accepted cleanup tombstone capped at the generated
180-second maximum; the MCP edge owns caller
wait limits. `direct_wait_seconds` remains an Action Service setting for `orchestration_v1`; the MCP
edge has its own independent wait settings.

Remove the two compatibility keys from generated manifests only after the Action Service image
containing their false-only/no-op parser is observed running. Deploy that config removal before
removing the parser fields in a later image. `direct_wait_seconds` remains a live Action Service
setting for `orchestration_v1` and is separate from the edge's own wait settings. Endpoint ownership
changes do not transfer or drop database rows or edit landed migrations.
