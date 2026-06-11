# FastMCP Resource Lifecycle and Cancellation Analysis

## Executive Summary

FastMCP uses **aggressive cancellation** when clients disconnect from in-process servers. The `FastMCPTransport.connect_session()` method creates a task group and **always calls `tg.cancel_scope.cancel()`** in its `finally` block (line 888 of `transports.py`). This cancels all async operations running in the server, including cleanup code in `finally` blocks.

**Key Finding**: Async operations in lifespan `finally` blocks are cancelled during client disconnect. Docker container cleanup must use synchronous operations or be shielded from cancellation.

---

## 1. Server-Scoped vs Session-Scoped Resources

### FastMCP Server Lifecycle

**File**: `/code/fastmcp/src/fastmcp/server/server.py`

FastMCP servers have a **lifespan context manager** that defines server-scoped resources:

```python
# Lines 111-121
@asynccontextmanager
async def default_lifespan(server: FastMCP[LifespanResultT]) -> AsyncIterator[Any]:
    """Default lifespan context manager that does nothing."""
    yield {}

# Lines 488-510
@asynccontextmanager
async def _lifespan_manager(self) -> AsyncIterator[None]:
    if self._lifespan_result_set:
        yield
        return

    async with (
        self._lifespan(self) as user_lifespan_result,
        self._docket_lifespan(user_lifespan_result) as lifespan_result,
    ):
        self._lifespan_result = lifespan_result
        self._lifespan_result_set = True

        async with AsyncExitStack[bool | None]() as stack:
            for server in self._mounted_servers:
                await stack.enter_async_context(
                    cm=server.server._lifespan_manager()
                )

            yield

    self._lifespan_result_set = False
    self._lifespan_result = None
```

### Lifespan Invocation Points

**When `__aenter__` runs:**

- For in-process servers: When `FastMCPTransport.connect_session()` calls `_enter_server_lifespan()` (lines 855, 895-903)
- The lifespan context is entered **once** when the first client connects
- Subsequent client connections reuse the existing lifespan (check: `if self._lifespan_result_set:`)

**When `__aexit__` runs:**

- When `_enter_server_lifespan()` exits
- This happens when the `FastMCPTransport` context manager exits
- **CRITICAL**: This occurs inside a cancel scope that gets cancelled

---

## 2. Cancellation Behavior - The Root Cause

### FastMCPTransport Cancellation

**File**: `/code/fastmcp/src/fastmcp/client/transports.py`, lines 841-888

```python
@contextlib.asynccontextmanager
async def connect_session(
    self, **session_kwargs: Unpack[SessionKwargs]
) -> AsyncIterator[ClientSession]:
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        # Create a cancel scope for the server task
        async with (
            anyio.create_task_group() as tg,
            _enter_server_lifespan(server=self.server),  # Line 855 - lifespan INSIDE task group
        ):
            # ... experimental capabilities setup ...

            tg.start_soon(
                lambda: self.server._mcp_server.run(
                    server_read,
                    server_write,
                    self.server._mcp_server.create_initialization_options(
                        experimental_capabilities=experimental_capabilities
                    ),
                    raise_exceptions=self.raise_exceptions,
                )
            )

            try:
                async with ClientSession(
                    read_stream=client_read,
                    write_stream=client_write,
                    **session_kwargs,
                ) as client_session:
                    yield client_session
            finally:
                tg.cancel_scope.cancel()  # Line 888 - ALWAYS CANCELS
```

**Critical Design Point**: The server lifespan is entered **inside** the task group. When `connect_session()` exits:

1. The `finally` block executes `tg.cancel_scope.cancel()` (line 888)
2. This cancels **all tasks in the task group**
3. The cancel scope then exits, triggering `_enter_server_lifespan()` to exit
4. This calls `__aexit__` on the server's lifespan context manager
5. **Any async operations in the lifespan `finally` block are cancelled**

### Why This Design?

Looking at the Client implementation:

**File**: `/code/fastmcp/src/fastmcp/client/client.py`, lines 570-593

