# Habitify MCP Server

MCP server providing Claude Desktop access to the [Habitify API](https://docs.habitify.me/).

## Implemented Endpoints

- Get all habits, habit details, areas, journal
- Check/set habit status (completed, skipped, failed, none)
- Not implemented: create/update/delete habits, notes/logs management

## Setup

```bash
export HABITIFY_API_KEY=your_key  # From Habitify app settings
bb run //x/habitify_mcp:habitify_cli -- install  # Installs to Claude Desktop
```

CLI: `bb run //x/habitify_mcp:habitify_cli -- <cmd>` with `mcp` (stdio) or `mcp --transport=sse --port=8080`.

## Environment Variables

- `HABITIFY_API_KEY` - required
- `HABITIFY_API_BASE_URL` - default: `https://api.habitify.me`

## Development

API reference captures: [`api_reference/`](api_reference/README.md). Read its collector warning before regenerating them.
