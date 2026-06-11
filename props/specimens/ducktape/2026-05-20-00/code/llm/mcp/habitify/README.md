# Habitify MCP Server

MCP server providing Claude Desktop access to the [Habitify API](https://docs.habitify.me/).

## Implemented Endpoints

- Get all habits, habit details, areas, journal
- Check/set habit status (completed, skipped, failed, none)
- Not implemented: create/update/delete habits, notes/logs management

## Setup

```bash
pip install -e .
export HABITIFY_API_KEY=your_key  # From Habitify app settings
habitify install  # Installs to Claude Desktop
```

CLI: `habitify mcp` (stdio), `habitify mcp --transport=sse --port=8080`, `habitify test`.

## Environment Variables

- `HABITIFY_API_KEY` - required
- `HABITIFY_API_BASE_URL` - default: `https://api.habitify.me`

## Development

API reference examples in `habitify_api_reference/` (regenerate with `python collect_references.py`).
