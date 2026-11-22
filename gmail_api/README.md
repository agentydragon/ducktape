# gmail_api

`labels.py` holds the Gmail label models shared by `gmail_archiver/` and `x/google_mcp_server/`.
`filters.py` and `messages.py` are used only by `x/google_mcp_server/`; the archiver keeps its own
filter models in `gmail_archiver/gmail_api_models.py`.

`service.py` builds Gmail clients for the archiver from google-auth authorized-user OAuth token
files, which google-auth refreshes. The remote `x/google_mcp_server/` instead builds bearer-only
Google credentials from its Airlock-minted access token in `GOOGLE_MCP_TOKEN_FILE`; its separate
`GOOGLE_MCP_BEARER_TOKEN` authenticates callers to the MCP server.
