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

## Executors that answer as MCP tools

`tool_results.py` renders an outcome per executor kind: an MCP group's stored `CallToolResult` as
it is, the sandbox executor's own JSON models the way FastMCP presents a returned model. Unifying
the executors behind MCP, with the sandbox executor answering as an MCP tool and storing a
`CallToolResult` too, would leave one result shape and remove that dispatch.