```python
async def _session_runner(self):
    """
    Background task that manages the actual session lifecycle.

    This task runs in the background and:
    1. Establishes the transport connection via _context_manager()
    2. Signals that the session is ready via _ready_event.set()
    3. Waits for disconnect signal via _stop_event.wait()
    4. Ensures _ready_event is always set, even on failures
    """
    try:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self._context_manager())
            # Session/context is now ready
            self._session_state.ready_event.set()
            # Wait until disconnect/stop is requested
            await self._session_state.stop_event.wait()
    finally:
        # Ensure ready event is set even if context manager entry fails
        self._session_state.ready_event.set()
```

The design uses cancellation to **forcefully terminate** server operations when a client disconnects. This ensures:

- No hanging connections
- Quick cleanup
- Resource reclamation

However, it comes at the cost of cancelling cleanup operations.

---

## 3. In-Process Client Pattern

### How `Client(server)` Works

**File**: `/code/fastmcp/src/fastmcp/client/client.py`, lines 238-336

```python
def __init__(
    self,
    transport: (
        ClientTransportT
        | FastMCP
        | FastMCP1Server
        | AnyUrl
        | Path
        | MCPConfig
        | dict[str, Any]
        | str
    ),
    # ... other params ...
) -> None:
    self.transport = cast(ClientTransportT, infer_transport(transport))
    # ... initialization ...
```

When you pass a `FastMCP` server instance:

1. `infer_transport()` wraps it in a `FastMCPTransport`
2. The transport creates in-memory streams connecting client and server
3. Both run in the same event loop but communicate via memory streams

### Client `__aenter__` and `__aexit__`

**Lines 474-482**:

```python
async def __aenter__(self):
    return await self._connect()

async def __aexit__(self, exc_type, exc_val, exc_tb):
    # Use a timeout to prevent hanging during cleanup if the connection is in a bad
    # state (e.g., rate-limited). The MCP SDK's transport may try to terminate the
    # session which can hang if the server is unresponsive.
    with anyio.move_on_after(5):
        await self._disconnect()
```

**Lines 484-530** (`_connect` method):

- Creates `_session_runner()` task
- Waits for server to be ready
- Manages reference counting for reentrant contexts

**Lines 532-568** (`_disconnect` method):

- Decrements reference counter
- When counter reaches 0: sets stop_event
- Waits for `_session_runner()` to complete

### Lifespan Management Flow

```
Client(server).__aenter__()
  └─> _connect()
      └─> creates _session_runner() task
          └─> enters _context_manager()
              └─> transport.connect_session()  [FastMCPTransport]
                  └─> _enter_server_lifespan()
                      └─> server._lifespan_manager()  ← Server lifespan __aenter__
                          └─> user's lifespan __aenter__ (e.g., create Docker container)

[Work happens here]

Client.__aexit__()
  └─> _disconnect()
      └─> sets stop_event
          └─> _session_runner() exits
              └─> _context_manager() exits
                  └─> transport.connect_session() exits
                      └─> tg.cancel_scope.cancel()  ← CANCELLATION POINT
                          └─> _enter_server_lifespan() exits
                              └─> server._lifespan_manager() exits
                                  └─> user's lifespan __aexit__ (CANCELLED)
```

---

## 4. Best Practices for Resource Management

### The Problem

```python
@asynccontextmanager
async def lifespan(server: FastMCP):
    # Container created successfully
    container = await docker_client.containers.create(...)
    await container.start()

    yield {}

    # This cleanup code runs in a finally block internally
    # BUT it gets cancelled when the client disconnects!
    await container.stop()  # ← CancelledError here
    await container.remove()  # ← Never runs
```

### Solution Options

#### Option 1: Synchronous Cleanup (Recommended for Critical Resources)

Use synchronous Docker SDK calls for cleanup:

```python
from contextlib import asynccontextmanager
import docker  # Synchronous Docker SDK
from aiodocker import Docker as AsyncDocker  # For async operations

@asynccontextmanager
async def lifespan(server: FastMCP):
    async_docker = AsyncDocker()
    sync_docker = docker.from_env()

    container = await async_docker.containers.create(...)
    container_id = container.id
    await container.start()

    try:
        yield {}
    finally:
        # Synchronous cleanup cannot be cancelled
        try:
            sync_container = sync_docker.containers.get(container_id)
            sync_container.stop(timeout=5)
            sync_container.remove()
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
        finally:
            await async_docker.close()
```

