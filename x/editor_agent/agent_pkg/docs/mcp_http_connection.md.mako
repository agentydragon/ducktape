# MCP Server Connection via HTTP

An MCP server is available via Streamable HTTP transport. Connection environment variables:
- `MCP_SERVER_URL`: HTTP endpoint URL for the MCP server
- `MCP_SERVER_TOKEN`: Bearer token for authentication

## Example: Discovering Tools

${include_doc("x/editor_agent/agent_pkg/examples/mcp_discover.py", raw=True)}

## Server Info

${run_command("python -m x.editor_agent.agent_pkg.examples.mcp_discover")}
