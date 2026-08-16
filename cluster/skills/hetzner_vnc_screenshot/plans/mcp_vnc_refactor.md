# Sketch: Hetzner VNC → MCP VNC Refactor

Sketch for replacing the bespoke `WebSocketStreamAdapter` with standard
components (unwebsockify + an off-the-shelf VNC MCP server) for full desktop
control. Not scheduled for implementation.

The adapter it replaces is still in `../vnc_screenshot.py`, so the sketch is
live, but the cluster is no longer a consumer: its Hetzner Cloud fleet was
retired in 2026-05 (<../../../archive/2026_05_28_hcloud_retirement.md>). Weigh
the work against whatever non-cluster Hetzner use remains. The MCP-server
comparison below is a 2026-07 market snapshot and should be re-checked before
being acted on.

---

## Current State

The current implementation (`vnc_screenshot.py`):

1. Calls Hetzner API to get WebSocket URL + one-time password
2. Connects to the WebSocket directly
3. Uses a custom `WebSocketStreamAdapter` (~60 LOC) to bridge WebSocket message-oriented semantics to stream-oriented interface that asyncvnc expects
4. Takes a screenshot via asyncvnc

The custom adapter is inelegant - it buffers WebSocket messages and implements `readline()`, `read(n)`, `write()`, `drain()` to mimic asyncio.StreamReader/StreamWriter.

---

## Research: VNC MCP Servers

### Summary

| Implementation           | WebSocket | Dynamic Target | Notes                                        |
| ------------------------ | :-------: | :------------: | -------------------------------------------- |
| signal-slot/mcp-vnc (Qt) |    ❌     |       ✅       | Has `connect(host, port, password)` tool     |
| mcp-vnc (hrrrsn)         |    ❌     |       ❌       | Env vars only at config time                 |
| volkan-m/vnc-mcp-server  |    ❌     |       ✅       | Has `vnc_connect(host, port, password)` tool |
| mcvnc (PyPI)             |    ❌     |       ✅       | Has `vnc_connect(host, port, password)` tool |

**Key finding**: None of the VNC MCP servers natively support WebSocket. All require a TCP connection. This is why unwebsockify is needed.

### Details

#### signal-slot/mcp-vnc

- **Language**: C++ / Qt6
- **Binary**: Yes (pre-built for Linux/macOS/Windows)
- **Tools**: connect, screenshot, mouseMove, mouseClick, sendText, setPreview (live window)
- **Dynamic Target**: ✅ Has `connect` tool

#### volkan-m/vnc-mcp-server

- **Language**: Node.js
- **Install**: `npx -y volkan-m/vnc-mcp-server`
- **Tools**: connect, screenshot, mouse, sendText, OCR, template matching, SSH execution
- **Dynamic Target**: ✅ Has `vnc_connect` tool

#### mcvnc (PyPI)

- **Language**: Python
- **Install**: `pip install mcvnc` or `uvx mcvnc`
- **Tools**: connect, screenshot, screenshot_region, click, type_text, clipboard, wait_for_screen_change
- **Dynamic Target**: ✅ Has `vnc_connect` tool

---

## Proposed Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│  SKILL: hetzner_vnc                                            │
│  ┌─────────────────┐    ┌──────────────┐    ┌───────────┐ │
│  │ 1. hcloud API │ -> │ 2. WS→TCP  │ -> │3. mcp-vnc │ │
│  │ request_con-   │    │ proxy      │    │ standard │ │
│  │ sole()       │    │ unwebsockify │    │ VNC      │ │
│  │             │    │           │    │ client  │ │
│  │ returns:     │    │ forwards  │    │         │ │
│  │ wss_url,    │    │ bytes    │    │         │ │
│  │ password    │    │ WS↔TCP   │    │         │ │
│  └─────────────────┘    └──────────────┘    └───────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Component Breakdown

| Component    | Role                         | Source                               |
| ------------ | ---------------------------- | ------------------------------------ |
| hcloud SDK   | Call `request_console()` API | hcloud-python (existing)             |
| WS→TCP proxy | Bridge WebSocket to TCP      | unwebsockify (jimparis/unwebsockify) |
| VNC client   | Standard MCP server          | signal-slot/mcp-vnc (recommended)    |

