# grocy_mcp

Auth-aware remote MCP server for [Grocy](https://grocy.info/), generated from
Grocy's OpenAPI 3.1 spec via FastMCP's `OpenAPIProvider`. Authenticates MCP
clients (claude.ai / Claude Code) through Authentik, then drives Grocy's
REST API on behalf of the calling user through Grocy's existing Authentik
proxy provider outpost.

FastMCP's `OIDCProxy` handles user-facing MCP OAuth. Before each backend call,
`CurrentAccessToken` supplies the user's upstream Authentik JWT to an Authentik
JWT-bearer `client_credentials` exchange. This is Authentik's JWT-bearer grant,
not RFC 8693 token exchange. The exchanged token is scoped to Grocy's proxy
provider so its outpost accepts the request and forwards the user's identity.

The exchange must request `openid email profile ak_proxy`, and the corresponding
scope mappings must be attached to the proxy provider. Omitting `ak_proxy` can
still yield a successful outpost response with empty identity headers. The
implementation is in <../mcp_infra/authentik_auth/token_exchange.py>; it uses
`CurrentAccessToken`, not the raw HTTP `Authorization` header, because
`OIDCProxy` exposes the upstream token through that dependency.

## Architecture

There are now two households, each with its own MCP server and Grocy instance:

- `grocy-mcp-sf.allegedly.works` → `grocy-sf.allegedly.works`
- `grocy-mcp-vallejo.allegedly.works` → `grocy-vallejo.allegedly.works`

```
claude.ai ──OAuth (MCP spec)──▶ https://grocy-mcp-{sf,vallejo}.allegedly.works/mcp
                                 (FastMCP + OIDCProxy, direct uvicorn)
                                      │
                                      │ tool dependency resolution
                                      │   1. CurrentAccessToken()
                                      │      (upstream user JWT resolved by OIDCProxy)
                                      │   2. POST https://auth.allegedly.works/application/o/token/
                                      │            grant_type=client_credentials
                                      │            client_id=<grocy proxy provider client_id>
                                      │            client_assertion_type=jwt-bearer
                                      │            client_assertion=<user JWT>
                                      │            scope=openid email profile ak_proxy
                                      │   3. Depends(...) yields a GrocyClient carrying
                                      │      Authorization: Bearer <new JWT>
                                      │   4. tool body starts
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

| Tool                 | Replaces                | Description                                             |
| -------------------- | ----------------------- | ------------------------------------------------------- |
| `entities_create`    | `create_entity`         | Create N entities of any type in one call               |
| `entities_list`      | `list_entities`         | Fetch N entity types concurrently                       |
| `entities_get`       | `get_entity`            | Fetch N objects of one type by ID                       |
| `stock_get`          | `list_stock`            | Stock list with optional QU + location enrichment       |
| `stock_add`          | `add_product_stock`     | Add stock for N products; returns `new_amount` per item |
| `stock_consume`      | `consume_product_stock` | Consume stock for N products                            |
| `stock_set`          | `inventory_product`     | Set absolute amounts for N products                     |
| `stock_entries_list` | `GET /stock/entry/{id}` | Fetch entries by product or ID, with enrichment         |
| `stock_entry_edit`   | `PUT /stock/entry/{id}` | Partial-update N stock entries concurrently             |

Stock operations return `transaction_id` per item for per-operation undo via the
`transaction_undo` tool. There is no cross-item atomicity — Grocy has no
client-initiated batch transaction API.

### OpenAPI-generated tools (from Grocy spec)

All remaining enabled routes from `/objects/*` and `/stock/*`:
`entity_update`, `entity_delete`, `get_product_stock`, `transfer_product_stock`,
`open_product_stock`, `list_product_stock_entries`, `list_product_locations`,
`products_merge`, `list_location_stock`, `list_volatile_stock`,
`shopping_list_*`, and others. See <tool_metadata.py> for the full list.

Explicitly excluded: `/chores`, `/batteries`, `/recipes`, `/tasks`, `/calendar`,
`/print`, `/files`, `/users`, `/user`, `/userfields`, and most `/system/*` routes
(except `get_system_info` and `get_db_changed_time`). Add a
`RouteMap(pattern=..., mcp_type=MCPType.TOOL)` in <server.py> to re-enable any.

## Why FastMCP's OpenAPI provider and not hand-written tools?

Grocy already publishes a complete OpenAPI 3.1 spec at
`/api/openapi/specification`. Hand-writing tool wrappers would duplicate
the schema for every entity and every stock endpoint, and drift the
moment Grocy ships a new field. `OpenAPIProvider` plus a route filter gives us
the entire surface without duplicating those schemas.

Per-call auth is the only custom execution layer. Hand-written tools declare a
hidden `Depends(...)` client parameter. Generated `OpenAPITool` components pass
through <../mcp_infra/request_scoped_openapi.py>, a small compatibility transform
that supplies the same dependency because FastMCP 3.4.4 has no public
per-invocation OpenAPI client factory. Dependency resolution completes before
the tool body, and the exchanged credential is passed explicitly in the
returned client; there is no cross-request token cache or ambient bearer lookup.

## Deploying

- **Terraform** (Authentik providers + K8s secret) is bundled into the
  existing `tf/gitops/agent-machine-access` module, which
  already owns the Grocy proxy provider. That module now also creates:
  - `authentik_provider_oauth2.grocy_mcp` — user-login AS that OIDCProxy
    wraps.
  - `jwt_federation_providers` on the existing Grocy proxy provider,
    pointing at the OAuth2 provider above.
  - Kubernetes secrets in the `grocy-sf` / `grocy-vallejo` namespaces,
    carrying `client_id`, `client_secret`, and `grocy_proxy_client_id`.
- **K8s manifests** are generated per household by <../cluster/cdk8s/grocy/mcp.py>,
  with the settings file included. Terraform is shared with `agent-machine-access`.

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
bbr test //grocy_mcp:test_server
```

## End-to-end verification

After Flux reconciles the per-household Grocy MCP deployment:

```bash
curl -i https://grocy-mcp-sf.allegedly.works/mcp
# HTTP/2 401 Unauthorized
# WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"

claude mcp add --transport http grocy-mcp-sf https://grocy-mcp-sf.allegedly.works/mcp
# Browser → Authentik consent → connected.

# In /mcp → grocy-mcp, call a tool generated from
# `GET /objects/{entity}` with `entity=locations`.
# Expected: JSON array of the user's Grocy locations.
```

If the backend returns success but identity headers are empty, check that the
exchange requests `ak_proxy` and that the Grocy proxy provider has its
`ak_proxy` scope mapping attached. An empty Grocy result can also mean the
authenticated user lacks permission to see those records.