**Pros:**

- Guaranteed to complete
- Cannot be cancelled
- Simple and reliable

**Cons:**

- Blocks the event loop during cleanup
- Less efficient for I/O operations

#### Option 2: Shield Critical Cleanup

Use `asyncio.shield()` to protect cleanup from cancellation:

```python
import asyncio

@asynccontextmanager
async def lifespan(server: FastMCP):
    container = await docker_client.containers.create(...)
    await container.start()

    try:
        yield {}
    finally:
        # Shield cleanup from cancellation
        try:
            await asyncio.shield(asyncio.gather(
                container.stop(),
                container.remove(),
                return_exceptions=True
            ))
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
```

**Pros:**

- Allows async cleanup to complete
- More efficient than sync calls

**Cons:**

- Slightly more complex
- Shield doesn't work across cancel scopes in some edge cases
- Must use `asyncio.shield()`, not anyio alternatives

#### Option 3: Background Cleanup Task

Start cleanup as a background task that survives cancellation:

```python
@asynccontextmanager
async def lifespan(server: FastMCP):
    container = await docker_client.containers.create(...)
    await container.start()

    try:
        yield {}
    finally:
        # Create detached cleanup task
        async def cleanup():
            try:
                await container.stop()
                await container.remove()
            except Exception as e:
                logger.error(f"Cleanup failed: {e}")

        asyncio.create_task(cleanup())  # Fire and forget
```

**Pros:**

- Never blocks
- Cannot be cancelled

**Cons:**

- No guarantee cleanup completes before process exit
- Harder to track cleanup failures
- Resource leaks if process terminates

#### Option 4: Hybrid Approach (Recommended for Production)

Combine shielded async cleanup with sync fallback:

```python
@asynccontextmanager
async def lifespan(server: FastMCP):
    async_docker = AsyncDocker()
    sync_docker = docker.from_env()

    container = await async_docker.containers.create(...)
    container_id = container.id
    await container.start()

    try:
        yield {}
    finally:
        cleanup_timeout = 5.0

        # Try async cleanup with shield
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(
                    container.stop(),
                    container.remove(),
                )),
                timeout=cleanup_timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Fallback to sync cleanup
            logger.warning("Async cleanup failed, using sync fallback")
            try:
                sync_container = sync_docker.containers.get(container_id)
                sync_container.stop(timeout=2)
                sync_container.remove()
            except Exception as e:
                logger.error(f"Sync cleanup also failed: {e}")
        finally:
            await async_docker.close()
```

---

## 5. Container Lifecycle Modes

### Mode Mapping to FastMCP Patterns

#### (a) External Container ID - No Lifecycle Management

```python
@asynccontextmanager
async def lifespan(server: FastMCP):
    # Server doesn't manage lifecycle
    external_container_id = os.environ.get("RUNTIME_CONTAINER_ID")
    yield {"container_id": external_container_id}
    # No cleanup needed
```

**Aligns with:** Not really a lifespan pattern - just configuration

#### (b) Server-Scoped Container

```python
@asynccontextmanager
async def lifespan(server: FastMCP):
    """Container lives as long as the server instance."""
    sync_docker = docker.from_env()

    container = sync_docker.containers.create(
        image="runtime:latest",
        detach=True
    )
    container.start()

    try:
        yield {"container": container}
    finally:
        # Synchronous cleanup for reliability
        try:
            container.stop(timeout=5)
            container.remove()
        except Exception as e:
            logger.error(f"Server-scoped container cleanup failed: {e}")
```

**Aligns with:** FastMCP server lifespan (but use sync cleanup!)

#### (c) Session-Scoped Container

```python
# NOT POSSIBLE with FastMCP lifespan pattern!
# Would need to be implemented differently
```

**Problem:** FastMCP doesn't have a "session-scoped" lifespan pattern. The server lifespan is entered once and reused across all client connections.

**Alternative Implementation:**

- Use a resource or tool with internal state tracking
- Manage container lifecycle in tool implementations
- Use request context to track sessions

