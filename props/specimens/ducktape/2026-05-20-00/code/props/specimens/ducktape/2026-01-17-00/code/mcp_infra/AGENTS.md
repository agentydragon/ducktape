@README.md

# Agent Guide for `mcp_infra/`

@docs/compositor.md

## MCP Conventions

- MCP naming
  - When composing MCP tool names programmatically, use `build_mcp_function(server, tool)` from `mcp_infra.naming`.
  - Avoid hard-coded strings like `server_tool` in code. Literal forms in docs/examples are illustrative only.
- FastMCP error handling
  - Do not wrap tool bodies in broad try/except. Uncaught exceptions become MCP errors (`isError=true`) with messages.
  - Prefer Pydantic models for inputs/outputs; validation errors surface as MCP errors automatically.
- MCP CallToolResult handling
  - FastMCP client returns `fastmcp.client.client.CallToolResult` (snake_case fields: `is_error`,
    `structured_content`). MCP Pydantic uses `mcp.types.CallToolResult` (camelCase aliases:
    `isError`, `structuredContent`). Use the appropriate type at each layer.
- Typing discipline
  - Handle exact runtime types. When an external API returns a loose object, convert it at the
    boundary so the rest of the code sees a single concrete type.
  - Centralize boundary conversions (e.g., `_normalize_result`/`_call_structured`) instead of
    duplicating `isinstance` + conversion logic.
- MCP servers with agent‑specific state
  - Prefer constructors that accept per‑agent state (no hidden globals/singletons)
  - In‑proc servers are mounted on a `Compositor` (via `mount_inproc(...)`)

### CallToolResult Conventions

- FastMCP client returns a lightweight `CallToolResult` dataclass (not a Pydantic model) with
  snake_case fields (`is_error`, `structured_content`).
- Pydantic MCP types live under `mcp.types` (e.g., `mcp.types.CallToolResult`) with camelCase
  aliases (`isError`, `structuredContent`). Use these when you need typed validation/serialization.
- Convert between types at boundaries as needed. For simple cases, construct
  `mcp.types.CallToolResult(content=..., structuredContent=..., isError=...)` directly.
- Do not call `.model_dump()` on FastMCP's client `CallToolResult` — it isn't a Pydantic model.

@docs/fastmcp_pydantic.md
@docs/fastmcp_exceptions.md

## Development

Part of the ducktape uv workspace. See root `AGENTS.md` for workspace setup.
