# Scan: FastMCP Documentation Patterns

## Context

@../shared-context.md

## FastMCP Documentation References

FastMCP automatically generates JSON schemas from Pydantic models and exposes them to MCP clients.
Understanding how FastMCP works is essential for effective documentation.

**Official Resources:**

- [FastMCP Documentation](https://gofastmcp.com/)
- [FastMCP GitHub](https://github.com/jlowin/fastmcp)
- [Tools Documentation](https://gofastmcp.com/servers/tools) - Parameter metadata and Field usage
- [Complex Inputs Example](https://github.com/jlowin/fastmcp/blob/main/examples/complex_inputs.py) - Pydantic models with validation

**Key FastMCP Behaviors:**

- Generates input schemas from function signatures and type annotations
- Generates output schemas from return type annotations
- Exposes Pydantic `Field(description=...)` in JSON schemas sent to clients
- Supports both simple string descriptions (`Annotated[str, "description"]`) and full Field metadata

## Pattern 1: Missing Field Descriptions in Response Models

### Good Example: adgn/mcp/gitea_mirror/server.py

```python
from pydantic import BaseModel, Field

class GetRepoInfoResponse(BaseModel):
    """Repository information from Gitea API (matches GET /repos/{owner}/{repo} response).

    All fields from Gitea's Repository object are explicitly declared.
    Mirror-relevant fields have detailed descriptions.
    """
    # Core repository identity
    id: int
    name: str = Field(description="Repository name. Mirror path for cloning: '{owner}/{name}.git'")
    full_name: str = Field(description="Full repository name including owner (owner/name)")
    description: str
    empty: bool
    private: bool
    fork: bool
    template: bool

    # Mirror-specific fields (with detailed descriptions)
    mirror: bool = Field(description="True if this repository is a pull mirror")
    mirror_updated: str = Field(
        description="ISO 8601 timestamp of last mirror update. Poll this endpoint and compare timestamps to detect sync completion."
    )
    mirror_interval: str = Field(description="Mirror sync interval (e.g., '8h0m0s')")

    # Repository metadata
    size: int = Field(description="Repository size in KB")
    language: str
    languages_url: str
    default_branch: str = Field(description="Default branch name (e.g., 'main', 'master')")
    archived: bool

    # ... (additional fields: URLs, statistics, timestamps, features, merge settings)

    model_config = ConfigDict(extra="forbid")
```

### FastMCP's Approach (from official examples)

FastMCP documentation shows Field usage for **input parameters**:

```python
# From FastMCP tools documentation
from typing import Annotated
from pydantic import Field

@mcp.tool
def process_image(
    image_url: Annotated[str, Field(description="URL of the image to process")],
    width: Annotated[int, Field(description="Target width in pixels", ge=1, le=2000)] = 800,
) -> dict:
    """Process an image with optional resizing."""
    # Implementation...
```

The same principle applies to **response models**. When your tool returns a Pydantic model,
FastMCP generates an output schema from that model, including Field descriptions.

### Bad Example

```python
# BAD: No field descriptions - clients only see field names in schema
class MyToolResponse(BaseModel):
    result: str
    status: str
    timestamp: str
    model_config = ConfigDict(extra="forbid")
```

**Why it matters**:

- FastMCP automatically generates JSON schemas from Pydantic models
- Field descriptions are embedded in the JSON schema sent to MCP clients
- LLM agents use these descriptions to understand field purpose
- No ambiguity about field formats, usage patterns, or relationships

**Per FastMCP docs**: "FastMCP handles schema generation from type hints and docstrings."
Field descriptions extend this with field-level documentation.

**Detection**:

```bash
# Find response models without Field descriptions in MCP servers
rg --type py "class.*Response.*BaseModel" mcp_infra/ adgn/mcp/ -A10 | rg -v "Field\(description="
```

## Pattern 2: Redundant Schema Documentation in Docstrings

### Bad Example

```python
# BAD: Repeating schema in docstring
@server.flat_model()
def get_status(input: GetStatusArgs) -> GetStatusResponse:
    """Get current status.

    Returns:
        status: Current status string
        timestamp: ISO 8601 timestamp of last update
        is_ready: Boolean indicating readiness
    """
    ...
```

### Good Example

```python
# GOOD: Schema documented in Pydantic models, docstring focuses on behavior
@server.flat_model()
def get_status(input: GetStatusArgs) -> GetStatusResponse:
    """Get current status.

    Poll this endpoint to check when the system becomes ready.
    Compare timestamp with initial value to detect changes.
    """
    ...

class GetStatusResponse(BaseModel):
    status: str = Field(description="Current status string")
    timestamp: str = Field(description="ISO 8601 timestamp of last update")
    is_ready: bool = Field(description="Boolean indicating readiness")
```

**Why it matters**:

- FastMCP automatically exposes Pydantic schemas to clients
- Docstring duplication leads to drift when schemas change
- Field descriptions in Pydantic models are the single source of truth
- Docstrings should focus on usage patterns, not schema structure

**Detection**:

```bash
# Find docstrings with "Returns:" sections listing fields
rg --type py -A10 "@server\.(flat_model|tool)" | rg "Returns:"
```

## Pattern 3: Missing Context in Field Descriptions

### Bad Example

```python
# BAD: Descriptions are just rephrasing of field names
class SyncResponse(BaseModel):
    repo: str = Field(description="The repository")
    updated: str = Field(description="Updated timestamp")
```

### Good Example

```python
# GOOD: Descriptions include usage context and format details
class SyncResponse(BaseModel):
    repo: str = Field(description="Repository name (auto-generated from URL)")
    updated: str = Field(
        description="Timestamp BEFORE sync started. Poll get_status() until this changes to detect completion."
    )
```

**Why it matters**:

- Field names alone don't convey usage patterns
- LLM agents need context about how to use values (polling, format, relationships)
- Include formats (ISO 8601, URLs, paths) when relevant
- Explain relationships between fields when applicable

## Pattern 4: Missing Recommended Polling Guidance

### Bad Example

```python
# BAD: No guidance on polling patterns
@server.flat_model()
def get_status(input: GetStatusArgs) -> GetStatusResponse:
    """Get current status."""
    ...
```

### Good Example

```python
# GOOD: Includes polling recommendations
@server.flat_model()
def get_status(input: GetStatusArgs) -> GetStatusResponse:
    """Get current status.

    Use this to poll for completion after calling start_process().
    Compare the returned timestamp with the initial value.

    Recommended polling: Every 2-5 seconds until timestamp changes.
    """
    ...
```

**Why it matters**:

- LLM agents need guidance on polling intervals to avoid rate limiting
- Helps agents implement efficient retry logic
- Prevents excessive API calls
- Documents expected completion time ranges

## Detection Strategy

**MANDATORY Step 0**: Discover ALL FastMCP tools and resources in the codebase.

- This scan is **required** - do not skip this step
- You **must** read and process ALL tool/resource output using your intelligence
- High recall required, high precision NOT required - you determine which need documentation improvements
- Review each for: Field descriptions, polling guidance, context in docstrings
- Prevents lazy analysis by forcing examination of ALL FastMCP endpoints

```bash
# Find ALL @mcp.tool and @server.tool decorators
rg --type py '@(mcp|server)\.(tool|flat_model)' -B 2 -A 15 --line-number

# Find ALL FastMCP server instantiations
rg --type py 'FastMCP\(|Server\(' -B 1 -A 5 --line-number

# Find ALL response models (BaseModel subclasses in MCP servers)
rg --type py 'class.*Response.*BaseModel' -A 10 --line-number

# Find ALL request/input models (often Args or Request suffix)
rg --type py 'class.*(Args|Request|Input).*BaseModel' -A 10 --line-number

# Find FastMCP-specific imports and usage
rg --type py 'from fastmcp import|import fastmcp' -B 1 -A 3 --line-number

# Find resource definitions (@server.resource or mcp.resource)
rg --type py '@(mcp|server)\.resource' -B 2 -A 10 --line-number
```

**What to review for each tool/resource**:

1. **Response models**: Do all fields have `Field(description=...)`?
2. **Field descriptions**: Provide context (formats, usage patterns, relationships)?
3. **Docstrings**: Focus on behavior/polling, not schema structure?
4. **Redundancy**: Does docstring duplicate Pydantic schema info?
5. **Polling guidance**: Do async operations have polling recommendations?

**Process ALL output**: Read each tool/resource, use your judgment to identify documentation gaps.

---

**Primary Method**: Manual code reading of MCP server implementations.

**Discovery aids**:

```bash
# Find response models without Field descriptions
rg --type py "class.*Response.*BaseModel" -A10 mcp_infra/ adgn/mcp/

# Find tools with redundant schema documentation
rg --type py -B2 -A15 "@server\.(flat_model|tool)" mcp_infra/ adgn/mcp/ | rg "Returns:"

# Find MCP server implementations
fd -e py "server\.py$" mcp_infra/ adgn/mcp/
```

**Manual review focus**:

1. Check all response model fields have Field(description=...)
2. Verify descriptions provide context, not just field name rephrasing
3. Ensure docstrings don't duplicate schema structure
4. Check for polling guidance in relevant tool docstrings

## Fix Strategy

1. **Add Field descriptions to response models**:

   ```python
   from pydantic import Field

   field_name: str = Field(description="Helpful description with context")
   ```

2. **Remove redundant "Returns:" sections** from tool docstrings

3. **Enhance field descriptions** with:
   - Expected formats (ISO 8601, URL, path patterns)
   - Usage context (how to use this value)
   - Relationships to other fields
   - Polling patterns when applicable

4. **Add polling guidance** to tool docstrings:
   - Recommended polling intervals
   - What to compare to detect completion
   - Typical completion time ranges

## References

### FastMCP Resources

- [FastMCP Official Documentation](https://gofastmcp.com/)
- [FastMCP GitHub Repository](https://github.com/jlowin/fastmcp)
- [Tools - Parameter Metadata](https://gofastmcp.com/servers/tools#parameter-metadata) - Official guide to Field usage
- [Complex Inputs Example](https://github.com/jlowin/fastmcp/blob/main/examples/complex_inputs.py) - Pydantic validation patterns
- [Memory Example](https://github.com/jlowin/fastmcp/blob/main/examples/memory.py) - Real-world usage with Field descriptions

### Related Resources

- [Pydantic Field Documentation](https://docs.pydantic.dev/latest/concepts/fields/)
- [JSON Schema Description](https://json-schema.org/understanding-json-schema/reference/generic.html#annotations)
- [Model Context Protocol Specification](https://spec.modelcontextprotocol.io/)
