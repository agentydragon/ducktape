# Async Cancellation Deep Dive: Container Cleanup Failure

## From First Principles

Let me explain async, cancellation, scopes, lifespans, and shields like you've never heard of any of it.

### What is Async/Await?

**Normal (synchronous) code** runs one step at a time:

```python
def download_file():
    data = network.read()  # Blocks here, waiting for network
    return data
```

When `network.read()` is waiting for bytes from the network, your entire program is frozen, doing nothing.

**Async code** can say "I'm waiting, go do other things":

```python
async def download_file():
    data = await network.read()  # Says "I'm waiting, run other tasks"
    return data
```

The `await` keyword means: "This will take time. While waiting, the event loop can run other tasks."

### What is Cancellation?

Sometimes you want to **interrupt** an async operation before it finishes:

```python
async def long_operation():
    await asyncio.sleep(60)  # Sleep for 1 minute
    return "done"

task = asyncio.create_task(long_operation())
await asyncio.sleep(1)  # Wait 1 second
task.cancel()  # DON'T wait the full 60 seconds, stop now!
```

When you call `.cancel()`, Python raises a **`CancelledError`** at the next `await` point in that task.

**Crucially**: Cancellation happens AT `await` points. If you're running synchronous code, cancellation can't interrupt you:

```python
async def mixed():
    compute_for_10_seconds()  # Synchronous - can't be cancelled mid-execution
    await asyncio.sleep(1)    # ← Cancellation hits HERE
```

### What is a Cancel Scope?

A **cancel scope** is a way to cancel **multiple** tasks together as a group.

**anyio** (a library FastMCP uses) provides structured cancellation via task groups:

```python
async with anyio.create_task_group() as tg:
    tg.start_soon(task1)
    tg.start_soon(task2)
    tg.start_soon(task3)
# When this context exits, ALL three tasks are cancelled
```

The `create_task_group()` creates a **cancel scope** that controls all tasks started with `tg.start_soon()`.

When you exit the `async with` block, `tg.cancel_scope.cancel()` is called, which cancels ALL tasks in the group.

### What is a Lifespan?

A **lifespan** is a pattern for managing **server-scoped resources** - things that should live as long as the server is running and be cleaned up when it stops.

Think of it like `__init__` and `__del__` for a web server, but async:

```python
@asynccontextmanager
async def lifespan(app):
    # Setup (like __init__)
    db_pool = await create_database_pool()
    redis = await connect_to_redis()

    yield {"db": db_pool, "redis": redis}  # Server runs here

    # Cleanup (like __del__)
    await db_pool.close()
    await redis.disconnect()
```

FastMCP servers have a lifespan that runs when the server starts and cleans up when the server stops.

### What is asyncio.shield()?

`asyncio.shield()` is supposed to **protect** an operation from cancellation:

```python
async def important_cleanup():
    await database.commit()  # Must complete!

try:
    await asyncio.shield(important_cleanup())
except CancelledError:
    # The task calling this got cancelled, but important_cleanup() finished
    pass
```

**BUT**: `shield()` only works against **asyncio cancellation**, not anyio cancel scopes!

---

## The Actual Problem: Step-by-Step Execution Trace

Let's trace through exactly what happens when you run a critic in GEPA:

### Stack Frame 1: GEPA Thread (gepa_adapter.py)

```python
def run_in_new_loop():
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(self._evaluate_async(batch, candidate))
    finally:
        loop.close()  # ← This triggers everything below
```

### Stack Frame 2: Critic (critic.py:448-496)

```python
async with Compositor() as comp:
    runtime_server = await wiring.attach(comp)  # Creates Docker container
    async with Client(comp) as mcp_client:
        agent = await Agent.create(...)
        await agent.run(user_prompt)
    # ← When this exits, cleanup should happen
```

When the `async with Client(comp)` block exits, it calls `Client.__aexit__()`.

### Stack Frame 3: Client.**aexit** (client.py:477-482)

```python
async def __aexit__(self, exc_type, exc_val, exc_tb):
    with anyio.move_on_after(5):  # Timeout after 5 seconds
        await self._disconnect()
```

This calls `_disconnect()`, which signals the background `_session_runner` task to stop.

### Stack Frame 4: `Client._disconnect` (client.py:532-568)

```python
async def _disconnect(self, force: bool = False):
    async with self._session_state.lock:
        self._session_state.nesting_counter -= 1
        if self._session_state.nesting_counter > 0:
            return  # Still nested, don't actually disconnect

        self._session_state.stop_event.set()  # Signal to stop
        await self._session_state.session_task  # Wait for session to end
```

This sets the `stop_event`, which wakes up the `_session_runner` task.

### Stack Frame 5: `Client._session_runner` (client.py:570-593)

