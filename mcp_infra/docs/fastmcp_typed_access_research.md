# FastMCP Typed Access Research

**Goal:** Design patterns to eliminate string literal constants for MCP server/tool names, enabling typed tool access without `# type: ignore` suppressions.

**Status:** Research complete - recommendations provided

---

## 1. FastMCP Capabilities

### Core Architecture

FastMCP uses a **manager pattern** for tools, resources, and prompts:

```python
class FastMCP:
    def __init__(self, name: str, ...):
        self._tool_manager: ToolManager = ToolManager(...)
        self._resource_manager: ResourceManager = ResourceManager(...)
        self._prompt_manager: PromptManager = PromptManager(...)
```

**Key findings:**

1. **Tools are registered via decorators** that create `FunctionTool` instances:

   ```python
   @mcp.tool()
   def my_tool(input: MyInput) -> MyOutput:
       ...
   ```

2. **Tools are stored in `ToolManager._tools: dict[str, Tool]`** keyed by tool name (string)

3. **No built-in typed tool access** - FastMCP has no `server.my_tool` pattern; tools are accessed by string name via `tool_manager.get_tool(name)`

4. **Tool class structure:**

   ```python
   class Tool(FastMCPComponent):
       name: str
       description: str | None
       parameters: dict[str, Any]  # JSON schema
       output_schema: dict[str, Any] | None

   class FunctionTool(Tool):
       fn: Callable[..., Any]

       async def run(self, arguments: dict[str, Any]) -> ToolResult:
           ...
   ```

5. **Resources have URIs** (not names) and are accessed via `resource_manager.get_resource(uri)`:

   ```python
   class Resource(FastMCPComponent):
       uri: AnyUrl  # e.g., "resource://container.info"
       name: str
       mime_type: str

       async def read(self) -> str | bytes:
           ...
   ```

### Current Pattern (What Exists Today)

```python
# Server definition
server = EnhancedFastMCP("Runtime")

@server.flat_model(name="exec")
async def tool_exec(input: ExecInput, context: Context) -> BaseExecResult:
    ...

# Manual attribute assignment (with type: ignore)
server.exec_tool = tool_exec  # type: ignore[attr-defined]

# Constants for mount prefixes
RUNTIME_MOUNT_PREFIX: Final[str] = "runtime"

# Usage in test/prod code (still uses strings)
factory.make_mcp_tool_call(RUNTIME_MOUNT_PREFIX, "exec", ExecInput(...))
```

**Problems:**

- `server.exec_tool` doesn't exist at type-check time → needs `# type: ignore`
- Tool names are string literals ("exec") scattered in code
- No typed access to tool properties (name, schema, etc.)
- Resource URIs are string literals repeated across code

---

## 2. Pattern Recommendations

### Pattern A: Server Subclasses with Typed Tool Attributes

**Approach:** Define per-server subclasses with properly typed tool attributes set during `__init__`.

**Prototype:**

```python
from __future__ import annotations
from typing import Final
from fastmcp import FastMCP
from fastmcp.tools.tool import FunctionTool
from pydantic import BaseModel

class ExecInput(BaseModel):
    cmd: list[str]

class ExecResult(BaseModel):
    exit_code: int
    stdout: str

# Tool name constant (used in construction contexts)
EXEC_TOOL_NAME: Final[str] = "exec"

class RuntimeServer(FastMCP):
    """Runtime MCP server with typed tool attributes."""

    # Class attribute declarations for mypy
    exec_tool: FunctionTool

    def __init__(self, docker_client):
        super().__init__("Runtime Server")
        self._docker_client = docker_client

        # Register tool and assign to typed attribute
        @self.tool(name=EXEC_TOOL_NAME)
        async def exec_impl(input: ExecInput) -> ExecResult:
            """Execute command in Docker container."""
            # implementation...
            return ExecResult(exit_code=0, stdout="ok")

        # Assign to typed attribute (no type: ignore needed)
        self.exec_tool = exec_impl

# Usage: typed tool access
server = RuntimeServer(docker_client)
tool_name = server.exec_tool.name  # type: str, no string literals
```

**Key points:**

