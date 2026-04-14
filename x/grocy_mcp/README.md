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

```
claude.ai ──OAuth (MCP spec)──▶ https://grocy-mcp.allegedly.works/mcp
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
                              https://grocy.allegedly.works/api/...
                                      │
                              Authentik embedded proxy outpost
                              (introspects token, injects X-Authentik-{Username,Email,...})
                                      ▼
                              http://grocy.grocy.svc.cluster.local:80/api/...
```

The MCP server itself is **not** behind the outpost — claude.ai drives
OAuth directly against OIDCProxy, which requires RFC 7591 DCR and PKCE to
its own redirect URI, not a forward-auth 302. Only the downstream call into
Grocy traverses the outpost.

## Tool surface

Generated from <grocy_openapi.json> (Grocy 4.6.0 OpenAPI 3.1 spec from
<https://demo.grocy.info/api/openapi/specification>). Filtered via
`ROUTE_MAPS` in <server.py> to just `/objects/*` and `/stock/*`, which
covers the bootstrap path "empty Grocy → populated inventory":

- `/objects/{entity}` — generic CRUD over quantity units, locations,
  product groups, products, quantity-unit conversions, etc. Discoverable
  entities: see the `entity` path parameter in Grocy's docs.
- `/stock` — current stock listing.
- `/stock/products/{id}/{add,consume,inventory,...}` — stock manipulation.

Explicitly excluded: `/chores`, `/batteries`, `/recipes`, `/tasks`,
`/calendar`, `/print`, `/files`, `/users`, `/user`, `/userfields`,
`/system`. If you later want any of these, add a
`RouteMap(pattern=..., mcp_type=MCPType.TOOL)` higher in the list in
<server.py>.

## Why `FastMCP.from_openapi` and not hand-written tools?

Grocy already publishes a complete OpenAPI 3.1 spec at
`/api/openapi/specification`. Hand-writing tool wrappers would duplicate
the schema for every entity and every stock endpoint, and drift the
moment Grocy ships a new field. `FastMCP.from_openapi` plus a route
filter gives us the entire surface in 20 LoC.

Per-request auth is the only thing that needs to be custom, and
`httpx.Auth.async_auth_flow` is exactly the right seam: it runs inside
the request lifecycle, `await`s fine, and has access to FastMCP's
request-scoped contextvars via `get_access_token()`. See <auth.py>.

## Deploying

- **Terraform** (Authentik providers + K8s secret) is bundled into the
  existing `cluster/terraform/gitops/agent-machine-access` module, which
  already owns the Grocy proxy provider. That module now also creates:
  - `authentik_provider_oauth2.grocy_mcp` — user-login AS that OIDCProxy
    wraps.
  - `jwt_federation_providers` on the existing Grocy proxy provider,
    pointing at the OAuth2 provider above.
  - `kubernetes_secret "grocy-mcp-oidc"` in the `grocy-mcp-oidc`
    namespace, carrying `client_id`, `client_secret`, and
    `grocy_proxy_client_id`.
- **K8s manifests** live at <../../cluster/k8s/agents/grocy-mcp-oidc/>
  and follow the POC's three-layer pattern (namespace / TF / app) minus
  the `tf/` layer since TF is shared with `agent-machine-access`.

## Refreshing the OpenAPI spec

The spec is checked in so container builds are deterministic and have no
startup-time dependency on demo.grocy.info. Refresh when Grocy ships a
new version:

```bash
curl -sSfL https://demo.grocy.info/api/openapi/specification \
  -o x/grocy_mcp/grocy_openapi.json
git diff x/grocy_mcp/grocy_openapi.json
bbr test //x/grocy_mcp:test_server   # catches schema drift
```

## End-to-end verification

Same shape as <../authentik_mcp_poc/README.md>'s "Verification in the
cluster" section. After Flux reconciles `grocy-mcp-oidc-namespace` →
`agent-machine-access-tf` → `grocy-mcp-oidc`:

```bash
curl -i https://grocy-mcp.allegedly.works/mcp
# HTTP/2 401 Unauthorized
# WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"

claude mcp add --transport http grocy-mcp https://grocy-mcp.allegedly.works/mcp
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
