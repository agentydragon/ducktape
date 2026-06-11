# agent-server

Agent server - FastAPI backend, runtime, and MCP infrastructure for LLM agents.

## Components

- **server/** - FastAPI application, WebSocket endpoints, state management
- **persist/** - Persistence layer (SQLite for conversation history)
- **runtime/** - Container lifecycle management, agent registry
- **mcp_bridge/** - Two-compositor architecture for agent-MCP integration
- **notifications/** - Notification batching and handling
- **policies/** - Policy types and presets
- **policy_eval/** - Container-based policy execution
- **mcp/** - Agent-specific MCP servers:
  - `ui/` - UI interaction (send_message, end_turn)
  - `approval_policy/` - Policy engine, readers, proposers
  - `notifications/` - Notifications buffer
  - `chat/` - Inter-agent messaging
  - `loop/` - Loop control
  - `runtime/` - Runtime server

## Development

See [AGENTS.md](../AGENTS.md) for standard Bazel workflow.
