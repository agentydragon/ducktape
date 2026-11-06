# gmail_api

Shared Gmail resource models used by `gmail_archiver/` and `x/google_mcp_server/` live in
`labels.py`, `filters.py`, and `messages.py`.

`service.py` builds Gmail clients for the archiver from google-auth authorized-user OAuth token
files, which google-auth refreshes. The remote `x/google_mcp_server/` instead builds bearer-only
Google credentials from its Airlock-minted access token in `GOOGLE_MCP_TOKEN_FILE`; its separate
`GOOGLE_MCP_BEARER_TOKEN` authenticates callers to the MCP server.
