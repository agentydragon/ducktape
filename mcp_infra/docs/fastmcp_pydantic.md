# FastMCP tools with Pydantic

Use FastMCP's native function signature as the tool input schema. Declare one
typed parameter per argument, and use `Annotated` with `Field` for parameter
descriptions and validation:

```python
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from pydantic import BaseModel, Field

from util.exec.models import TimeoutMs


class LookupResult(BaseModel):
    value: str


class LookupServer(FastMCP):
    lookup_tool: FunctionTool

    def __init__(self) -> None:
        super().__init__("Lookup")

        def lookup(
            key: Annotated[str, Field(description="Identifier to look up")],
            timeout_ms: TimeoutMs,
        ) -> LookupResult:
            """Look up a value by identifier."""
            return LookupResult(value=key)

        self.lookup_tool = self.tool(output_schema=None)(lookup)
```

FastMCP publishes the schema generated from the function signature directly.
Tools passed to the Agent's OpenAI Responses API must meet its strict-schema
requirements: every argument must be required, and every object must forbid
additional properties. For nullable arguments, use `T | None` without a Python
default so the argument remains required while accepting `null`. Avoid defaulted
arguments on Agent tools; their FastMCP schema marks them optional and the Agent
rejects that schema when building the request.

## Inputs

- Prefer native function parameters over a single Pydantic model parameter;
  the latter produces a nested `input` object in the MCP schema.
- Use `Annotated[T, Field(...)]` for constraints and LLM-facing descriptions.
- Use nested `BaseModel` arguments with `ConfigDict(extra="forbid")`, and make
  each field required when the tool is sent through the Agent.
- Use `list[T]` rather than `set[T]`, and `str` rather than `Path` in tool
  schemas.
- FastMCP injects `Context` parameters and `Depends(...)` values without
  exposing them as tool arguments.

## Outputs

FastMCP infers structured output schemas from return annotations by default.
Pass `output_schema=None` when the tool returns MCP content blocks or when the
existing tool contract intentionally has no structured output schema. Use a
Pydantic `BaseModel` return type for structured data.

## Typed clients and display

For tests, `TypedClient.from_server` and
`mcp_infra.testing.tool_schemas.extract_tool_input_schemas` derive Pydantic input
models from each native `FunctionTool` signature. This keeps typed test clients
and rich input rendering available without a custom FastMCP `Tool` subclass.