```python
async def _session_runner(self):
    try:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self._context_manager())
            self._session_state.ready_event.set()
            await self._session_state.stop_event.wait()  # ← Wakes up here
        # ← When the AsyncExitStack exits, it triggers cleanup
    finally:
        self._session_state.ready_event.set()
```

The `AsyncExitStack` exits, which triggers `_context_manager().__aexit__`.

### Stack Frame 6: `Client._context_manager` (client.py:393-409)

```python
@asynccontextmanager
async def _context_manager(self):
    with catch(get_catch_handlers()):
        async with self.transport.connect_session(**self._session_kwargs) as session:
            self._session_state.session = session
            if self.auto_initialize:
                await self.initialize()
            yield
        # ← Exits here, triggering transport.__aexit__
```

This exits the `async with self.transport.connect_session()` context, calling the transport's `__aexit__`.

### Stack Frame 7: FastMCPTransport.connect_session (transports.py:841-888)

**THIS IS WHERE THE CANCELLATION HAPPENS:**

```python
@asynccontextmanager
async def connect_session(self, **session_kwargs):
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with (
            anyio.create_task_group() as tg,
            _enter_server_lifespan(server=self.server),  # ← Line 855: Lifespan INSIDE task group
        ):
            tg.start_soon(
                lambda: self.server._mcp_server.run(...)
            )

            try:
                async with ClientSession(...) as client_session:
                    yield client_session
            finally:
                tg.cancel_scope.cancel()  # ← Line 888: ALWAYS CANCELS EVERYTHING
```

**Key observations:**

1. **Line 855**: `_enter_server_lifespan(server=self.server)` enters the server's lifespan context manager **INSIDE** the task group's `async with` block
2. **Line 888**: When the `connect_session` context exits, it **ALWAYS** calls `tg.cancel_scope.cancel()`
3. This cancellation affects **everything** in the task group's scope, including the lifespan context manager's `__aexit__`

### Stack Frame 8: `_enter_server_lifespan` (transports.py:894-903)

```python
@asynccontextmanager
async def _enter_server_lifespan(server: FastMCP | FastMCP1Server):
    if isinstance(server, FastMCP):
        async with server._lifespan_manager():
            yield
    else:
        yield
```

This enters `server._lifespan_manager()`, which for our case is the Compositor.

### Stack Frame 9: Compositor -> Runtime Server Lifespan

The Compositor has mounted an in-process runtime server with a Docker container lifespan. When `server._lifespan_manager()` exits, it triggers our container lifespan's `__aexit__`.

### Stack Frame 10: make_container_lifespan (container_session.py:152-205)

**THIS IS WHERE THE FAILURE HAPPENS:**

```python
@asynccontextmanager
async def lifespan(server: FastMCP):
    client = await _init_docker()
    container_dict = None
    try:
        container_dict = await _start_container(client=client, opts=opts)  # ✓ Works
        yield ContainerSessionState(...)  # Server runs
    finally:
        # ← We enter this finally block while CANCELLED
        print(f">>> Lifespan __aexit__: cleaning up container")
        try:
            if container_dict is not None:
                print(f">>> Killing container {container_dict['Id']}...")
                container = await client.containers.get(container_dict["Id"])  # ← CANCELLED HERE!
```

**What happens:**

1. We enter the `finally` block
2. We're **in a cancelled state** - the cancel scope from Stack Frame 7 has marked this entire context as cancelled
3. We try to `await client.containers.get(...)`
4. **`CancelledError` is raised immediately** at this `await` point
5. The `kill()` and `delete()` calls never execute
6. Container is left running

---

## Why Sync Cleanup "Works" (But Feels Wrong)

If we use **synchronous** Docker operations:

```python
finally:
    if container_dict is not None:
        sync_docker = docker.from_env()  # Synchronous client
        container = sync_docker.containers.get(container_dict["Id"])  # No await!
        container.stop(timeout=5)  # No await!
        container.remove()  # No await!
```

**Why this works:**

- Synchronous code has **no `await` points**
- Cancellation can only raise `CancelledError` at `await` points
- Since there are no `await` points, the cancellation **can't interrupt** the cleanup
- The cleanup completes, then Python sees we're in a cancelled state and propagates the `CancelledError` upward

**Why it feels wrong:**

- We're doing blocking I/O (network calls to Docker daemon) in an async context
- This **blocks the entire event loop** - no other tasks can run during cleanup
- It's mixing sync/async paradigms in a hacky way
- But it **does** guarantee cleanup happens

---

## Why asyncio.shield() Doesn't Help

You might think: "Just shield the cleanup!"

```python
finally:
    await asyncio.shield(container.kill())
```

**Why this doesn't work:**

1. `asyncio.shield()` only protects against **asyncio cancellation**
2. We're being cancelled by an **anyio cancel scope**
3. anyio and asyncio use different cancellation mechanisms
4. anyio's `tg.cancel_scope.cancel()` doesn't respect `asyncio.shield()`

---

## The Real Question: Is This FastMCP's Fault?

