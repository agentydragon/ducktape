# BaseHTTPMiddleware Response Reconstruction Issues

## Overview

Two separate routing middlewares in the MCP bridge both inherit from `BaseHTTPMiddleware` and suffer from the same
architectural problem: they reconstruct responses by buffering ASGI messages, which breaks important HTTP semantics.

## Affected Clusters

### Cluster #11: MCP Routing Duplicate Headers

- **File:** `adgn/src/adgn/agent/server/mcp_routing.py` (lines 99-150)
- **Complexity:** 50
- **Problem:** Dict comprehension collapses duplicate headers (e.g., multiple Set-Cookie)

### Cluster #18: Compositor Routing Buffers Streaming

- **File:** `adgn/src/adgn/agent/mcp_bridge/server.py` (lines 222-248)
- **Complexity:** 60
- **Problem:** Buffers entire response, breaking SSE and other streaming endpoints

## Root Cause: BaseHTTPMiddleware

Both middlewares use `BaseHTTPMiddleware` which provides a nice high-level API:

```python
class SomeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Do auth/routing
        return response
```

However, when routing to another ASGI app (not calling `call_next`), they must manually invoke the backend app and
reconstruct a Response:

```python
async def dispatch(self, request: Request, call_next) -> Response:
    backend_app = await self._get_backend_app(...)

    # Capture backend's ASGI messages
    headers = []
    body_parts = []

    async def send(message):
        if message["type"] == "http.response.start":
            headers = message.get("headers", [])
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    await backend_app(request.scope, request.receive, send)

    # Reconstruct Response - THIS IS WHERE THINGS BREAK
    return Response(...)
```

## Issue #1: Duplicate Headers (Cluster #11)

**Location:** `mcp_routing.py` line 142

**The Bug:**

```python
response_headers = {k.decode(): v.decode() for k, v in headers}
return Response(content=body, status_code=status_code, headers=response_headers)
```

**Why it breaks:**

- ASGI headers are `list[(bytes, bytes)]` to preserve duplicates
- Converting to dict keeps only the last value for each header name
- Multiple `Set-Cookie` headers collapse to one

**Example:**

```python
# ASGI headers from backend
headers = [
    (b'set-cookie', b'session=abc'),
    (b'set-cookie', b'token=xyz'),
]

# Dict comprehension
response_headers = {k.decode(): v.decode() for k, v in headers}
# Result: {'set-cookie': 'token=xyz'}  ← First cookie lost!
```

### Inspection Commands

```bash
# View the problematic line
adgn-properties snapshot exec ducktape/2025-11-20-00 -- \
  sed -n '142,142p' adgn/src/adgn/agent/server/mcp_routing.py

# View full dispatch method
adgn-properties snapshot exec ducktape/2025-11-20-00 -- \
  sed -n '99,150p' adgn/src/adgn/agent/server/mcp_routing.py
```

## Issue #2: Streaming Broken (Cluster #18)

**Location:** `server.py` lines 222-248

**The Bug:**

```python
async def send(message):
    if message["type"] == "http.response.body":
        body_parts.append(message.get("body", b""))
        # Ignores message.get("more_body") flag!

await compositor_app(request.scope, request.receive, send)

# Only returns after compositor_app completes
body = b"".join(body_parts)
return Response(content=body, ...)
```

**Why it breaks:**

- SSE and streaming endpoints send chunks over time with `more_body=True`
- This middleware buffers everything and only returns after the stream ends
- Clients see no data until the entire stream completes
- Defeats real-time transport (SSE message feeds, progress updates, etc.)

**Example failure:**

- Compositor exposes SSE endpoint for real-time approval updates
- Client connects expecting incremental messages
- Middleware buffers all SSE events
- Client receives nothing until stream closes
- Real-time functionality completely broken

### Inspection Commands

```bash
# View the buffering middleware
adgn-properties snapshot exec ducktape/2025-11-20-00 -- \
  sed -n '222,248p' adgn/src/adgn/agent/mcp_bridge/server.py
```

## Unified Solution: Pure ASGI Middleware

Both issues share the same fix: **stop reconstructing responses**. Use pure ASGI middleware that forwards messages
directly.

### Example: MCP Routing (fixes both issues if applied to both files)

```python
class MCPRoutingMiddleware:
    """Pure ASGI middleware - no response reconstruction."""

    def __init__(self, app, token_table):
        self.app = app
        self.token_table = token_table

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract and validate token from scope["headers"]
        token = self._extract_bearer_token(scope["headers"])
        if not token:
            response = Response("Missing Authorization header", 401)
            await response(scope, receive, send)
            return

        token_info = self.token_table.get(token)
        if not token_info:
            response = Response("Invalid token", 401)
            await response(scope, receive, send)
            return

        # Get backend ASGI app
        try:
            role = TokenRole(token_info["role"])
            agent_id = token_info.get("agent_id")
            backend_app = await self._get_backend_app(role, agent_id)
        except (KeyError, ValueError) as e:
            response = Response(str(e), 500)
            await response(scope, receive, send)
            return

        # Forward directly - backend's send() goes straight to client
        # ✅ Preserves duplicate headers
        # ✅ Streams chunks in real-time
        # ✅ No buffering
        await backend_app(scope, receive, send)
```

