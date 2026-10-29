@README.md

## MCP conventions

- Tests constructing namespaced tool names use `build_mcp_function(server, tool)`
  from `mcp_infra.testing.naming`, not hard-coded `server_tool` strings.
- Server constructors take per-agent state; no globals/singletons, mutable state
  scoped to the server instance.
- Many clients never show server `instructions` to the model: anything needed to call
  a tool correctly goes in tool/field descriptions too (intentional redundancy, not
  drift).
- Non-mutating tools set `annotations=ToolAnnotations(readOnlyHint=True)` so
  claude.ai / Claude Code skip per-call approvals: <docs/tool_annotations.md>.
- **`CallToolResult` gotcha**: FastMCP's client returns a plain dataclass with
  snake_case fields (`is_error`, `structured_content`) — no `.model_dump()`;
  `mcp.types.CallToolResult` is Pydantic with camelCase aliases (`isError`,
  `structuredContent`). Convert at boundaries.

- For tool parameter and result schemas, see [FastMCP with Pydantic](docs/fastmcp_pydantic.md).
- For tool error handling and client results, see [FastMCP tool errors](docs/fastmcp_exceptions.md).
