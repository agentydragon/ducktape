# TODO — Agentplane Action Service

Gaps to close if a workflow needs them; none is committed.

## Dynamic Client Registration for MCP OAuth linkage

`McpOAuthServer.client_id` (`mcp_linkage.py`) is required, so an upstream MCP server can be linked
only through a pre-registered client, never one registered at link time via RFC 7591.

## `client_secret_basic` token-endpoint authentication

`McpLinkageAuthority._post_token` (`mcp_linkage.py`) sends a confidential client's secret only in
the form body (`client_secret_post`), so an authorization server requiring HTTP Basic client
authentication cannot be linked.

## Per-group Agent tool denylist

An `McpExecutorBinding` group (`mcp_executor.py`) offers every upstream tool as an Action. A group
could name tools to hide from discovery and refuse at admission, such as GitHub's Copilot
delegation tools.
