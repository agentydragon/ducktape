# Management UI WebSocket Auth Bypass

## Cluster Information

- **Cluster #13:** Management UI WebSocket endpoint lacks auth
- **Snapshot:** `ducktape/2025-11-20-00`
- **Complexity:** 55 (moderate)
- **Instances:** 1 critic finding
- **Files:**
  - `adgn/src/adgn/agent/mcp_bridge/auth.py`
  - `adgn/src/adgn/agent/mcp_bridge/server.py`

## The Problem

The management UI advertises token-authenticated access, but the `/ws/mcp` WebSocket endpoint is completely
unauthenticated. Anyone who can reach the server can open the management WebSocket without a token, undermining the
access controls the UI advertises.

### Inspection Commands

```bash
# View UITokenAuthMiddleware definition
adgn-properties snapshot exec ducktape/2025-11-20-00 -- \
  sed -n '124,160p' adgn/src/adgn/agent/mcp_bridge/auth.py

# View middleware installation in create_management_ui_app
adgn-properties snapshot exec ducktape/2025-11-20-00 -- \
  sed -n '271,289p' adgn/src/adgn/agent/mcp_bridge/server.py

# Search for WebSocket handler registration
adgn-properties snapshot exec ducktape/2025-11-20-00 -- \
  bash -c "grep -n 'ws/mcp\|websocket' adgn/src/adgn/agent/mcp_bridge/server.py | head -20"

# Find the actual WebSocket endpoint handler
adgn-properties snapshot exec ducktape/2025-11-20-00 -- \
  bash -c "grep -n 'async def.*websocket\|@.*websocket' adgn/src/adgn/agent/mcp_bridge/server.py | head -10"
```

## Root Cause Analysis

### The Auth Middleware

`create_management_ui_app()` installs `UITokenAuthMiddleware`:

```python
# From server.py lines 271-289 (approximate)
def create_management_ui_app(...):
    middleware = [
        Middleware(UITokenAuthMiddleware, token_table=token_table)
    ]
    # ...
    return Starlette(routes=routes, middleware=middleware)
```

### The Middleware Implementation

`UITokenAuthMiddleware` inherits from `BaseHTTPMiddleware`:

```python
# From auth.py lines 124-160 (approximate)
class UITokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Extract and validate bearer token
        token = self._extract_bearer_token(request)
        if not token or not self._validate_token(token):
            return Response("Unauthorized", status_code=401)
        return await call_next(request)
```

### Why WebSockets Bypass Auth

**Starlette's `BaseHTTPMiddleware` only processes HTTP requests:**

```python
# From Starlette's BaseHTTPMiddleware implementation
class BaseHTTPMiddleware:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # ← Bypasses dispatch!
            return

        # Only HTTP requests reach dispatch()
        request = Request(scope, receive)
        response = await self.dispatch(request, ...)
        await response(scope, receive, send)
```

**WebSocket handshakes use `scope['type'] == 'websocket'`**, so they skip the `dispatch()` method entirely and go
straight to the app.

### The WebSocket Handler

The WebSocket endpoint handler (presumably in `server.py`) accepts connections without checking authentication:

```python
# Typical pattern (actual code may vary)
@app.websocket_route("/ws/mcp")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()  # ← No auth check!
    # ... handle messages ...
```

### Attack Scenario

1. Attacker connects to `ws://server:port/ws/mcp`
2. WebSocket handshake bypasses `UITokenAuthMiddleware`
3. Handler calls `websocket.accept()` without checking token
4. Attacker has full access to management UI functionality

## Why This Happens

This is a common pitfall when using `BaseHTTPMiddleware`:

1. **Intuitive but wrong assumption:** "If I add auth middleware, all endpoints are protected"
2. **Reality:** `BaseHTTPMiddleware` only protects HTTP endpoints
3. **WebSocket handshakes are HTTP-like but use a different ASGI scope type**
4. **Result:** Silent auth bypass

## Solution Options

### Option 1: Manual Auth Check in WebSocket Handler (Simplest)

Check the token directly in the WebSocket endpoint before accepting:

```python
@app.websocket_route("/ws/mcp")
async def websocket_endpoint(websocket: WebSocket):
    # Extract token from headers
    token = None
    for header_name, header_value in websocket.headers.items():
        if header_name.lower() == 'authorization':
            if header_value.startswith('Bearer '):
                token = header_value[7:]
            break

    # Validate token
    if not token or token not in token_table:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    await websocket.accept()
    # ... handle messages ...
```

**Advantages:**

- ✅ Simple and explicit
- ✅ Clear that WebSocket needs special auth handling
- ✅ No middleware complexity

