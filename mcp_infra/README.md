# mcp-infra

Shared support for the repo's FastMCP servers.

- `exec/` — Docker exec server backend and image-reading helpers
- `authentik_auth/` — Authentik-backed MCP auth: provider composition, RFC 7523 token
  exchange, FastMCP proxy compatibility
- `static_bearer.py` — fixed-bearer ASGI guard for cluster-internal endpoints
- `persistence.py` — server state store config (PostgreSQL / Valkey)
- `constants.py` — shared path and process constants
- `testing/` — fixtures, naming and result matchers