```python
class SessionManager:
    def __init__(self):
        self.sessions: dict[str, Container] = {}

    async def get_or_create_session_container(self, session_id: str):
        if session_id not in self.sessions:
            container = await docker_client.containers.create(...)
            await container.start()
            self.sessions[session_id] = container
        return self.sessions[session_id]

    async def cleanup_session(self, session_id: str):
        if session_id in self.sessions:
            container = self.sessions.pop(session_id)
            await asyncio.shield(container.stop())
            await asyncio.shield(container.remove())

# In server lifespan
session_manager = SessionManager()
yield {"session_manager": session_manager}
```

#### (d) Call-Scoped Container (Ephemeral)

```python
@mcp.tool()
async def execute_in_container(code: str) -> str:
    """Each tool call creates a fresh container."""
    sync_docker = docker.from_env()

    container = sync_docker.containers.create(
        image="runtime:latest",
        command=["python", "-c", code],
        detach=True
    )

    try:
        container.start()
        result = container.wait()
        logs = container.logs().decode('utf-8')
        return logs
    finally:
        # Cleanup is NOT in lifespan, so no cancellation issues
        try:
            container.stop(timeout=2)
            container.remove()
        except Exception as e:
            logger.error(f"Container cleanup failed: {e}")
```

**Aligns with:** Tool-level resource management (no lifespan involved)

---

## 6. Key Recommendations

### For Container Cleanup

1. **Use synchronous operations** for critical cleanup in lifespan contexts
2. **Never rely on async cleanup** in `finally` blocks during client disconnect
3. **Always wrap cleanup** in try/except to prevent cascading failures
4. **Log cleanup failures** for debugging

### For Resource Scoping

1. **Server-scoped resources:** Put in lifespan (with sync cleanup)
2. **Session-scoped resources:** Use request context or separate manager (not lifespan)
3. **Call-scoped resources:** Manage in tool/resource functions (not lifespan)
4. **External resources:** Just store configuration (no lifecycle)

### Testing

Test cleanup behavior with:

```python
async def test_cleanup_on_disconnect():
    server = FastMCP("test")

    cleanup_called = asyncio.Event()

    @asynccontextmanager
    async def lifespan(server):
        yield {}
        # This should NOT be async
        cleanup_called.set()

    server._lifespan = lifespan

    async with Client(server) as client:
        await client.list_tools()

    # Wait briefly for cleanup
    await asyncio.wait_for(cleanup_called.wait(), timeout=1.0)
```

---

## 7. Related FastMCP Patterns

### Docket Integration (Background Tasks)

**File**: `/code/fastmcp/src/fastmcp/server/server.py`, lines 377-486

FastMCP integrates with Docket for background task execution. The Docket lifespan is nested **inside** the user lifespan:

```python
async with (
    self._lifespan(self) as user_lifespan_result,
    self._docket_lifespan(user_lifespan_result) as lifespan_result,
):
    # ...
```

**Implication**: Docket cleanup also happens in the cancel scope and may be interrupted.

### Mounted Servers

**Lines 501-507**:

```python
async with AsyncExitStack[bool | None]() as stack:
    for server in self._mounted_servers:
        await stack.enter_async_context(
            cm=server.server._lifespan_manager()
        )
    yield
```

Mounted servers share the same cancellation fate as the parent server.

---

## 8. Conclusion

FastMCP's cancellation behavior is **by design** - it prioritizes quick disconnect over graceful cleanup. This is appropriate for most use cases but requires careful handling of critical resources like Docker containers.

**Key Takeaway**: Any cleanup that **must** complete should use synchronous operations or be adequately shielded from cancellation.

### Further Reading

- FastMCP source: `/code/fastmcp/src/fastmcp/`
- anyio cancel scopes: <https://anyio.readthedocs.io/en/stable/cancellation.html>
- asyncio.shield: <https://docs.python.org/3/library/asyncio-task.html#asyncio.shield>

---

## Appendix: Error You're Seeing

```
ERROR during cleanup: CancelledError: Cancelled via cancel scope ...
  by <Task coro=<Client._session_runner()>
```

**What's happening:**

1. Your lifespan's `__aexit__` runs inside the cancel scope
2. It tries to call `await container.stop()`
3. The cancel scope has already been cancelled
4. The async operation raises `CancelledError`
5. Container cleanup never completes

**Fix:** Use synchronous Docker operations or shield the cleanup.
