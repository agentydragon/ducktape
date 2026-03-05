@README.md

# approval_gate — Agent Instructions

## Conventions

- State transitions are append-only: once an action leaves `pending`, it cannot go back.
- `session_key` is injected by the OpenClaw plugin; agents must not set it manually.
- `justification` is required; operators see it when deciding whether to approve.
- The `resource://actions/{id}` MCP resource is the source of truth for action state.
- Tool call results return a full `Action` object with `state.status` indicating whether
  the action resolved (`done`, `rejected`) or is still in flight (`pending`, `executing`).
  With `default_approval_timeout_seconds`, resolved actions include the result directly.
