# MCP Tool Content Blocks - Simplifying Resources Server

## TL;DR - The Key Discovery

**MCP has a built-in `EmbeddedResource` type that bundles content with its URI semantically!**

Instead of custom JSON wrappers, you can return:

```python
mcp_types.EmbeddedResource(
    type="resource",
    resource=mcp_types.TextResourceContents(
        uri="file:///workspace/src/server.py",
        mimeType="text/x-python",
        text="content here..."
    )
)
```

This is **exactly** what the resources server needs if we remove windowing:

- ✅ URI preserved semantically
- ✅ MimeType included
- ✅ Standard MCP type (clients handle it natively)
- ✅ Zero conversion (compositor already returns TextResourceContents/BlobResourceContents with URIs)

**Implementation becomes trivial:** Just wrap compositor output in `EmbeddedResource` - that's it!

## The Broader Insight

MCP tools can return **content blocks** (TextContent, ImageContent, EmbeddedResource, etc.) directly in their results, not just structured JSON. This allows tools to return rich, multimodal data without custom wrapper types.

## Current Approach (With Windowing)

The resources server currently returns a custom Pydantic model because it implements windowing/truncation:

```python
# Current: Custom wrapper for windowed reads
class WindowedTextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str
    mime: str | None = None
    total_bytes: int
    bytes_returned: int

class ResourceReadResult(BaseModel):
    window: ResourceWindowInfo
    parts: list[WindowedPart]  # WindowedTextPart | WindowedBlobPart
    total_parts: int

@mcp.flat_model()
async def read(input: ResourcesReadArgs) -> ResourceReadResult:
    # ... windowing logic ...
    return ResourceReadResult(...)
```

**Why it needs a custom type:**

- Windowing metadata (start_offset, max_bytes, bytes_returned)
- Need to track total_bytes vs bytes_returned for pagination
- Custom JSON structure for client parsing

## Simplified Approach (No Windowing)

### Option 1: Return EmbeddedResource (Preserves URI Semantically)

**This is the cleanest approach** - use MCP's `EmbeddedResource` type which bundles content with its URI:

```python
from mcp import types as mcp_types

@mcp.tool()
async def read_semantic(server: str, uri: str) -> list[mcp_types.EmbeddedResource]:
    """Read a resource and return it as an embedded resource with URI attached.

    Returns EmbeddedResource content blocks that preserve the semantic
    connection between content and its source URI.
    """
    prefixed = add_resource_prefix(uri, server, compositor.resource_prefix_format)
    uri_value = ANY_URL.validate_python(prefixed)

    # Get raw MCP resource contents from compositor
    # These already have URIs attached: TextResourceContents | BlobResourceContents
    contents = await compositor.read_resource_contents(uri_value)

    # Wrap each in EmbeddedResource - preserves URI semantically!
    return [
        mcp_types.EmbeddedResource(
            type="resource",
            resource=part  # part is TextResourceContents or BlobResourceContents with uri field
        )
        for part in contents
    ]
```

**Key Benefits:**

- ✅ URI is preserved in `resource.uri` field (semantic linkage)
- ✅ MimeType preserved in `resource.mimeType`
- ✅ Standard MCP type - clients know how to handle it
- ✅ Can be rendered with context about the source
- ✅ Zero conversion needed - just wrap the compositor output!

### Option 2: Return Plain Content Blocks (Simpler, Loses URI)

If you don't need the URI in the result (e.g., tool args already specify it):

```python
from mcp import types as mcp_types

@mcp.tool()
async def read_simple(server: str, uri: str) -> list[mcp_types.TextContent | mcp_types.ImageContent]:
    """Read a resource and return its content blocks directly.

    No windowing - returns the full resource content as MCP content blocks.
    URI is not preserved in the response (caller already knows it from args).
    """
    prefixed = add_resource_prefix(uri, server, compositor.resource_prefix_format)
    uri_value = ANY_URL.validate_python(prefixed)

    # Get raw MCP resource contents from compositor
    contents = await compositor.read_resource_contents(uri_value)

    # Convert to tool result content blocks
    result_blocks: list[mcp_types.TextContent | mcp_types.ImageContent] = []

    for part in contents:
        if isinstance(part, mcp_types.TextResourceContents):
            result_blocks.append(
                mcp_types.TextContent(
                    type="text",
                    text=part.text,
                    # Note: URI and mimeType are lost here
                )
            )
        elif isinstance(part, mcp_types.BlobResourceContents):
            # Blob data is base64-encoded per MCP spec
            result_blocks.append(
                mcp_types.ImageContent(
                    type="image",
                    data=part.blob,  # base64 string
                    mimeType=part.mimeType or "application/octet-stream",
                )
            )

    return result_blocks
```

