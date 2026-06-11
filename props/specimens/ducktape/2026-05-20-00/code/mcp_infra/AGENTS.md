@README.md

# Agent Guide for `mcp_infra/`

@docs/compositor.md

## MCP Conventions

- **Naming**: use `build_mcp_function(server, tool)` from `mcp_infra.naming`. No hard-coded `server_tool` strings.
- **Error handling**: do not wrap tool bodies in broad try/except. Uncaught exceptions become MCP errors (`isError=true`). Prefer Pydantic models for inputs/outputs.
- **Typing**: convert loose external API objects at the boundary so internal code sees a single concrete type. Centralize boundary conversions.
- **Server state**: constructors accept per-agent state (no globals/singletons). In-proc servers mount on a `Compositor` via `mount_inproc(...)`.

### CallToolResult Conventions

- FastMCP client returns a lightweight `CallToolResult` dataclass (not Pydantic) with snake_case fields (`is_error`, `structured_content`). Do not call `.model_dump()` on it.
- `mcp.types.CallToolResult` (Pydantic) uses camelCase aliases (`isError`, `structuredContent`). Use for typed validation/serialization.
- Convert between types at boundaries as needed.

@docs/fastmcp_pydantic.md
@docs/fastmcp_exceptions.md