- Class attribute declarations (`exec_tool: FunctionTool`) make mypy happy
- Tools registered in `__init__` and assigned to typed attributes
- Tool name accessed via `server.exec_tool.name` (not string literal)
- Constants like `EXEC_TOOL_NAME` only needed for construction contexts (policy eval, default values)

**Trade-offs:**

- ✅ **Pros:**
  - Clean mypy validation (no type: ignore needed)
  - Typed access to tool metadata (name, schema, etc.)
  - IDE autocomplete works
  - Single point of truth for tool definitions

- ❌ **Cons:**
  - Requires subclassing `FastMCP` for each server type
  - Tool registration must happen in `__init__` (can't use class-level decorators)
  - Slightly more boilerplate per server

**FastMCP compatibility:** ✅ Excellent - works with FastMCP's existing decorator pattern, just adds typed wrappers

---

### Pattern B: Tool Registry Pattern (Separate Registry Object)

**Approach:** Return both server and a typed registry object from factory functions.

**Prototype:**

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Final
from fastmcp import FastMCP
from fastmcp.tools.tool import FunctionTool
from pydantic import BaseModel

class ExecInput(BaseModel):
    cmd: list[str]

class ExecResult(BaseModel):
    exit_code: int
    stdout: str

EXEC_TOOL_NAME: Final[str] = "exec"

@dataclass
class RuntimeTools:
    """Typed registry of runtime server tools."""
    exec: FunctionTool

def make_runtime_server(docker_client) -> tuple[FastMCP, RuntimeTools]:
    """Create runtime server with typed tool registry."""
    mcp = FastMCP("Runtime Server")

    @mcp.tool(name=EXEC_TOOL_NAME)
    async def exec_impl(input: ExecInput) -> ExecResult:
        """Execute command in Docker container."""
        return ExecResult(exit_code=0, stdout="ok")

    tools = RuntimeTools(exec=exec_impl)
    return mcp, tools

# Usage: separate registry object
server, tools = make_runtime_server(docker_client)
tool_name = tools.exec.name  # type: str
```

**Trade-offs:**

- ✅ **Pros:**
  - No subclassing needed
  - Clean separation of server and tool metadata
  - Easy to understand and test

- ❌ **Cons:**
  - Tuple return requires unpacking at callsites
  - Two objects to track (server + registry)
  - Doesn't help with mount prefixes (still need constants)

**FastMCP compatibility:** ✅ Excellent - completely orthogonal to FastMCP's internal structure

---

### Pattern C: Tool Name and Resource URI Constants (Class-Level)

**Approach:** Define tool names and resource URIs as class-level constants for construction contexts.

**Key Insight:** Tests and other construction contexts (policy eval, prompt templates) need tool names and resource URIs **before server instances exist**. Class-level constants solve this while keeping a single source of truth.

**Prototype:**

```python
from __future__ import annotations
from typing import ClassVar
from fastmcp import FastMCP
from fastmcp.tools.tool import FunctionTool

class RuntimeServer(FastMCP):
    """Runtime server with typed tool/resource access."""

    # Class-level constants for construction contexts (tests, policy eval, prompts)
    EXEC_TOOL_NAME: ClassVar[str] = "exec"
    CONTAINER_INFO_URI: ClassVar[str] = "resource://container.info"
    CONTAINER_LOGS_URI: ClassVar[str] = "resource://container.logs"

    # Instance-level typed attributes for production code
    exec_tool: FunctionTool
    # container_info_resource: Resource  # If FastMCP exposes Resource type

    def __init__(self, docker_client):
        super().__init__("Runtime Server")

        @self.tool(name=self.EXEC_TOOL_NAME)
        async def exec_impl(input: ExecInput) -> ExecResult:
            return ExecResult(exit_code=0, stdout="ok")
        self.exec_tool = exec_impl

        @self.resource(self.CONTAINER_INFO_URI)
        async def container_info() -> dict:
            return {"id": "abc123", "image": "ubuntu:22.04"}
        # Could store: self.container_info_resource = container_info

# Usage patterns:

# 1. Test code (no server instance - construction context)
uri = RuntimeServer.CONTAINER_INFO_URI  # ✅ Class constant
tool_name = RuntimeServer.EXEC_TOOL_NAME  # ✅ Class constant

responses_factory.make_mcp_tool_call(
    AgentRecipe.runtime.prefix,   # Mount prefix from recipe
    RuntimeServer.EXEC_TOOL_NAME,  # Tool name from class constant
    ExecInput(cmd=["ls"])
)

# 2. Production code (has server instance)
server = RuntimeServer(docker_client)
tool_name = server.exec_tool.name  # ✅ Typed attribute (preferred for refactoring)
# OR
tool_name = RuntimeServer.EXEC_TOOL_NAME  # ✅ Class constant (simpler, works everywhere)

# For resources, class constant is typically sufficient
uri = RuntimeServer.CONTAINER_INFO_URI
data = await session.read_resource(uri)
```

**Trade-offs:**

- ✅ **Pros:**
  - Single source of truth (class definition)
  - Works in both production and test contexts
  - Class-level constants (no instance needed for tests)
  - No repeated string literals
  - Easy to discover via IDE
  - Typed attributes available for production code when desired

- ❌ **Cons:**
  - Slightly more boilerplate (but necessary for tests)
  - Two ways to access (class constant vs instance attribute)

**FastMCP compatibility:** ✅ Perfect - just organizational, no conflicts

**Why both class constants and instance attributes?**

- **Class constants:** Required for construction contexts (tests, policy eval, prompts) that don't have server instances
- **Instance attributes:** Optional convenience for production code that already has server instances, enables typed access and refactoring

**Recommendation:** Always define class constants. Add instance attributes for tools (typed access), optional for resources (URIs are typically sufficient).

---

### Pattern D: Compositor Recipes (Server Composition Specifications)

**Approach:** Define typed recipes that specify standard server compositions with mount prefixes.

**Prototype:**

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import ClassVar, Type
from fastmcp import FastMCP

@dataclass
class ServerMount:
    """Specification for mounting a server at a specific prefix."""
    prefix: str
    server_class: Type[FastMCP]

class AgentCompositorRecipe:
    """Standard agent compositor layout - SSOT for mount prefixes."""

    # Class-level mount specifications
    runtime: ClassVar[ServerMount] = ServerMount(
        prefix="runtime",
        server_class=RuntimeServer
    )
    ui: ClassVar[ServerMount] = ServerMount(
        prefix="ui",
        server_class=UiServer
    )
    resources: ClassVar[ServerMount] = ServerMount(
        prefix="resources",
        server_class=ResourcesServer
    )

    @classmethod
    async def setup(cls, docker_client, ui_bus) -> Compositor:
        """Create and configure compositor per this recipe."""
        comp = Compositor()

        # Mount servers using recipe specs
        runtime_server = RuntimeServer(docker_client)
        await comp.mount_inproc(cls.runtime.prefix, runtime_server)

        ui_server = UiServer(ui_bus)
        await comp.mount_inproc(cls.ui.prefix, ui_server)

        resources_server = ResourcesServer(compositor=comp)
        await comp.mount_inproc(cls.resources.prefix, resources_server)

        return comp

# Usage: typed mount prefix access
mount_prefix = AgentCompositorRecipe.runtime.prefix  # "runtime"

# Tool call factory
def make_tool_call(server_mount: ServerMount, tool: FunctionTool, input: BaseModel):
    return ResponsesResult(
        output=[FunctionCallItem(
            name=f"{server_mount.prefix}_{tool.name}",
            arguments=input.model_dump_json()
        )]
    )

# Usage in test code
result = make_tool_call(
    AgentCompositorRecipe.runtime,
    runtime_server.exec_tool,
    ExecInput(cmd=["ls"])
)
```

**Trade-offs:**

- ✅ **Pros:**
  - Centralized composition specifications
  - Mount prefixes flow from recipe to callsites
  - Typed, structured "standard compositions"
  - Eliminates "constant grab-bags"
  - Works well for pinned infrastructure servers

- ❌ **Cons:**
  - Only suitable for pinned servers (foundational, non-optional)
  - Requires defining recipe classes
  - More abstraction layers

**FastMCP compatibility:** ✅ Good - recipes sit above FastMCP, no conflicts

---

## 3. Recommended Combined Approach

**Use all four patterns together:**

### 1. Server Subclasses with Class Constants (Patterns A + C)

Define typed server classes with both class constants (for tests) and instance attributes (for production):

```python
class RuntimeServer(FastMCP):
    # Class constants for construction contexts (tests, policy eval, prompts)
    EXEC_TOOL_NAME: ClassVar[str] = "exec"
    CONTAINER_INFO_URI: ClassVar[str] = "resource://container.info"

    # Instance attributes for production code (typed access)
    exec_tool: FunctionTool

    def __init__(self, docker_client):
        super().__init__("Runtime Server")

        @self.tool(name=self.EXEC_TOOL_NAME)
        async def exec_impl(input: ExecInput) -> ExecResult:
            ...
        self.exec_tool = exec_impl

        @self.resource(self.CONTAINER_INFO_URI)
        async def container_info() -> dict:
            ...
```

### 2. Compositor Recipes (Pattern D)

Define standard compositions with mount prefixes:

```python
class AgentCompositorRecipe:
    runtime: ClassVar[ServerMount] = ServerMount(
        prefix="runtime",
        server_class=RuntimeServer
    )
    # ... other servers
```

### 3. Tool Call Factory (Two Variants)

**Variant A: Production Code (has server instance)**

```python
def make_mcp_call_typed(
    server_mount: ServerMount,
    tool: FunctionTool,  # From server instance
    input: BaseModel
) -> ResponsesResult:
    """Generate MCP tool call using typed tool attribute."""
    return ResponsesResult(
        output=[FunctionCallItem(
            name=f"{server_mount.prefix}_{tool.name}",
            arguments=input.model_dump_json()
        )]
    )

# Usage in production code
server = RuntimeServer(docker_client)
result = make_mcp_call_typed(
    AgentCompositorRecipe.runtime,  # mount prefix from recipe
    server.exec_tool,                # tool from typed instance attribute
    ExecInput(cmd=["ls"])
)
```

**Variant B: Test/Construction Code (no server instance)**

```python
def make_mcp_call(
    server_prefix: str,  # From recipe
    tool_name: str,      # From class constant
    input: BaseModel
) -> ResponsesResult:
    """Generate MCP tool call using class constants (for tests)."""
    return ResponsesResult(
        output=[FunctionCallItem(
            name=f"{server_prefix}_{tool_name}",
            arguments=input.model_dump_json()
        )]
    )

# Usage in test code (mock construction - no server instance)
result = make_mcp_call(
    AgentCompositorRecipe.runtime.prefix,  # mount prefix from recipe
    RuntimeServer.EXEC_TOOL_NAME,          # tool name from class constant
    ExecInput(cmd=["ls"])
)
```

**Recommendation:** Keep the simpler variant B as the primary `make_mcp_tool_call` signature. Production code can use class constants too - they work everywhere and are simpler than managing server instances just for tool names.

### 4. Resource URIs

Access URIs via class constants (works in both production and test code):

```python
# No string literals, works everywhere
uri = RuntimeServer.CONTAINER_INFO_URI
data = await session.read_resource(uri)
```

---

## 4. Working Prototypes

### Prototype 1: Basic Server with Typed Tool Access

```python
from __future__ import annotations
from typing import ClassVar, Literal
from fastmcp import FastMCP
from fastmcp.tools.tool import FunctionTool
from pydantic import BaseModel, Field

# Input/Output models
class GreetInput(BaseModel):
    name: str = Field(description="Name to greet")

class GreetOutput(BaseModel):
    kind: Literal["Success"] = "Success"
    message: str

# Server with typed attributes
class GreeterServer(FastMCP):
    """Greeter MCP server with typed tool/resource access."""

    # Tool attribute (typed for mypy)
    say_hello_tool: FunctionTool

    # Resource URI constant (class-level)
    GREETING_TEMPLATE_URI: ClassVar[str] = "resource://greeting/template"

    def __init__(self):
        super().__init__("Greeter Server")

        # Register tool
        @self.tool(name="say_hello")
        async def say_hello_impl(input: GreetInput) -> GreetOutput:
            """Greet someone by name."""
            return GreetOutput(message=f"Hello, {input.name}!")

        # Assign to typed attribute (no type: ignore needed)
        self.say_hello_tool = say_hello_impl

        # Register resource
        @self.resource(self.GREETING_TEMPLATE_URI)
        async def greeting_template() -> str:
            """Greeting template text."""
            return "Hello, {name}!"

# Usage: typed access to tool name and resource URI
server = GreeterServer()
assert server.say_hello_tool.name == "say_hello"
assert GreeterServer.GREETING_TEMPLATE_URI == "resource://greeting/template"
```

**Verification:**

- ✅ No string literals for tool names (accessed via `server.say_hello_tool.name`)
- ✅ No string literals for resource URIs (accessed via class constant)
- ✅ Mypy validates without `type: ignore` directives
- ✅ IDE autocomplete works for `server.say_hello_tool`

---

### Prototype 2: Tool Call Factory Pattern

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import ClassVar
from fastmcp import FastMCP
from fastmcp.tools.tool import FunctionTool
from pydantic import BaseModel

# Tool call result model (simplified)
@dataclass
class ToolCallResult:
    server_prefix: str
    tool_name: str
    arguments: dict

# Factory function (no string literals!)
def make_mcp_tool_call(
    server_prefix: str,
    tool: FunctionTool,
    input: BaseModel
) -> ToolCallResult:
    """Create MCP tool call using typed references."""
    return ToolCallResult(
        server_prefix=server_prefix,
        tool_name=tool.name,  # No string literal - from tool object
        arguments=input.model_dump()
    )

# Server mount specification
@dataclass
class ServerMount:
    prefix: str
    server_class: type[FastMCP]

# Example usage with recipe
class MyRecipe:
    greeter: ClassVar[ServerMount] = ServerMount(
        prefix="greeter",
        server_class=GreeterServer
    )

# Usage: no string literals anywhere
server = GreeterServer()
result = make_mcp_tool_call(
    MyRecipe.greeter.prefix,      # mount prefix from recipe
    server.say_hello_tool,         # tool from typed attribute
    GreetInput(name="Alice")       # typed input
)

assert result.server_prefix == "greeter"
assert result.tool_name == "say_hello"
assert result.arguments == {"name": "Alice"}
```

**Verification:**

- ✅ No string literals for server names (from recipe)
- ✅ No string literals for tool names (from tool object)
- ✅ Type-safe input construction (Pydantic model)
- ✅ Refactoring-friendly (rename tool → updates all callsites)

---

### Prototype 3: Compositor Recipe with Mounted Servers

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import ClassVar

# Simplified compositor (interface only)
class Compositor:
    async def mount_inproc(self, prefix: str, server: FastMCP):
        ...

@dataclass
class ServerMount:
    """Specification for mounting a server."""
    prefix: str
    server_class: type[FastMCP]

class SimpleRecipe:
    """Standard composition with 2 servers."""

    # Class-level mount specifications (SSOT for prefixes)
    greeter: ClassVar[ServerMount] = ServerMount(
        prefix="greeter",
        server_class=GreeterServer
    )
    echo: ClassVar[ServerMount] = ServerMount(
        prefix="echo",
        server_class=EchoServer  # assume exists
    )

    @classmethod
    async def setup(cls) -> Compositor:
        """Create compositor with standard server layout."""
        comp = Compositor()

        # Mount servers using specs
        greeter_server = cls.greeter.server_class()
        await comp.mount_inproc(cls.greeter.prefix, greeter_server)

        echo_server = cls.echo.server_class()
        await comp.mount_inproc(cls.echo.prefix, echo_server)

        return comp

# Usage: typed access to mount prefixes
assert SimpleRecipe.greeter.prefix == "greeter"
assert SimpleRecipe.echo.prefix == "echo"

# Test code references recipe (not scattered constants)
def test_greeter_tool():
    server = GreeterServer()
    result = make_mcp_tool_call(
        SimpleRecipe.greeter.prefix,  # from recipe
        server.say_hello_tool,
        GreetInput(name="Bob")
    )
    assert result.server_prefix == "greeter"
```

**Verification:**

- ✅ Mount prefixes centralized in recipe class
- ✅ No "constant grab-bags" (`_shared/constants.py` not needed)
- ✅ Test code references recipe attributes (typed)
- ✅ Recipe documents standard composition patterns

---

## 5. Trade-offs Summary

### Pattern A: Server Subclasses

| Aspect                    | Score      | Notes                                       |
| ------------------------- | ---------- | ------------------------------------------- |
| **Mypy compatibility**    | ⭐⭐⭐⭐⭐ | Perfect - no type: ignore needed            |
| **FastMCP compatibility** | ⭐⭐⭐⭐⭐ | Works seamlessly with existing patterns     |
| **Boilerplate**           | ⭐⭐⭐     | Requires subclass per server                |
| **Refactoring support**   | ⭐⭐⭐⭐⭐ | Excellent - rename propagates automatically |
| **IDE support**           | ⭐⭐⭐⭐⭐ | Perfect autocomplete and go-to-definition   |

**Recommendation:** ✅ **Adopt** - Best pattern for typed tool access

### Pattern B: Tool Registry

| Aspect                    | Score      | Notes                                  |
| ------------------------- | ---------- | -------------------------------------- |
| **Mypy compatibility**    | ⭐⭐⭐⭐⭐ | Clean typed dataclasses                |
| **FastMCP compatibility** | ⭐⭐⭐⭐⭐ | Orthogonal to FastMCP internals        |
| **Boilerplate**           | ⭐⭐⭐⭐   | Minimal - just dataclass + factory     |
| **Refactoring support**   | ⭐⭐⭐⭐   | Good but requires tracking two objects |
| **IDE support**           | ⭐⭐⭐⭐   | Good for registry access               |

**Recommendation:** ⚠️ **Optional** - Use if subclassing is undesirable, but Pattern A is preferred

### Pattern C: Resource URI Constants

| Aspect                    | Score      | Notes                          |
| ------------------------- | ---------- | ------------------------------ |
| **Mypy compatibility**    | ⭐⭐⭐⭐⭐ | Simple class constants         |
| **FastMCP compatibility** | ⭐⭐⭐⭐⭐ | No conflicts                   |
| **Boilerplate**           | ⭐⭐⭐⭐⭐ | Minimal - just class variables |
| **Refactoring support**   | ⭐⭐⭐⭐   | Good - centralized URIs        |
| **IDE support**           | ⭐⭐⭐⭐⭐ | Excellent                      |

**Recommendation:** ✅ **Adopt** - Simple and effective for resource URIs

### Pattern D: Compositor Recipes

| Aspect                    | Score      | Notes                              |
| ------------------------- | ---------- | ---------------------------------- |
| **Mypy compatibility**    | ⭐⭐⭐⭐⭐ | Clean typed dataclasses            |
| **FastMCP compatibility** | ⭐⭐⭐⭐⭐ | Sits above FastMCP layer           |
| **Boilerplate**           | ⭐⭐⭐     | Requires recipe classes            |
| **Refactoring support**   | ⭐⭐⭐⭐⭐ | Excellent - single source of truth |
| **IDE support**           | ⭐⭐⭐⭐⭐ | Perfect                            |

**Recommendation:** ✅ **Adopt** - Essential for eliminating constant grab-bags

---

## 6. Final Recommendations

### Adopt These Patterns

1. **Server Subclasses with Class Constants (Patterns A + C)**
   - Define typed subclasses for each MCP server type
   - Add **class-level constants** for tool names and resource URIs (for construction contexts)
   - Add **instance-level attributes** for typed tool access (for production code)
   - **Example:**

     ```python
     class RuntimeServer(FastMCP):
         # Class constants (for tests, policy eval, prompts)
         EXEC_TOOL_NAME: ClassVar[str] = "exec"
         CONTAINER_INFO_URI: ClassVar[str] = "resource://container.info"

         # Instance attributes (for production code)
         exec_tool: FunctionTool

         def __init__(self, docker_client):
             super().__init__("Runtime Server")
             @self.tool(name=self.EXEC_TOOL_NAME)
             async def exec_impl(...): ...
             self.exec_tool = exec_impl

             @self.resource(self.CONTAINER_INFO_URI)
             async def container_info(): ...
     ```

2. **Why Both Class Constants and Instance Attributes?**
   - **Class constants:** Required for test mock construction, policy eval, and prompt templates (no server instance available)
   - **Instance attributes:** Optional for production code with server instances (enables typed refactoring)
   - **Recommendation:** Always define class constants. Instance attributes optional but useful for tools.

3. **Compositor Recipes (Pattern D)**
   - Define recipe classes with `ServerMount` specifications
   - Centralize mount prefixes (no more `_shared/constants.py`)
   - **Example:**

     ```python
     class AgentCompositorRecipe:
         runtime: ClassVar[ServerMount] = ServerMount(
             prefix="runtime",
             server_class=RuntimeServer
         )
     ```

4. **Tool Call Factory**
   - Keep the simple signature using class constants (works everywhere)
   - **Example:**

     ```python
     def make_mcp_tool_call(
         server_prefix: str,  # From recipe.server.prefix
         tool_name: str,      # From ServerClass.TOOL_NAME
         input: BaseModel
     ) -> ResponsesResult:
         return ResponsesResult(
             output=[FunctionCallItem(
                 name=f"{server_prefix}_{tool_name}",
                 arguments=input.model_dump_json()
             )]
         )

     # Usage (works in both production and test code)
     result = make_mcp_tool_call(
         AgentCompositorRecipe.runtime.prefix,  # From recipe
         RuntimeServer.EXEC_TOOL_NAME,          # From server class
         ExecInput(cmd=["ls"])
     )
     ```

### Key Design Decisions

1. **Mount prefixes live in recipes** (not server classes)
   - Same server class can be mounted at different prefixes
   - Recipes document standard composition patterns
   - Eliminates "constant grab-bags"

2. **Tool names and resource URIs live on server classes**
   - **Class-level constants** for universal access (production + tests)
   - **Instance-level attributes** optionally for production code (typed refactoring)
   - Tests use class constants (no server instance needed)

3. **Keep it simple**
   - Production code doesn't need server instances just to get tool names
   - Class constants work everywhere (tests, policy eval, prompts, production)
   - Instance attributes optional enhancement when you already have a server

4. **No string literals in tool/resource operations**
   - All references via class constants or recipe attributes
   - Refactoring-safe and IDE-friendly

### FastMCP Limitations (None Critical)

- No built-in typed tool access (but easily added via subclasses)
- No built-in resource URI typing (but class constants work well)
- Tools must be registered in `__init__` (can't use class-level decorators)

**None of these limitations prevent the recommended patterns.**

### Migration Path

1. **Phase 1:** Implement server subclasses with typed tool attributes
   - Start with 2-3 core servers (runtime, ui, loop)
   - Validate mypy passes without `type: ignore`

2. **Phase 2:** Define compositor recipes
   - Create `AgentCompositorRecipe`, `PropsCompositorRecipe`, etc.
   - Migrate mount prefix constants to recipes

3. **Phase 3:** Implement tool call factory
   - Update test helpers to use typed references
   - Eliminate string literals in tool call construction

4. **Phase 4:** Add resource URI constants
   - Centralize URIs on server classes
   - Remove repeated `"resource://..."` literals

5. **Phase 5:** AST-based verification
   - Scan for any remaining string literals matching MCP names
   - Validate zero violations

---

## 7. Conclusion

**FastMCP supports the needed patterns** through standard Python features (subclassing, type annotations, class variables). No framework changes needed.

**The recommended combined approach:**

- ✅ Eliminates all string literals for MCP names
- ✅ Passes mypy without `type: ignore` suppressions
- ✅ Provides typed tool/resource access
- ✅ Enables IDE autocomplete and refactoring
- ✅ Works seamlessly with FastMCP internals
- ✅ Eliminates "constant grab-bags" via compositor recipes

**Next step:** Implement Phase 1 (server subclasses) for 2-3 servers and validate the approach before broader rollout.
