# FastMCP tool errors

- Expected failures: `raise ToolError("actionable message for the LLM")`
  (`fastmcp.exceptions`).
- Unexpected failures propagate; FastMCP returns them as MCP errors (`isError=true`)
  and the server stays healthy. No blanket `try/except` in tool bodies.
- No OK/ERR discriminated-union results: `ToolError` for errors, the return value
  for success.
- Client side, `call_tool(...)` raises `ToolError`; with `raise_on_error=False` check
  `result.is_error`.
