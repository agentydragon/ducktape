# MCP edge

The standalone `mcp-edge` binary is the external MCP endpoint and Connection/OAuth authority. It
owns its PostgreSQL schema, serves the operator [`ConnectionService`](api/connection/connection_service.proto)
over gRPC and HTTP/JSON, and, when OAuth is configured, serves `/mcp` and the OAuth protocol routes.
It calls the Action Service over gRPC with its own projected workload token and a method-scoped,
Connection-backed Identity delegation.
The Action Service keeps upstream MCP linkage and CIMD, but does not serve external `/mcp`, OAuth,
or ConnectionService endpoints.

For a failed Action or an unknown execution outcome, `get_action_result` and direct Action tools
return `isError: true` and structured content containing `request_id`, `state`, and the retained
execution `error` object when present. These are the same error details included in the text
fallback and caller-authorized Action receipt; clients can inspect them without parsing text.
An unknown outcome still requires inspecting effects before requesting another execution.

## Agent guides

`get_guide(name="index")` reads the workflow index; request `session_delegation` before launching
child Sessions, or another named guide only when needed. This generic read-only tool needs no shell,
HTTP client or execution environment and remains available when the Action catalogue cannot be read.
It creates no Action request and grants no authority. The guide is returned as `{name, markdown}`
in structured content with the same JSON text fallback.

The edge embeds the same [guide assets](../action_service/docs/agents/llms.txt) as the Agentplane CLI.
Each binary carries its documentation snapshot; use `list_actions`, `get_action` and
`get_action_policy` for current operations, schemas and permissions. The MCP initialization
instructions point to the guide reader, while detailed workflows stay out of standing context.

## Authority

The Connection API verifies the app's request-bound operator OIDC bearer independently of MCP
OAuth. Each MCP request resolves its issued bearer through the current grant and Connection
binding. Consent and operator rebind choose a live Identity in the Action policy namespace and pin
its exact Kubernetes UID. Resolution checks that UID directly; an unavailable, deleted or replaced
Identity refuses the request. The operator-facing picker also requires a current Identity watch.

For each admitted Action RPC, the edge sends its own projected `agentplane-egress` audience token
plus typed Connection ID, grant ID and effective Identity provenance. Action middleware reviews the
edge token and checks the live grant and Connection binding. Only the explicitly configured Action
methods accept this provenance; it is not a general actor impersonation path. The edge does not
request or cache tokens for other ServiceAccounts. It rechecks the grant, binding and Identity on
each call and every five seconds while a stream is active; revocation latency is bounded by that
interval plus the direct UID lookup. The operator watch bounds waiting for each stream write to clear
transport backpressure at five seconds; an exceeded bound ends `DEADLINE_EXCEEDED`. An idle drain
sends `reconnect_required`; a drain during a write ends that stream without another frame. This
bound does not guarantee when a queued frame reaches the peer. Tool arguments never name the caller
Identity.

Existing Connections bound to a ServiceAccount are retained for history but paused by the schema
migration. No same-name Identity is inferred and no old bearer resolves until an operator explicitly
rebinds the Connection to a live Identity. The migration retains Connection and grant IDs, consent
and audit facts, revocation status, and the original ServiceAccount attribution. One existing
external Connection is expected to pause until that operator action. Rolling back to an old image
does not restore its authority: the retained `bound_caller` column is NULL and constrained against
old-writer updates. Recovery after rollback requires a reviewed forward migration/rebind; there is
no ServiceAccount fallback.

## Deployment contract

`AGENTPLANE_MCP_EDGE_CONFIG_FILE` names the complete YAML settings file. It is the only environment
input for static settings; `AGENTPLANE_MCP_EDGE_DATABASE_URL` is read explicitly from the PostgreSQL
Secret and is not part of the file model. `actions_target` is the internal Action Service gRPC
endpoint. OAuth deployments set `policy_namespace` to the namespace whose live Identities may be
selected, and `actions_token_file` to the mounted projected Action audience token. The edge refuses
use when required Identity state or the Action token is unavailable. `operator_oidc` requires the
issuer, audience, JWKS URI and token profile used by the app's operator tokens. `oauth` is an
optional typed inline object for synthetic deployments. Staging instead sets `oauth_file` to the
provider-owned settings file projected from the existing Authentik Secret, keeping its user and
provider bindings in Terraform. Inline and file-based OAuth settings cannot be combined. When both
are absent, the Connection API remains available and external protocol routes are absent. The
provider settings' `upstream_client_secret_file` names the separately mounted OAuth client Secret
file. The gRPC listener
(`port`) defaults to `0.0.0.0:8080`; the HTTP listener (`http_port`: the facade, `/mcp`, OAuth and metrics) to
`0.0.0.0:8081`. gRPC health uses the empty service for readiness and `livez` for liveness. Readiness
requires PostgreSQL, its notification listener and a synchronized live Identity view.

The published image is `agentplane-mcp-edge`, built from `//agentplane/mcp_edge:service_image`.
The same binary embeds Goose migrations and applies them before serving. `mcp-edge --migrate-only`
uses the same lifecycle for explicit initialization; both modes read `AGENTPLANE_MCP_EDGE_DATABASE_URL`.

## Connection and OAuth storage

The MCP edge owns Connection and external OAuth state in its `mcp_edge` database. Its serving
tables are independent of the Action Service database. Actions migration 9 retires the old copies
after the operator approved deleting their eight historical records. See the
[staging investigation](debug/staging_handoff_20261023.md) for the pre-retirement inventory.
Actions still owns upstream MCP server linkage and OAuth token state for configured Actions.

The operator accepts database-stored secrets without application-level encryption; issued opaque
tokens are stored only as hashes.
