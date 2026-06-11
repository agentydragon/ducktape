# FastMCP tool exceptions and error handling

## TL;DR

- Don't blanket-catch exceptions inside tools — let FastMCP surface unexpected failures as MCP errors.
- Use `ToolError` for expected failures (normal control flow: file not found, validation error, etc.)
- Let unexpected exceptions bubble (system errors, bugs) — FastMCP will handle them.
- Prefer strict, typed inputs (`OpenAIStrictModeBaseModel`) so FastMCP returns clear validation errors automatically.

## How FastMCP surfaces errors

- Tool exceptions are caught and returned as MCP errors (isError=true). The server stays healthy; inspect server logs for details.
- Successful calls return structured content (when you return Pydantic models) or primitives.
- For error results, the error message is visible to the LLM in the tool response.

## Best practices

- **Validation first:**
  - Use `OpenAIStrictModeBaseModel` for all tool inputs (auto-validates at class definition)
  - FastMCP handles validation errors automatically
- **Expected failures (normal control flow):**
  - Raise `ToolError` with clear, actionable messages for the LLM
  - Examples: "File not found", "Server not running", "Invalid format"
- **Unexpected failures (bugs, system errors):**
  - Let them bubble — don't catch broadly
  - FastMCP will surface them as errors with stack traces in logs
- **Keep tool bodies simple:**
  - Validate inputs via Pydantic models
  - Raise ToolError for expected error cases
  - Let unexpected exceptions propagate

## Error handling pattern (raise ToolError)

Use `ToolError` for all expected failures:

```python
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool
from pydantic import BaseModel
from mcp_infra.enhanced import EnhancedFastMCP
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

class FetchInput(OpenAIStrictModeBaseModel):
    """Input for fetch tool."""
    url: str
    timeout_secs: int | None = 5

class FetchResult(BaseModel):
    """Successful fetch result."""
    status: int
    content: str
    content_length: int

class FetchServer(EnhancedFastMCP):
    """Fetcher MCP server."""

    fetch_tool: FunctionTool

    def __init__(self):
        super().__init__("Fetcher", instructions="Fetch URLs...")

        def fetch(input: FetchInput) -> FetchResult:
            """Fetch a URL and return its contents."""
            try:
                resp = http_get(input.url, timeout=input.timeout_secs or 5)
            except TimeoutError:
                # Expected failure: timeout
                raise ToolError(f"Request timed out after {input.timeout_secs}s")
            except ConnectionError as e:
                # Expected failure: network error
                raise ToolError(f"Connection failed: {e}")

            if resp.status != 200:
                # Expected failure: bad status code
                raise ToolError(f"HTTP {resp.status}: {resp.reason}")

            # Success: return structured result
            return FetchResult(
                status=resp.status,
                content=resp.text,
                content_length=len(resp.content)
            )

        self.fetch_tool = self.flat_model()(fetch)
```

**Key points:**

- `ToolError` takes a single string message (visible to the LLM)
- No `code` or `details` parameters (just use a clear message)
- Message should be actionable: "File not found: /path/to/file" (not just "error")

## What NOT to do

**Don't use discriminated unions for OK/ERR flows:**

```python
# ❌ BAD: Don't do this
class Success(BaseModel):
    kind: Literal["Success"]
    result: str

class Failure(BaseModel):
    kind: Literal["Failure"]
    message: str

Result = Annotated[Success | Failure, Field(discriminator="kind")]

def my_tool(input: MyInput) -> Result:
    if something_wrong:
        return Failure(kind="Failure", message="error")  # ❌ Wrong
    return Success(kind="Success", result="ok")
```

**Instead, use ToolError:**

```python
# ✅ GOOD: Use ToolError for failures
def my_tool(input: MyInput) -> str:
    if something_wrong:
        raise ToolError("Clear error message for the LLM")  # ✅ Right
    return "ok"
```

**Don't blanket-catch exceptions:**

```python
# ❌ BAD: Swallows bugs
def my_tool(input: MyInput) -> str:
    try:
        return do_work(input)
    except Exception:  # ❌ Too broad
        return "error"

# ✅ GOOD: Catch specific expected errors, let bugs bubble
def my_tool(input: MyInput) -> str:
    try:
        return do_work(input)
    except FileNotFoundError as e:
        raise ToolError(f"File not found: {e.filename}")
    # Other exceptions bubble up as unexpected errors
```

## Client behavior

- Default: `client.call_tool(...)` raises `ToolError` on tool failure
- With `raise_on_error=False` you receive a result object where `result.is_error` is True

## Lifespan errors

- Errors in lifespan (startup/shutdown) prevent successful initialize
- Clients see initialize failure; diagnose via server logs

## References

- Tools — validation, structured output, error handling: <https://gofastmcp.com/servers/tools>
- Clients — tool success/error envelopes: <https://gofastmcp.com/clients/tools>
- Exceptions reference: <https://gofastmcp.com/python-sdk/fastmcp-exceptions>
- Server settings/logging: <https://gofastmcp.com/servers/server>
