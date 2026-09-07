"""Drive the client-disconnect sequence behind the StreamableHTTPSessionManager wedge.

`mcp==1.29.0`/`fastmcp==3.4.4`'s `StreamableHTTPSessionManager` tears down its one shared
task group permanently the first time any per-request task raises an uncaught
`CancelledError` (e.g. a client disconnecting mid-request) — its own `except Exception`
does not catch `BaseException`-derived `CancelledError`. Every later request to that
server instance then fails until the process restarts (upstream
`modelcontextprotocol/python-sdk#820`, `#1219`; ducktape issue #5786).

`cancel_request_mid_flight_then_retry` drives that sequence — cancel an in-flight call,
then issue a fresh one against the same server instance — over a real HTTP socket
(loopback), and hands back the follow-up request's result for a caller to assert on. It
intentionally carries no assertion itself: whether *this* clean, same-process,
loopback-socket cancellation actually reproduces the task-group teardown is timing- and
transport-dependent, and wasn't reliably reproducible this way in ad hoc local testing
(nor was `modelcontextprotocol/python-sdk#820`'s literal malformed-arguments trigger,
which the currently pinned SDK already converts into a clean JSON-RPC error response
rather than an uncaught exception). Use this to assert the follow-up keeps succeeding
once migrated to the SDK version that removes the shared task group architecturally
(see #5786); it is not by itself proof the wedge was live on the old SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

import anyio
from fastmcp import Client, FastMCP
from fastmcp.client.client import CallToolResult

from mcp_infra.testing.remote_server import as_remote_server

# Long enough that the test driver can reliably cancel the call before it would ever
# return on its own; the call is always cancelled well before this elapses.
_SLOW_TOOL_SECONDS = 3600.0


def make_wedge_repro_server(*, name: str = "wedge_repro") -> tuple[FastMCP, threading.Event]:
    """Build the two-tool server this repro needs, plus a signal for when `slow` starts.

    `serve_app`/`as_remote_server` run the server under uvicorn in its own thread with
    its own event loop, so the returned `threading.Event` (not `asyncio.Event`) is what
    lets the test driver wait for the in-flight request to have actually reached the
    server before cancelling it, instead of guessing with a fixed sleep.
    """
    server = FastMCP(name)
    slow_call_started = threading.Event()

    @server.tool()
    async def slow() -> str:
        slow_call_started.set()
        await anyio.sleep(_SLOW_TOOL_SECONDS)
        return "done"

    @server.tool()
    async def ping() -> str:
        return "pong"

    return server, slow_call_started


async def cancel_request_mid_flight_then_retry(server: FastMCP, slow_call_started: threading.Event) -> CallToolResult:
    """Serve `server`, cancel a call to its `slow` tool mid-flight, then call `ping`.

    Returns `ping`'s result from a fresh client against the same server instance. See the
    module docstring for what this is (and isn't) known to demonstrate on the currently
    pinned SDK.
    """
    async with as_remote_server(server) as spec, Client(spec.url) as client:
        call = asyncio.ensure_future(client.call_tool("slow", {}))
        await asyncio.to_thread(slow_call_started.wait, 10)
        if not slow_call_started.is_set():
            raise TimeoutError("slow tool never started executing on the server")
        call.cancel()
        with contextlib.suppress(BaseException):
            await call

        async with Client(spec.url) as retry_client:
            result: CallToolResult = await retry_client.call_tool("ping", {})
            return result
