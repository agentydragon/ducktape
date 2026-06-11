# mcp-oauth-facade image

Container image that runs `//mcp_infra/oauth_facade:server` as a Starlette HTTP
server. Configured entirely via `MCP_FACADE_*` env vars (see
`mcp_infra/oauth_facade/README.md`). Reused by every Authentik-backed MCP
deployment in the cluster — Tana (HTTP upstream), Manifold (stdio upstream
populated by an init container), and any future instance.