### Example: Compositor Routing

Same pattern, different auth/routing logic:

```python
class CompositorRoutingMiddleware:
    """Pure ASGI middleware for compositor routing."""

    def __init__(self, app, registry):
        self.app = app
        self.registry = registry

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Get agent_id from request state (set by upstream middleware)
        # Note: In pure ASGI, we need to handle state differently
        # Simplest: require agent_id in scope or parse from path

        agent_id = scope.get("state", {}).get("agent_id")
        if not agent_id:
            response = Response("No agent ID", 400)
            await response(scope, receive, send)
            return

        # Get compositor app for this agent
        compositor_app = await self.registry.get_compositor_app(agent_id)

        # Forward directly - preserves streaming!
        await compositor_app(scope, receive, send)
```

**Note on request.state:** Pure ASGI middleware doesn't have `Request.state`. You'll need to:

1. Parse agent_id from scope directly, OR
2. Have upstream middleware set `scope["state"] = {"agent_id": ...}`, OR
3. Use path parameters

## Why Pure ASGI is Better

### Advantages

- ✅ **Preserves all ASGI semantics**
  - Duplicate headers work correctly
  - Streaming works correctly
  - WebSocket upgrade works correctly
- ✅ **No buffering**
  - Lower memory usage
  - Real-time chunk delivery
  - Supports arbitrarily large responses
- ✅ **Simpler code**
  - No manual message capture
  - No response reconstruction
  - Fewer lines of code
- ✅ **Standard pattern**
  - How ASGI proxies/routers are meant to work
  - Used by production ASGI servers
- ✅ **Future-proof**
  - Doesn't rely on Starlette internals
  - Works with any ASGI app

### Disadvantages

- ❌ **Lower-level API**
  - `__call__(scope, receive, send)` instead of `dispatch(request, call_next)`
  - Less familiar to web framework developers
- ❌ **No Request object**
  - Must parse scope manually
  - Can't use `request.state` directly (but can use `scope["state"]`)

**Trade-off verdict:** The advantages far outweigh the disadvantages for routing middleware. The lower-level API is
worth it for correctness.

## Alternative Solutions (Not Recommended)

### Option 2: Preserve raw_headers manually

Keep BaseHTTPMiddleware but hack around the limitations:

```python
async def dispatch(self, request: Request, call_next) -> Response:
    # ... buffer headers/body ...

    response = Response(content=body, status_code=status_code)
    # Hack: Override internal attribute
    object.__setattr__(response, '_raw_headers', headers)
    return response
```

**Why not:**

- Only fixes duplicate headers, not streaming
- Relies on Starlette internals
- Still buffers entire response

### Option 3: Custom Response subclass

```python
class ASGIHeadersResponse(Response):
    def __init__(self, content, status_code, raw_headers):
        super().__init__(content=content, status_code=status_code, headers=None)
        self.raw_headers = raw_headers

    def init_headers(self, headers=None):
        pass  # Prevent recomputation
```

**Why not:**

- Only fixes duplicate headers, not streaming
- Still buffers entire response
- Adds custom Response type to maintain

## FastMCP Investigation

Checked FastMCP (jlowin's package) for existing solutions:

- Provides ASGI apps (`StreamableHTTPASGIApp`) but no routing middleware
- Uses standard Starlette routing (`Route`, `Mount`)
- Uses pure ASGI where needed (e.g., `StreamableHTTPASGIApp.__call__`)

**Conclusion:** FastMCP doesn't provide routing middleware. We need our own, and pure ASGI is the right approach.

## Recommendation

**Fix both middlewares by converting to pure ASGI middleware.**

Implementation steps:

1. Change `class MCPRoutingMiddleware(BaseHTTPMiddleware)` to plain class
2. Replace `async def dispatch(request, call_next)` with `async def __call__(scope, receive, send)`
3. Parse auth tokens from `scope["headers"]` instead of `Request` object
4. Remove response reconstruction - forward `send` directly to backend
5. Repeat for `CompositorRoutingMiddleware`

This fixes both issues (duplicate headers + streaming) with one architectural change.

## Issue File Status

**Decision:** Skipping creation of issue files for now (per user request).

When created, the issues should:

- Describe the specific symptom (header collapsing OR streaming broken)
- NOT prescribe pure ASGI as the solution
- Focus on what's broken and why it matters
- Keep rationale problem-focused, not solution-focused
