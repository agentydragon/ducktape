# Habitify MCP Server

MCP server providing Claude Desktop access to the [Habitify API](https://docs.habitify.me/).

## Implemented Endpoints

- Get all habits, habit details, areas, journal
- Check/set habit status (completed, skipped, failed, none)
- Not implemented: create/update/delete habits, notes/logs management

## Setup

```bash
export HABITIFY_API_KEY=your_key  # From Habitify app settings
bb run //x/habitify_mcp:habitify_cli -- mcp
```

CLI: `bb run //x/habitify_mcp:habitify_cli -- <cmd>` with `mcp` (stdio) or `mcp --transport=sse --port=8080`.

Claude Desktop registration needs a server launcher that survives Bazel cleanup. The experimental
`install` command writes a Python MCP SDK entry. That SDK rejects the Habitify FastMCP factory at startup.
See the [local server configuration guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
for the Desktop launch contract.

## Environment Variables

- `HABITIFY_API_KEY` - required
- `HABITIFY_API_BASE_URL` - default: `https://api.habitify.me`

## Development

Launch [MCP Inspector](https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector)
with the Habitify stdio server. Install Node.js with `npx` using a version supported by Inspector.

```bash
bb run //x/habitify_mcp:mcp_dev_runner
```

Set `HABITIFY_API_KEY` in the environment or pass `--api-key` after `--`.
Pass `--debug` for verbose server logs. The runner removes its private session config when Inspector exits.

API reference captures: [`api_reference/`](api_reference/README.md). Read its collector warning before regenerating them.