## What Changes? Comparing All Three Approaches

### Approach A: Custom Type (Current - With Windowing)

```python
# Client receives:
{
  "window": {"start_offset": 0, "max_bytes": 0},
  "parts": [
    {
      "kind": "text",
      "text": "file contents...",
      "mime": "text/plain",
      "total_bytes": 1000,
      "bytes_returned": 1000
    }
  ],
  "total_parts": 1
}
```

**Characteristics:**

- ✅ Supports windowing/pagination
- ✅ Metadata rich (bytes, offsets)
- ❌ Custom JSON structure
- ❌ Client needs custom parsing
- ❌ Loses semantic URI linkage

### Approach B: EmbeddedResource (Best for No-Windowing Case)

```python
# Client receives CallToolResult:
{
  "content": [
    {
      "type": "resource",
      "resource": {
        "uri": "file:///workspace/src/server.py",
        "mimeType": "text/x-python",
        "text": "file contents..."
      }
    }
  ],
  "isError": false
}
```

**Characteristics:**

- ✅ Standard MCP type
- ✅ URI semantically linked to content
- ✅ MimeType preserved
- ✅ Zero conversion (just wrap compositor output)
- ✅ Clients can render with source context
- ❌ No windowing support

### Approach C: Plain Content Blocks (Simplest)

```python
# Client receives CallToolResult:
{
  "content": [
    {
      "type": "text",
      "text": "file contents..."
    }
  ],
  "isError": false
}
```

**Characteristics:**

- ✅ Simplest structure
- ✅ Standard MCP type
- ✅ Easy client rendering
- ❌ No URI linkage
- ❌ No mimeType
- ❌ No windowing support

## Benefits of MCP Content Blocks

1. **Standard Format**: MCP clients already know how to handle TextContent, ImageContent, etc.
2. **Multimodal**: Can mix text, images, audio in one response
3. **Less Code**: No custom Pydantic models, windowing logic, or client parsing
4. **Better UX**: IDEs/UIs can render content blocks natively (syntax highlighting, images, etc.)

## When to Use Each Approach

### Use Custom Types (Current Approach) When

- You need windowing/pagination for large resources
- You need custom metadata (total_bytes, window info)
- You want precise control over JSON structure

### Use Content Blocks (Simplified) When

- Resources fit in memory/response size limits
- You want standard MCP client rendering
- You want multimodal responses (text + images)
- Simpler code is more important than truncation

## Real-World Example: Ultra-Simple Resources Server

Here's how the resources server would look if we eliminated windowing and used `EmbeddedResource`:

```python
from mcp import types as mcp_types
from mcp_infra.compositor.server import Compositor
from mcp_infra.notifying_fastmcp import NotifyingFastMCP

def make_simple_resources_server(compositor: Compositor) -> NotifyingFastMCP:
    """Dead-simple resources server using MCP content blocks."""
    mcp = NotifyingFastMCP("resources")

    @mcp.tool()
    async def read(server: str, uri: str) -> list[mcp_types.EmbeddedResource]:
        """Read a resource with URI preserved semantically.

        Returns the resource contents as EmbeddedResource blocks,
        preserving the URI and mimeType for each part.
        """
        # Add server prefix to URI
        prefixed = add_resource_prefix(uri, server, compositor.resource_prefix_format)
        uri_value = ANY_URL.validate_python(prefixed)

        # Get resource contents (already have uri + mimeType + text/blob)
        contents = await compositor.read_resource_contents(uri_value)

        # Wrap in EmbeddedResource - that's it!
        return [
            mcp_types.EmbeddedResource(type="resource", resource=part)
            for part in contents
        ]

    return mcp
```

**That's the entire implementation!** Compare to the current 500+ line version with:

- Custom `WindowedPart` types
- Windowing logic (`_iter_window_parts`, `_build_window_payload`)
- Offset/bytes tracking
- Part normalization

## Other Examples: Tools Using Content Blocks

