# FastMCP Resource Lifecycle and Cancellation Analysis

> **Status:** Research complete. Key finding documented; see also `x/agent_server/docs/async_cancellation_deep_dive.md` for detailed explanation.

## Key Finding

FastMCP uses **aggressive cancellation** when clients disconnect from in-process servers. `FastMCPTransport.connect_session()` creates a task group and **always calls `tg.cancel_scope.cancel()`** in its `finally` block. This cancels all async operations running in the server, including cleanup code in `finally` blocks.

**Consequence:** Async operations in lifespan `finally` blocks are cancelled during client disconnect. Docker container cleanup must use synchronous operations or be shielded from cancellation.

## Server vs Session Lifecycle

- **Server-scoped** (`lifespan` context manager): resources shared across all sessions. Cleanup runs when the last session disconnects.
- **Session-scoped** (`session_manager`): per-client resources. Cleanup runs on disconnect -- inside the cancel scope.

Both lifespans run inside `connect_session()`'s cancel scope, so both are subject to cancellation.

## Cancellation Path

1. Client disconnects (EOF on read stream)
2. `connect_session()` exits, entering `finally` block
3. `tg.cancel_scope.cancel()` fires
4. All pending `await` points raise `CancelledError`
5. Lifespan `__aexit__` tries to run cleanup
6. Any `await` in cleanup raises `CancelledError` -- cleanup fails

Mounted servers share the same cancellation fate as the parent server (nested `AsyncExitStack`).

## Fixes for Critical Cleanup

| Option                                | Approach                               | Tradeoffs                                   |
| ------------------------------------- | -------------------------------------- | ------------------------------------------- |
| **Synchronous cleanup**               | Use sync Docker SDK calls              | Simple, reliable, blocks event loop briefly |
| **Move lifecycle out**                | Manage containers outside lifespan     | Cleaner architecture, more refactoring      |
| **`anyio.move_on_after` with shield** | Shield cleanup from cancellation       | Fragile, depends on anyio internals         |
| **Synchronous fallback**              | Try async, fall back to sync on cancel | Best of both worlds, more code              |

**Recommendation:** Use synchronous cleanup for Docker containers (short-term), move container management outside lifespan (long-term).
