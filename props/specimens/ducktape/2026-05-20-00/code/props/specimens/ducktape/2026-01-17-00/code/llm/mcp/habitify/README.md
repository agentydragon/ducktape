# Habitify MCP Server

A Model Context Protocol (MCP) server that provides Claude Desktop with access to the Habitify habit tracking API. This allows you to manage your habits through natural conversation with Claude.

## Current State

The server implements a subset of the Habitify API endpoints:

- ✅ Get all habits
- ✅ Get habit details by ID
- ✅ Check habit status for specific dates
- ✅ Set habit status (completed, skipped, failed, none)
- ✅ Get areas (categories)
- ✅ Get journal (daily habit view)
- ❌ Create/update/delete habits (not implemented)
- ❌ Habit notes/logs management (not implemented)

See [Habitify API documentation](https://docs.habitify.me/) for full API capabilities.

## Installation

### Quick Start

1. Install the package:

   ```bash
   pip install -e .
   ```

2. Set your Habitify API key (get it from Habitify app settings):

   ```bash
   export HABITIFY_API_KEY=your_api_key_here
   ```

3. Install to Claude Desktop:

   ```bash
   habitify install
   ```

### Manual Configuration

If you prefer manual setup, add to your Claude Desktop config:

```json
{
  "mcpServers": {
    "habitify": {
      "command": "habitify",
      "args": ["mcp"],
      "env": {
        "HABITIFY_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

## Usage Examples

### Using the Python Client Directly

```python
from habitify_mcp_server.habitify_client import HabitifyClient
import asyncio
from datetime import date, timedelta

async def main():
    async with HabitifyClient() as client:
        # Get all habits
        habits = await client.get_habits()
        for habit in habits:
            print(f"{habit.name}: {habit.id}")

        # Check habit status for today
        habit_id = habits[0].id
        status = await client.check_habit_status(habit_id)
        print(f"Status: {status.status}")

        # Mark habit as completed
        await client.set_habit_status(
            habit_id,
            status="completed",
            note="Completed via API!"
        )

        # Check habit status for date range
        start = date.today() - timedelta(days=7)
        statuses = await client.check_habit_status_range(
            habit_id,
            start_date=start,
            end_date=date.today()
        )
        for s in statuses:
            print(f"{s.date}: {s.status}")

asyncio.run(main())
```

### Conversation Examples with Claude

Once installed, you can ask Claude:

- "What habits do I have?"
- "Did I complete my meditation habit today?"
- "Mark my exercise habit as completed for today"
- "Show me my habit completion for the last week"
- "Skip my reading habit for today with note 'traveling'"

### Command Line Usage

```bash
# Run server with stdio transport (for Claude Desktop)
habitify mcp

# Run with SSE transport on custom port
habitify mcp --transport=sse --port=8080

# Test the API connection
habitify test

# View help
habitify --help
```

## Development

### Project Structure

```
habitify_mcp_server/
├── __init__.py          # Package exports
├── cli.py               # Command-line interface
├── server.py            # MCP server implementation
├── habitify_client.py   # Habitify API client
├── types.py             # Pydantic models
└── utils/
    └── date_utils.py    # Date handling utilities
```

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=habitify_mcp_server

# Run specific test file
pytest tests/test_client.py
```

### API Reference Collection

The `habitify_api_reference/` directory contains YAML files with real API request/response examples:

```bash
# Regenerate reference examples (requires API key)
cd habitify_api_reference
python collect_references.py
```

## Environment Variables

- `HABITIFY_API_KEY` - Your Habitify API key (required)
- `HABITIFY_API_BASE_URL` - API base URL (default: <https://api.habitify.me>)

## Security Notes

- Never commit API keys to version control
- The `.env` file is gitignored for safety
- API keys provide full account access - keep them secure

## License

MIT
