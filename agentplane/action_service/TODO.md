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

## Submit only if a policy decides

An agent that would rather not spend the operator's attention has no way to ask `request_action`
to run an Action only if a policy decides it, and to be told the refusal instead of queuing it for
a human. `ActionService.submit_decided` already refuses that way for direct tools, before anything
is persisted.

## Direct tools per Connection

Every external Connection sees the same configured `direct_tools`, narrowed only by its
ServiceAccount's policy. A client that should see a different set, or a workload that should see
any, needs a per-caller selection, e.g. on the ServiceAccount or the Connection.

## Direct tool calls rerun on a client retry

`call_direct` (`mcp_frontend.py`) submits each call under a fresh `direct-<uuid>` idempotency key,
and a policy approves it at admission, so a client that loses the response and calls again runs the
Action again: a second sandbox from `create`, a second run of `exec`. Unlike a `request_action`
caller, it holds no key to find the first request by. Options: take an optional caller key from the
call's `_meta`, or answer a repeat of the same caller, Action and arguments within a short window
with the first request, which would also absorb a deliberate identical repeat.
