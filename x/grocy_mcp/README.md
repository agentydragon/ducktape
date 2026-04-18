# grocy_mcp

Auth-aware remote MCP server for [Grocy](https://grocy.info/), generated from
Grocy's OpenAPI 3.1 spec via `FastMCP.from_openapi`. Authenticates MCP
clients (claude.ai / Claude Code) through Authentik, then drives Grocy's
REST API on behalf of the calling user through Grocy's existing Authentik
proxy provider outpost.

This is built on top of the pattern that the authentik MCP POC
(<../authentik_mcp_poc/README.md>) proved out: FastMCP's `OIDCProxy` for the
user-facing MCP OAuth dance, and an RFC 7521 jwt-bearer token exchange on
the backend hop so Authentik's proxy outpost mints an identity-preserving
token that Grocy's `ReverseProxyAuthMiddleware` accepts.

## Architecture

There are now two households, each with its own MCP server and Grocy instance:

- `grocy-mcp-sf.allegedly.works` → `grocy-sf.allegedly.works`
- `grocy-mcp-vallejo.allegedly.works` → `grocy-vallejo.allegedly.works`

```
claude.ai ──OAuth (MCP spec)──▶ https://grocy-mcp-{sf,vallejo}.allegedly.works/mcp
                                 (FastMCP + OIDCProxy, direct uvicorn)
                                      │
                                      │ generated tool call
                                      │   → httpx.AsyncClient.request(...)
                                      │     ↓ AuthentikExchangeAuth
                                      │       1. get_access_token().token  (upstream user JWT)
                                      │       2. POST https://auth.allegedly.works/application/o/token/
                                      │            grant_type=client_credentials
                                      │            client_id=<grocy proxy provider client_id>
                                      │            client_assertion_type=jwt-bearer
                                      │            client_assertion=<user JWT>
                                      │            scope=openid email profile ak_proxy
                                      │       3. request.headers["Authorization"] = f"Bearer {new JWT}"
                                      ▼
                              https://grocy-{sf,vallejo}.allegedly.works/api/...
                                      │
                              Authentik embedded proxy outpost
                              (introspects token, injects X-Authentik-{Username,Email,...})
                                      ▼
                              http://grocy.grocy-{sf,vallejo}.svc.cluster.local:80/api/...
```

The MCP server itself is **not** behind the outpost — claude.ai drives
OAuth directly against OIDCProxy, which requires RFC 7591 DCR and PKCE to
its own redirect URI, not a forward-auth 302. Only the downstream call into
Grocy traverses the outpost.

## MCP resources vs tools

As of 2026-04-14, claude.ai does not expose MCP resources to the AI. Everything
the AI needs to invoke must be a tool. `get_system_info` (from `GET /system/info`)
is exposed as a tool and is available to the AI. New capabilities should be
added as tools, not resources.

## Tool surface

The tool surface combines OpenAPI-generated tools (from Grocy's spec) with custom
batch tools registered by `register_batch_tools` in <batch_tools.py>.

### Batch tools (custom, in <batch_tools.py>)

These replace the equivalent single-shot OpenAPI routes to enable efficient
multi-item operations via `asyncio.gather`. Each failed item is collected with
`ok=False`; failures do not abort the others.

| Tool              | Replaces                | Description                                             |
| ----------------- | ----------------------- | ------------------------------------------------------- |
| `entities_create` | `create_entity`         | Create N entities of any type in one call               |
| `entities_list`   | `list_entities`         | Fetch N entity types concurrently                       |
| `entities_get`    | `get_entity`            | Fetch N objects of one type by ID                       |
| `stock_get`       | `list_stock`            | Stock list with optional QU + location enrichment       |
| `stock_add`       | `add_product_stock`     | Add stock for N products; returns `new_amount` per item |
| `stock_consume`   | `consume_product_stock` | Consume stock for N products                            |
| `stock_set`       | `inventory_product`     | Set absolute amounts for N products                     |

Stock operations return `transaction_id` per item for per-operation undo via the
`transaction_undo` tool. There is no cross-item atomicity — Grocy has no
client-initiated batch transaction API.

### OpenAPI-generated tools (from Grocy spec)

All remaining enabled routes from `/objects/*` and `/stock/*`:
`entity_update`, `entity_delete`, `get_product_stock`, `transfer_product_stock`,
`open_product_stock`, `list_product_stock_entries`, `list_product_locations`,
`products_merge`, `list_location_stock`, `get_stock_entry`, `stock_entry_edit`,
`list_volatile_stock`, `shopping_list_*`, and others. See <tool_metadata.py> for
the full list.

Explicitly excluded: `/chores`, `/batteries`, `/recipes`, `/tasks`, `/calendar`,
`/print`, `/files`, `/users`, `/user`, `/userfields`, and most `/system/*` routes
(except `get_system_info` and `get_db_changed_time`). Add a
`RouteMap(pattern=..., mcp_type=MCPType.TOOL)` in <server.py> to re-enable any.

## Why `FastMCP.from_openapi` and not hand-written tools?

Grocy already publishes a complete OpenAPI 3.1 spec at
`/api/openapi/specification`. Hand-writing tool wrappers would duplicate
the schema for every entity and every stock endpoint, and drift the
moment Grocy ships a new field. `FastMCP.from_openapi` plus a route
filter gives us the entire surface in 20 LoC.

Per-request auth is the only thing that needs to be custom, and
`httpx.Auth.async_auth_flow` is exactly the right seam: it runs inside
the request lifecycle, `await`s fine, and has access to FastMCP's
request-scoped contextvars via `get_access_token()`. See
<../../mcp_infra/authentik_auth/auth.py>.

## Deploying

- **Terraform** (Authentik providers + K8s secret) is bundled into the
  existing `cluster/terraform/gitops/agent-machine-access` module, which
  already owns the Grocy proxy provider. That module now also creates:
  - `authentik_provider_oauth2.grocy_mcp` — user-login AS that OIDCProxy
    wraps.
  - `jwt_federation_providers` on the existing Grocy proxy provider,
    pointing at the OAuth2 provider above.
  - Kubernetes secrets in the `grocy-sf` / `grocy-vallejo` namespaces,
    carrying `client_id`, `client_secret`, and `grocy_proxy_client_id`.
- **K8s manifests** live at <../../cluster/k8s/grocy/{sf,vallejo}/mcp/>
  and follow the POC's three-layer pattern (namespace / TF / app) minus
  the `tf/` layer since TF is shared with `agent-machine-access`.

## Refreshing the OpenAPI spec

The spec is fetched at build time from
`https://raw.githubusercontent.com/grocy/grocy/<tag>/grocy.openapi.json`
via the `@grocy_openapi_spec` `http_file` repo declared in
<../../MODULE.bazel>. Refresh when Grocy ships a new version:

```bash
# 1. Pick a new release tag from https://github.com/grocy/grocy/releases
NEW_TAG=v4.7.0
# 2. Fetch the file and recompute its sha256
curl -sSfL "https://raw.githubusercontent.com/grocy/grocy/${NEW_TAG}/grocy.openapi.json" \
  | sha256sum
# 3. Bump `urls` and `sha256` on the `grocy_openapi_spec` http_file in MODULE.bazel
# 4. Run the smoke test — catches new spec quirks (e.g. additional empty
#    enums) FastMCP rejects.
bbr test //x/grocy_mcp:test_server
```

## End-to-end verification

Same shape as <../authentik_mcp_poc/README.md>'s "Verification in the
cluster" section. After Flux reconciles the per-household grocy namespace →
`agent-machine-access-tf` → grocy MCP app:

```bash
curl -i https://grocy-mcp-sf.allegedly.works/mcp
# HTTP/2 401 Unauthorized
# WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"

claude mcp add --transport http grocy-mcp-sf https://grocy-mcp-sf.allegedly.works/mcp
# Browser → Authentik consent → connected.

# In /mcp → grocy-mcp, call one of the auto-generated tools, e.g. the
# tool generated from `GET /objects/{entity}` with `entity=locations`.
# Expected: JSON array of the user's Grocy locations.
```

Failure-mode mapping is the POC's 12-component table
(<../authentik_mcp_poc/README.md>); the only new mode is "tool returns
200 with an empty result" which usually means Grocy accepted the
request but the authenticated user's permissions in Grocy hide
everything.
