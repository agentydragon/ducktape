# FastMCP tools with Pydantic

The tool's native function signature is its input schema:

```python
def lookup(
    key: Annotated[str, Field(description="Identifier to look up")],
    timeout_ms: TimeoutMs,  # util.exec.models
) -> LookupResult:
    """Look up a value by identifier."""
    return LookupResult(value=key)

self.lookup_tool = self.tool(output_schema=None)(lookup)  # inside a FastMCP subclass __init__
```

- One typed parameter per argument, `Annotated[T, Field(...)]` for constraints and
  descriptions — not a single model parameter (that nests everything under `input`).
- **Agent tools (OpenAI Responses API strict schema)**: every argument required and
  every object forbids extra properties. Nullable → `T | None` with **no** default; a
  defaulted argument becomes optional and the Agent rejects the schema. Nested
  `BaseModel` args use `ConfigDict(extra="forbid")` with all fields required.
- `list[T]` not `set[T]`, `str` not `Path` in tool schemas.
- `Context` and `Depends(...)` parameters are injected, not exposed.
- Return a `BaseModel` for structured output; `output_schema=None` when returning MCP
  content blocks or when the contract intentionally has no structured schema.
- Tests: `TypedClient.from_server` and
  `mcp_infra.testing.tool_schemas.extract_tool_input_schemas` derive Pydantic input
  models from the `FunctionTool` signature — no custom `Tool` subclass needed.