```python
# Search tool returning matches with URIs
@mcp.tool()
async def grep(pattern: str) -> list[mcp_types.EmbeddedResource]:
    """Search for pattern across files."""
    matches = await run_grep(pattern)

    # Return each match as an embedded resource
    return [
        mcp_types.EmbeddedResource(
            type="resource",
            resource=mcp_types.TextResourceContents(
                uri=f"file://{match.filename}",
                text=f"{match.line_number}: {match.line}",
                mimeType="text/plain"
            )
        )
        for match in matches
    ]

# Screenshot tool returning image content
@mcp.tool()
async def screenshot() -> mcp_types.ImageContent:
    """Take a screenshot."""
    image_data = await capture_screenshot()
    return mcp_types.ImageContent(
        type="image",
        data=base64.b64encode(image_data).decode(),
        mimeType="image/png"
    )

# Multi-part response with different content types
@mcp.tool()
async def analyze_page(url: str) -> list[mcp_types.TextContent | mcp_types.ImageContent]:
    """Analyze a web page, returning text summary and screenshot."""
    html = await fetch_page(url)
    screenshot = await capture_screenshot(url)
    summary = await analyze_html(html)

    return [
        mcp_types.TextContent(type="text", text=summary),
        mcp_types.ImageContent(
            type="image",
            data=base64.b64encode(screenshot).decode(),
            mimeType="image/png"
        )
    ]
```

## Migration Path for Resources Server

### Option 1: Feature Flag (Gradual Migration)

Add a flag to enable content blocks for clients that support it:

```python
class ResourcesReadArgs(BaseModel):
    server: str
    uri: str
    use_content_blocks: bool = Field(
        default=False,
        description="Return EmbeddedResource blocks instead of custom JSON (no windowing)"
    )
    start_offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=0, ge=0)

@mcp.flat_model()
async def read(
    input: ResourcesReadArgs
) -> ResourceReadResult | list[mcp_types.EmbeddedResource]:
    prefixed = add_resource_prefix(input.uri, input.server, compositor.resource_prefix_format)
    uri_value = ANY_URL.validate_python(prefixed)
    contents = await compositor.read_resource_contents(uri_value)

    if input.use_content_blocks:
        # Simple path: return EmbeddedResource blocks
        return [mcp_types.EmbeddedResource(type="resource", resource=part) for part in contents]
    else:
        # Legacy path: windowed custom JSON
        return _build_window_payload(contents, input.start_offset, ...)
```

**Migration steps:**

1. Add `use_content_blocks` flag (defaults to False)
2. Clients opt-in to new behavior
3. Monitor adoption
4. Eventually flip default or remove legacy path

### Option 2: Separate Tool (Clean Separation)

Keep both tools for different use cases:

```python
@mcp.flat_model()
async def read(input: ResourcesReadArgs) -> ResourceReadResult:
    """Read with windowing support (for large resources)."""
    # Current implementation unchanged
    ...

@mcp.tool()
async def read_embedded(server: str, uri: str) -> list[mcp_types.EmbeddedResource]:
    """Read full resource as EmbeddedResource blocks (no windowing).

    Best for: Small to medium resources where you want URI preservation
    and standard MCP client rendering.
    """
    prefixed = add_resource_prefix(uri, server, compositor.resource_prefix_format)
    uri_value = ANY_URL.validate_python(prefixed)
    contents = await compositor.read_resource_contents(uri_value)
    return [mcp_types.EmbeddedResource(type="resource", resource=part) for part in contents]
```

**Advantages:**

- No breaking changes
- Clear separation of concerns
- Users choose based on use case
- Both can coexist indefinitely

### Option 3: Replace Entirely (Breaking Change)

If windowing isn't actually used in practice:

```python
# Remove: ResourceReadResult, WindowedPart, windowing logic
# Replace with:

@mcp.tool()
async def read(server: str, uri: str) -> list[mcp_types.EmbeddedResource]:
    """Read a resource (full content, no windowing)."""
    prefixed = add_resource_prefix(uri, server, compositor.resource_prefix_format)
    uri_value = ANY_URL.validate_python(prefixed)
    contents = await compositor.read_resource_contents(uri_value)
    return [mcp_types.EmbeddedResource(type="resource", resource=part) for part in contents]
```

**Prerequisite analysis:**

- Are any clients actually using windowing?
- What's the largest resource size we encounter?
- Can we handle all resources in-memory?

## Next Steps

1. **Experiment**: Try `read_embedded` in a branch and test with real resources
2. **Measure**: Profile memory usage with large resources
3. **Validate**: Ensure Agent UI and other clients render EmbeddedResource correctly
4. **Decide**: Based on results, choose migration option
5. **Document**: Update client code to use the simpler interface

## References

- **MCP Spec - Content Types**: <https://spec.modelcontextprotocol.io/specification/2024-11-05/basic/types/#content-types>
  - `TextContent`, `ImageContent`, `AudioContent`, `EmbeddedResource`
- **MCP Spec - Resource Contents**: <https://spec.modelcontextprotocol.io/specification/2024-11-05/server/resources/#resource-contents>
  - `TextResourceContents`, `BlobResourceContents` (both have `uri` field)
- **FastMCP Tool Returns**: Tools can return Pydantic models, dicts, lists, or raw MCP types
- **Tool Results**: `CallToolResult.content` is a list of content blocks
