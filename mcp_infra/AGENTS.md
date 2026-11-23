@README.md

## MCP conventions

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
- **Typed tool access**: `@mcp.tool` returns the registered callable. Keep it on an
  annotated server attribute, and keep tool names and resource URIs as class constants,
  as `ContainerExecServer` does (`exec_tool`, `EXEC_TOOL_NAME`, `CONTAINER_INFO_URI`).
  No custom FastMCP base class.
- **Cleanup that must survive a client disconnect**: a lifespan that owns a container,
  subprocess or lease cannot assume an uncancelled teardown. Disconnecting an in-process
  FastMCP client once cancelled the enclosing AnyIO scope (seen in the retired
  `x/agent_server`), so the first `await` in `finally` raised `CancelledError`.
  `asyncio.shield` does not stop an AnyIO scope. Clean up under
  `anyio.CancelScope(shield=True)` (`scoped_container`) or from a caller that owns the
  resource, never in a fire-and-forget task. Test it by disconnecting the client and
  asserting the resource is gone. Re-verify after FastMCP or AnyIO upgrades.

- For tool parameter and result schemas, see [FastMCP with Pydantic](docs/fastmcp_pydantic.md).
- For tool error handling and client results, see [FastMCP tool errors](docs/fastmcp_exceptions.md).