Let's look at the design decision in `transports.py:888`:

```python
finally:
    tg.cancel_scope.cancel()
```

**Why does FastMCP do this?**

The `finally` block ensures that when a client disconnects:

1. The server's message-handling task is terminated immediately
2. No messages are processed after the client is gone
3. The connection is cleaned up quickly

This is **aggressive cleanup** - FastMCP prioritizes **fast disconnect** over **graceful cleanup**.

**Is this reasonable?**

For most resources, yes:

- Database connections can be dropped
- File handles will be closed by the OS
- Memory will be garbage collected

For Docker containers? **No**:

- Containers keep running after your program exits
- They consume real system resources
- They need explicit cleanup

---

## Potential Solutions

### Option 1: Synchronous Cleanup (Current Recommendation)

```python
finally:
    if container_dict is not None:
        sync_docker = docker.from_env()
        try:
            container = sync_docker.containers.get(container_dict["Id"])
            container.stop(timeout=5)
            container.remove()
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
```

**Pros:**

- Guaranteed to complete
- Simple
- Works with current FastMCP design

**Cons:**

- Blocks event loop during cleanup
- Mixing sync/async
- Feels wrong

### Option 2: Move Container Management Out of Lifespan

```python
# In critic.py
async with Compositor() as comp:
    # Create container BEFORE mounting server
    container = await docker_client.containers.create(...)
    await container.start()

    try:
        # Mount server with container ID (mode "a" - external)
        runtime_server = await wiring.attach(comp, container_id=container.id)
        async with Client(comp) as mcp_client:
            agent = await Agent.create(...)
            await agent.run(user_prompt)
    finally:
        # Cleanup AFTER Compositor closes
        await container.stop()
        await container.remove()
```

**Pros:**

- Container lifecycle independent of MCP server lifespan
- Async cleanup happens in uncancelled context
- Clean separation of concerns

**Cons:**

- Requires refactoring all call sites
- More boilerplate at each usage point
- Duplicates container management logic

### Option 3: Background Cleanup Task

```python
finally:
    if container_dict is not None:
        # Spawn task that outlives this context
        asyncio.create_task(_cleanup_container_background(
            container_dict["Id"],
            client
        ))

async def _cleanup_container_background(container_id, client):
    try:
        # This runs in a separate task, not in the cancelled scope
        container = await client.containers.get(container_id)
        await container.stop()
        await container.remove()
    except Exception as e:
        logger.error(f"Background cleanup failed: {e}")
```

**Pros:**

- Async cleanup
- Doesn't block
- Works with current structure

**Cons:**

- Cleanup happens "eventually", not immediately
- Task might not complete if event loop closes
- Hard to test/verify
- No guarantee of cleanup

### Option 4: Custom anyio Shielding

```python
from anyio import CancelScope

finally:
    if container_dict is not None:
        with CancelScope(shield=True):  # anyio's version of shield
            container = await client.containers.get(container_dict["Id"])
            await container.stop()
            await container.remove()
```

**Let me check if this actually works...**

From anyio docs:

> A shielded cancel scope allows you to perform cleanup operations in response to cancellation requests, while still protecting a critical section from being cancelled.

**This might actually work!** The `shield=True` parameter tells anyio to **ignore** cancellation from parent scopes.

**Pros:**

- Async cleanup
- Doesn't block
- Works within lifespan pattern
- Respects async paradigm

**Cons:**

- Still might fail if event loop closes
- Adds dependency on anyio CancelScope behavior
- Need to test thoroughly

---

## Recommendation

**Short term (quick fix):** Use Option 1 (synchronous cleanup)

**Long term (proper architecture):** Either:

- Option 4 (anyio shielding) if it works reliably
- Option 2 (move lifecycle out of lifespan) for cleaner architecture

The fundamental issue is that **Docker containers are OS-level resources that need explicit cleanup**, and FastMCP's lifespan pattern is designed for **in-process resources that can be implicitly cleaned up**.

We need to either:

1. Make the cleanup robust against cancellation (Option 4)
2. Move container management to a layer that doesn't use the lifespan pattern (Option 2)

---

## Test Case

Here's a minimal test to verify each solution:

```python
import asyncio
from contextlib import asynccontextmanager
import anyio

@asynccontextmanager
async def lifespan_with_cleanup():
    print("Setup")
    resource = "container_id_123"
    try:
        yield resource
    finally:
        print("Cleanup starting")
        # Try different approaches here
        await asyncio.sleep(0.1)  # Simulate async cleanup
        print("Cleanup finished")

async def test():
    async with anyio.create_task_group() as tg:
        async with lifespan_with_cleanup() as res:
            print(f"Using: {res}")
        # TaskGroup.__aexit__ will call tg.cancel_scope.cancel()

asyncio.run(test())
```

Run this and see if "Cleanup finished" prints. If not, cleanup was cancelled.
