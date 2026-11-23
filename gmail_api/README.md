# gmail_api

The archiver and Google MCP server share Gmail label models, filter criteria and message headers.
The archiver requires known criteria when replaying filters, treats absent action label lists as empty,
and reads metadata-only messages. Its response models live in `gmail_archiver/gmail_api_models.py`.

`service.py` builds Gmail clients for the archiver from google-auth authorized-user OAuth token
files, which google-auth refreshes. The remote `x/google_mcp_server/` instead builds bearer-only
Google credentials from its Airlock-minted access token in `GOOGLE_MCP_TOKEN_FILE`; its separate
`GOOGLE_MCP_BEARER_TOKEN` authenticates callers to the MCP server.
