# FastMCP tools with Pydantic

Use FastMCP's native function signature as the tool input schema. Declare one
typed parameter per argument, and use `Annotated` with `Field` for parameter
descriptions and validation:

```python
from typing import Annotated

from fastmcp.tools.function_tool import FunctionTool
from pydantic import BaseModel, Field

from mcp_infra.enhanced.server import EnhancedFastMCP
from util.exec.models import TimeoutMs


class LookupResult(BaseModel):
    value: str


class LookupServer(EnhancedFastMCP):
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

`EnhancedFastMCP` validates registered function schemas for OpenAI strict mode.
It rebuilds the input schema using the same Pydantic schema generator as the
rest of the repository, including nested models and discriminated unions. Every
exposed parameter is required in the published schema; a Python default still
applies when calling the handler directly. Use `T | None` when the client may
send `null`.

## Inputs

- Prefer native function parameters over a single Pydantic model parameter;
  the latter produces a nested `input` object in the MCP schema.
- Use `Annotated[T, Field(...)]` for constraints and LLM-facing descriptions.
- Use `OpenAIStrictModeBaseModel` for nested request models that need strict
  schema checks.
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

`TypedClient.from_server` and `extract_tool_input_schemas` derive Pydantic input
models from each native `FunctionTool` signature. This keeps typed test clients
and rich input rendering available without a custom FastMCP `Tool` subclass.