> **Note**: "unwebsockify" is the reverse of websockify - accepts TCP and connects to a WebSocket server. This is exactly our use case.
>
> **Important**: May need `--subproto binary` flag since Hetzner uses WebSocket binary subprotocol.

### Protocol Sequence

```text
1. hcloud API
   client.servers.request_console(server)
   Returns: { wss_url, password }

2. Start WS→TCP proxy (unwebsockify)
   - Listen on localhost:PORT (default 5900 or auto-allocate)
   - Connect to wss_url using password
   - Forward: TCP ↔ WebSocket (raw byte forwarding, no RFB parsing)

3. mcp-vnc connect
   mcp_vnc.connect(host="localhost", port=PORT, password=PASSWORD)
   - Uses all standard VNC tools: screenshot, mouseClick, sendText, etc.
```

### Why This Works

- **Hetzner WebSocket already speaks RFB**: The WSS endpoint expects RFB protocol, just wrapped in WebSocket frames
- **Proxy is simple byte-forwarding**: No RFB protocol parsing, auth handling, or state needed
- **MCP VNC servers already exist**: Pre-built binaries, expose MCP tools

---

## Benefits

| Aspect               | Current                       | Proposed                      |
| -------------------- | ----------------------------- | ----------------------------- |
| Lines of custom code | ~200 LOC                      | ~30 LOC (wrapper only)        |
| Proxy needed         | custom WebSocketStreamAdapter | unwebsockify (existing)       |
| VNC tooling          | bespoke screenshot            | full desktop control          |
| Maintainability      | custom adapter                | standard components           |
| Extensibility        | one-shot tool                 | MCP tools (click, type, etc.) |

---

## Implementation

### New Files

```text
skills/hetzner_vnc_screenshot/
├── SKILL.md                      # existing
├── vnc_screenshot.py            # keep for backwards compatibility
├── PLAN.md                       # this file
├── hetzner_vnc_proxy.py         # NEW: wrapper that calls unwebsockify
├── unwebsockify.py             # vendored: jimparis/unwebsockify
└── mcp_vnc                     # signal-slot/mcp-vnc binary
```

### Using unwebsockify

```bash
# Usage: unwebsockify [options] URL
# Example:
unwebsockify --port 5900 wss://web-console.hetzner.cloud/?server_id=XXX&token=XXX
```

### Wrapper Script (`hetzner_vnc_proxy.py`)

```python
#!/usr/bin/env python3
"""Wrapper around unwebsockify for Hetzner VNC console."""

import subprocess
import sys
from hcloud import Client

def main():
    server_name = sys.argv[1] if len(sys.argv) > 1 else input("Server name: ")
    token = os.environ.get("HCLOUD_TOKEN")

    # 1. Get console credentials from Hetzner API
    client = Client(token=token)
    server = client.servers.get_by_name(server_name)
    response = client.servers.request_console(server[0])

    # 2. Print password for user
    print(f"VNC password: {response.password}")
    print(f"Connect mcp-vnc to localhost:5900 with this password")

    # 3. Start unwebsockify
    subprocess.run(["unwebsockify", "--port", "5900", response.wss_url])
```

### Usage Flow

```bash
# 1. Start proxy (in background)
python hetzner_vnc_proxy.py my-hetzner-server

# 2. In MCP client, use mcp-vnc tools:
# - connect(host="localhost", port=5900, password=<from_step1>)
# - screenshot()
# - mouseClick(x=100, y=200)
# - sendText("hello world")
```

---

## Test Plan

1. Find a test Hetzner server
2. Run `python hetzner_vnc_proxy.py <server>` - gets credentials, starts proxy
3. Connect using mcp-vnc binary with localhost:5900 and password
4. Verify screenshot, mouse, keyboard tools work
5. Clean up proxy when done

---

## Open Questions

1. **Port conflicts**: 5900 is common - use auto-allocation?
2. **One proxy per server**: Multiple proxies for multiple servers?
3. **Proxy lifecycle**: Kill on client disconnect, or keep running?
4. **MCP server management**: How does skill start/stop mcp-vnc? (stdio vs process)