**Disadvantages:**

- ❌ Duplicates auth logic from middleware
- ❌ Easy to forget if adding more WebSocket endpoints

### Option 2: Pure ASGI Middleware (Most Robust)

Write a pure ASGI middleware that intercepts both HTTP and WebSocket:

```python
class TokenAuthMiddleware:
    """Pure ASGI middleware that handles both HTTP and WebSocket."""

    def __init__(self, app, token_table):
        self.app = app
        self.token_table = token_table

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Extract token from headers
        token = self._extract_token(scope["headers"])

        if not token or token not in self.token_table:
            if scope["type"] == "http":
                response = Response("Unauthorized", 401)
                await response(scope, receive, send)
            else:  # websocket
                # Send WebSocket close frame
                await send({
                    "type": "websocket.close",
                    "code": 1008,
                    "reason": "Unauthorized"
                })
            return

        # Token valid - forward to app
        await self.app(scope, receive, send)

    def _extract_token(self, headers):
        for name, value in headers:
            if name == b'authorization':
                value_str = value.decode('latin-1')
                if value_str.startswith('Bearer '):
                    return value_str[7:]
        return None
```

**Advantages:**

- ✅ Centralized auth logic for all endpoints
- ✅ Works for both HTTP and WebSocket
- ✅ Hard to bypass accidentally

**Disadvantages:**

- ❌ More complex than BaseHTTPMiddleware
- ❌ Lower-level ASGI API

### Option 3: WebSocket Dependency Injection (Starlette Pattern)

Use Starlette's dependency injection for WebSocket endpoints:

```python
from starlette.websockets import WebSocket
from starlette.exceptions import WebSocketException

async def verify_token(websocket: WebSocket) -> str:
    """Dependency that verifies token and returns it."""
    token = None
    for header_name, header_value in websocket.headers.items():
        if header_name.lower() == 'authorization':
            if header_value.startswith('Bearer '):
                token = header_value[7:]
            break

    if not token or token not in token_table:
        raise WebSocketException(code=1008, reason="Unauthorized")

    return token

# Usage
@app.websocket_route("/ws/mcp")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Depends(verify_token)  # ← Auth enforced here
):
    await websocket.accept()
    # token is validated and available
```

**Advantages:**

- ✅ Idiomatic Starlette pattern
- ✅ Reusable dependency
- ✅ Type-safe token access

**Disadvantages:**

- ❌ Starlette dependencies may not work with raw WebSocket routes
- ❌ Still requires remembering to add dependency to each endpoint

### Option 4: Custom WebSocket Decorator

Create a decorator that wraps WebSocket handlers with auth:

```python
from functools import wraps

def require_token(token_table):
    def decorator(func):
        @wraps(func)
        async def wrapper(websocket: WebSocket):
            # Extract and validate token
            token = extract_token_from_websocket(websocket)
            if not token or token not in token_table:
                await websocket.close(code=1008, reason="Unauthorized")
                return

            # Add token to websocket state for handler use
            websocket.state.token = token

            # Call original handler
            await func(websocket)

        return wrapper
    return decorator

# Usage
@app.websocket_route("/ws/mcp")
@require_token(token_table)
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # websocket.state.token is validated and available
```

**Advantages:**

- ✅ Reusable across endpoints
- ✅ Clear visual indicator of auth requirement
- ✅ DRY principle

**Disadvantages:**

- ❌ Decorator can be forgotten
- ❌ Additional indirection

## Recommendation

**Use Option 2 (Pure ASGI Middleware)** if you need comprehensive protection across all endpoints.

**Use Option 1 (Manual Check)** if you only have one or two WebSocket endpoints and want simplicity.

Reasoning:

- Pure ASGI middleware provides the most robust protection
- It's impossible to accidentally forget auth on a new WebSocket endpoint
- The complexity is worth it for security-critical auth boundaries
- Follows the same pattern as the MCP routing middleware analysis

## Security Impact

**Severity:** High

**Attack Vector:**

1. No authentication required
2. Direct WebSocket connection to management endpoint
3. Full access to management UI functionality

**Affected Scenarios:**

- Any deployment where the management UI is network-accessible
- Internal networks where not all users should have admin access
- Multi-tenant scenarios where token-based isolation is expected

**Mitigation:** Until fixed, restrict network access to the management UI port via firewall rules.

## Issue File Status

**Decision:** Skipping creation of the issue file for now (per user request).

When created, it should:

- Document the auth bypass clearly
- Explain why BaseHTTPMiddleware doesn't protect WebSockets
- NOT prescribe a specific solution
- Focus on the security impact and problem description
