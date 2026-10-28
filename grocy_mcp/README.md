# grocy_mcp

Auth-aware remote MCP server for [Grocy](https://grocy.info/), one per household:

- `grocy-mcp-sf.allegedly.works` → `grocy-sf.allegedly.works`
- `grocy-mcp-vallejo.allegedly.works` → `grocy-vallejo.allegedly.works`

Deploy, spec refresh and end-to-end verification: <docs/operations.md>.

## Auth

FastMCP `OIDCProxy` handles user-facing MCP OAuth against Authentik (the MCP server is **not**
behind the outpost: claude.ai needs RFC 7591 DCR and PKCE to OIDCProxy's own redirect URI, not a
forward-auth 302). Per tool call, a `Depends(...)` provider takes the user's upstream JWT from
`CurrentAccessToken` (not the raw `Authorization` header — OIDCProxy exposes the upstream token
only there) and exchanges it via Authentik's JWT-bearer `client_credentials` grant (not RFC 8693)
for a token scoped to the household's Grocy proxy provider. The resulting `GrocyClient` calls
`https://grocy-<household>.allegedly.works/api/...`; the embedded outpost introspects the token
and injects `X-Authentik-*` identity headers. No cross-request token cache, no ambient bearer.
Implementation: <../mcp_infra/authentik_auth/token_exchange.py>.

**Gotcha:** the exchange must request `openid email profile ak_proxy`, with those scope mappings
attached to the proxy provider. Without `ak_proxy` the outpost can still succeed with empty
identity headers.

## Tool surface

claude.ai does not expose MCP resources to the model (as of 2026-04-14): add capabilities as
tools only.

- **OpenAPI-generated** via FastMCP `OpenAPIProvider` from Grocy's OpenAPI 3.1 spec (fixed up at
  build time by <fix_openapi_spec.py>): every route needs a `TOOL_OVERRIDES` entry in
  <tool_metadata.py> (name, extra description, `enabled`), or the build raises. Output schemas
  are stripped (Grocy's are unreliable; e2e tests are the contract). Generated tools get the
  per-call client through <../mcp_infra/request_scoped_openapi.py>: FastMCP binds the configured
  client to each generated tool, so the transform binds the `Depends` client to an invocation-local
  copy while preserving the generated route metadata.
- **Custom batch tools** in <batch_tools.py> (`register_batch_tools`) replace single-shot routes
  with N-item operations via `asyncio.gather`. A failed item is reported `ok=False` without
  aborting the rest. Stock operations return a `transaction_id` per item for
  `transaction_undo`; there is no cross-item atomicity (Grocy has no client batch transaction).

Generating from the spec rather than hand-writing wrappers keeps the surface in step with
Grocy's schema; per-call auth is the only custom execution layer.
