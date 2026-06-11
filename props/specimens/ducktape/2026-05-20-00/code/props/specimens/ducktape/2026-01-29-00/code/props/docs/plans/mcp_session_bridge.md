# MCP Session Bridge: Unix Socket to HTTP SSE

## Overview

The MCP Session Bridge is a transparent forwarder that maintains a single persistent MCP session over HTTP SSE while allowing multiple ephemeral processes to connect via Unix domain socket.

**Problem it solves:** Agent code runs as ephemeral `docker_exec` subprocesses that can't maintain state between invocations. But we want:

- One persistent MCP session to the upstream server (avoiding re-initialization overhead)
- Background notification accumulation (while no subprocess is connected)
- Multiple sequential subprocess connections sharing the same session

**Solution:** A bridge process that:

1. Maintains one HTTP SSE connection to upstream MCP server
2. Exposes a Unix domain socket for local connections
3. Forwards JSON-RPC frames bidirectionally
4. Buffers notifications when no client is connected

## Architecture

```
Host Machine:
  MCP Server (HTTP SSE at host.docker.internal:54321)
       ↑
       │ persistent HTTP SSE connection
       │ (single MCP session)
       │
Container:
  Bridge Process (/tmp/mcp.sock)
       ↑
       │ Unix domain socket
       │ (ephemeral connections)
       │
  Agent Subprocess 1 ─→ connect, send request, read response, disconnect
  Agent Subprocess 2 ─→ connect, send request, read response, disconnect
  Agent Subprocess 3 ─→ connect, read notifications, disconnect
```

## Key Properties

- **Protocol-agnostic:** Bridge doesn't parse MCP semantics - just forwards frames
- **First client initializes:** First subprocess sends `initialize` with desired capabilities
- **Notification buffering:** Incoming notifications queued when no client connected
- **Buffer flush on connect:** Client gets all buffered notifications immediately
- **Sequential access:** Only one subprocess connected at a time (matches `docker_exec` pattern)
- **Transparent forwarding:** Upstream sees normal MCP client, subprocess sees normal MCP server

## Implementation

```python
import asyncio
import json
import os
from collections import deque
from pathlib import Path
from mcp.client.streamable_http import streamablehttp_client

class MCPSessionBridge:
    """Transparent forwarder between Unix socket and HTTP SSE MCP connection."""

    def __init__(self, upstream_url: str, token: str, socket_path: str):
        self.upstream_url = upstream_url
        self.token = token
        self.socket_path = Path(socket_path)
        self.notification_buffer = deque(maxlen=1000)
        self.current_client = None

    async def start(self):
        """Main bridge lifecycle - blocks until upstream closes"""
        async with streamablehttp_client(
            self.upstream_url,
            headers={"Authorization": f"Bearer {self.token}"}
        ) as (http_read, http_write, get_session_id):
            self.http_read = http_read
            self.http_write = http_write

            # Background: read upstream, forward or buffer
            asyncio.create_task(self._collect_from_upstream())
            await self._serve_unix_socket()

    async def _collect_from_upstream(self):
        """Read from upstream SSE. Forward to client or buffer notifications."""
        while True:
            frame_raw = await self.http_read()
            if frame_raw is None:
                break

            if self.current_client:
                self.current_client.write(frame_raw + b'\n')
                await self.current_client.drain()
            else:
                # Buffer notifications only (has 'method' but no 'id')
                try:
                    message = json.loads(frame_raw)
                    if "method" in message and "id" not in message:
                        self.notification_buffer.append(frame_raw)
                except json.JSONDecodeError:
                    pass

    async def _serve_unix_socket(self):
        """Serve Unix socket - handle client connections"""
        self.socket_path.unlink(missing_ok=True)

        async def handle_client(reader, writer):
            self.current_client = writer
            try:
                # Flush buffered notifications
                while self.notification_buffer:
                    writer.write(self.notification_buffer.popleft() + b'\n')
                    await writer.drain()

                # Forward client → upstream
                while line := await reader.readline():
                    await self.http_write(line.rstrip(b'\n'))
            finally:
                self.current_client = None
                writer.close()

        server = await asyncio.start_unix_server(handle_client, path=str(self.socket_path))
        async with server:
            await server.serve_forever()


async def main():
    bridge = MCPSessionBridge(
        upstream_url=os.environ['MCP_SERVER_URL'],
        token=os.environ['MCP_SERVER_TOKEN'],
        socket_path=os.getenv('MCP_SOCKET_PATH', '/tmp/mcp.sock'),
    )
    await bridge.start()

if __name__ == '__main__':
    asyncio.run(main())
```

## Usage

### Starting the Bridge

```bash
export MCP_SERVER_URL="http://host.docker.internal:54321"
export MCP_SERVER_TOKEN="secret-token-here"
python -m mcp_bridge &
```

### Client Usage (from subprocesses)

First subprocess initializes the session, subsequent ones reuse it:

```python
import socket, json

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect('/tmp/mcp.sock')

# Send any MCP request (initialize on first call, tools/call later, etc.)
sock.sendall(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", ...}).encode() + b'\n')
response = json.loads(sock.recv(65536).decode())

sock.close()
```

On connect, buffered notifications are flushed immediately before any request/response.

## When to Use This Pattern

**Use the bridge when:**

- Agent runs as ephemeral subprocesses (e.g., `docker_exec`)
- Need background notification accumulation
- Want to avoid re-initialization overhead
- Want to maintain subscriptions across subprocess invocations

**Don't use the bridge when:**

- Agent can maintain long-lived Python process
- No need for notifications between invocations
- Single-shot tool calls only (direct MCP connection is simpler)

## Design Decisions

### Why Unix Socket?

- Zero network overhead (in-kernel IPC)
- No port conflicts (named path)
- File permissions for auth
- Works with any language

### Why Buffer Notifications?

Without buffering, clients miss updates while disconnected and need server-side re-subscription on every connect.

### Why Protocol-Agnostic?

Simpler, future-proof, lower latency. Only protocol knowledge needed: distinguish notifications (buffer) from responses (forward immediately).

## Container Integration

```dockerfile
COPY mcp_bridge.py /usr/local/bin/mcp-bridge
RUN chmod +x /usr/local/bin/mcp-bridge
```

```yaml
environment:
  - MCP_SERVER_URL=http://host.docker.internal:54321
  - MCP_SERVER_TOKEN=secret-token
  - MCP_SOCKET_PATH=/tmp/mcp.sock
```

## Future Enhancements

- Multiple concurrent clients (track request IDs for routing)
- Auto-reconnect on upstream connection loss
- Notification TTL (expire old notifications)
- Metrics (request counts, buffer size)
