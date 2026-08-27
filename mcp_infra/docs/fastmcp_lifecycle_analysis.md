# FastMCP in-process lifecycle cancellation finding

> **Status:** Historical regression warning preserved from the retired
> `x/agent_server` experiment. Re-verify the implementation after FastMCP or
> AnyIO upgrades before relying on the exact call path.

## Key finding

The pinned FastMCP version used by the experiment applied **aggressive
cancellation** when an in-process client disconnected.
`FastMCPTransport.connect_session()` entered the server lifespan inside an AnyIO
task group and unconditionally called `tg.cancel_scope.cancel()` from its
`finally` block. That parent cancel scope also covered lifespan teardown.

Consequently, an async `finally` block could start in an already-cancelled
scope: its next `await` raised `CancelledError`, so explicit cleanup of external
resources such as Docker containers never ran. Mounted in-process servers shared
the parent server's cancellation fate.

`asyncio.shield()` was not sufficient because it protects against asyncio task
cancellation, not an enclosing AnyIO cancel scope.

## Observed disconnect path

1. The FastMCP client signalled its in-process session runner to stop.
2. The transport exited its client-session context.
3. `connect_session()` cancelled its task group's cancel scope.
4. The server lifespan exited inside that cancelled scope.
5. The first awaited Docker lookup/stop/remove operation raised
   `CancelledError`, leaving the container running.

The historical reproducer involved a Docker-backed mounted server, but the
boundary was generic FastMCP lifecycle code rather than application-specific
logic.

## Design guidance

Critical external-resource cleanup must not assume that an async MCP lifespan
gets a graceful, uncancelled teardown window. Prefer, in order:

1. own the external resource outside the in-process transport/lifespan and clean
   it up from an uncancelled caller;
2. use an explicitly shielded AnyIO cancel scope with a bounded timeout, after
   proving the behavior with a disconnect test;
3. use a short synchronous cleanup fallback when leaking the resource is worse
   than briefly blocking the event loop.

Do not fire-and-forget cleanup into an unowned background task: process or event
loop shutdown can still abandon it.

## Regression test requirement

Any lifecycle helper that owns a container, subprocess, lease, or similar
external resource should have a test that disconnects the in-process client and
asserts the resource is gone before teardown returns. A setup/cleanup happy-path
test without forced disconnect does not cover this failure mode.
