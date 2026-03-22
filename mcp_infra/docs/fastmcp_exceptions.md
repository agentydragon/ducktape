# FastMCP Tool Error Handling

## Rules

- **Expected failures**: raise `ToolError("actionable message for LLM")` from `fastmcp.exceptions`
- **Unexpected failures**: let them bubble. FastMCP catches and returns as MCP errors (`isError=true`). Server stays healthy.
- **No blanket try/except** in tool bodies
- **No discriminated unions for OK/ERR** -- use `ToolError` for errors, return value for success

```python
from fastmcp.exceptions import ToolError

def fetch(input: FetchInput) -> FetchResult:
    try:
        resp = http_get(input.url, timeout=input.timeout_secs or 5)
    except TimeoutError:
        raise ToolError(f"Request timed out after {input.timeout_secs}s")
    if resp.status != 200:
        raise ToolError(f"HTTP {resp.status}: {resp.reason}")
    return FetchResult(status=resp.status, content=resp.text)
```

## Client Behavior

- Default: `client.call_tool(...)` raises `ToolError` on failure
- `raise_on_error=False`: returns result object where `result.is_error` is True

## References

- <https://gofastmcp.com/servers/tools>
- <https://gofastmcp.com/clients/tools>
- <https://gofastmcp.com/python-sdk/fastmcp-exceptions>
