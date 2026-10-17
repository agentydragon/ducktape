# FastMCP Typed Access Research

> **Status:** Research complete. Use ordinary `FastMCP` subclasses with typed tool attributes; there is no custom FastMCP base class.

## Problem

Tool/resource access in FastMCP is string-based (`tool_manager.get_tool(name)`). This means tool names and resource URIs are scattered string literals, invisible to mypy and IDE refactoring.

## Key Findings

1. **FastMCP has no built-in typed tool access.** Tools are stored in `ToolManager._tools: dict[str, Tool]`, keyed by string name.
2. **Standard Python features suffice** -- subclassing, class variables, and type annotations provide typed access without framework changes.
3. **"Steps constructed before servers exist"** is a pervasive pattern (tests, policy eval, prompts), so class-level constants are required alongside instance attributes.

## Recommended Patterns

### 1. Server Subclasses with Callable Tool Attributes

```python
from collections.abc import Awaitable, Callable
from typing import ClassVar

class RuntimeServer(FastMCP):
    # Class constants -- usable without server instance (tests, policy eval)
    EXEC_TOOL_NAME: ClassVar[str] = "exec"
    CONTAINER_INFO_URI: ClassVar[str] = "resource://container.info"

    # FastMCP's decorator returns the registered function, not a Tool object.
    exec_tool: Callable[..., Awaitable[ExecResult]]

    def __init__(self, docker_client):
        super().__init__("Runtime Server")
        @self.tool
        async def exec(input: ExecInput) -> ExecResult: ...
        self.exec_tool = exec

        @self.resource(self.CONTAINER_INFO_URI)
        async def container_info() -> dict: ...
```

### 2. Agent CLI Compositor Recipes (mount prefix SSOT)

```python
@dataclass
class ServerMount:
    prefix: str
    server_class: type[FastMCP]

class AgentCompositorRecipe:
    runtime: ClassVar[ServerMount] = ServerMount(prefix="runtime", server_class=RuntimeServer)
```

Mount prefixes live in recipes (not server classes) because the same server can be mounted at different prefixes.

### 3. Tool Call Factory

```python
# Works in both production and test code via class constants
make_mcp_tool_call(
    AgentCompositorRecipe.runtime.prefix,  # from recipe
    RuntimeServer.EXEC_TOOL_NAME,          # from server class
    ExecInput(cmd=["ls"])
)
```

## Migration Path

1. Implement server subclasses with typed tool attributes (start with 2-3 core servers)
2. Define compositor recipes, migrate mount prefix constants
3. Update test helpers to use typed references
4. Add resource URI constants to server classes
5. AST-based scan to verify zero remaining string literals
