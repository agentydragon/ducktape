# Compositor

The compositor aggregates multiple MCP servers behind a single interface, handling tool namespacing, resource aggregation, and lifecycle management.

## API Conventions

### Mount Prefixes vs Server Names

**Mount prefix** (`MCPMountPrefix`): Used when mounting a server on a compositor. Tools are namespaced as `{prefix}_{tool}` (but NEVER construct this yourself - see below). Use mount prefixes when:

- Constructing bootstrap tool use sequences
- Writing test fixtures/matchers for tool calls
- Any code that acts as a stand-in for or checks behavior of an LLM agent (which sees prefixed tool names)

**Server name**: Implementation detail passed to `FastMCP(name=...)` constructor. Just metadata - nothing outside the server should care about it. Pass as inline literal.

**Rule: ALWAYS use `MCPMountPrefix` for mount prefixes, NEVER strings.**

Standard prefixes from `mcp_infra.constants`:

- `RESOURCES_MOUNT_PREFIX`, `RUNTIME_MOUNT_PREFIX`, `COMPOSITOR_META_MOUNT_PREFIX`
- `POLICY_READER_MOUNT_PREFIX`, `POLICY_PROPOSER_MOUNT_PREFIX`
- `SEATBELT_EXEC_MOUNT_PREFIX`

**Rule: NEVER construct prefixed tool names inline** (e.g., `prefix + "_" + tool`). ALWAYS use `build_mcp_function(prefix, tool)` from `mcp_infra.naming` - it is the single source of truth for the namespacing logic.

Resource URIs are exposed as typed attributes on server classes (e.g., `CompositorMetaServer.server_state_resource`).

## Architecture

The compositor runs inside an agent process. Usage: `async with Compositor() as comp`. Mount servers with `comp.mount_server(prefix, server)`, then create an MCP client with `async with Client(comp) as client` and pass it to the agent.

**Mounted servers** have a lifecycle (mount → unmount) and include a FastMCPProxy for routing, a persistent client session for notifications, and an AsyncExitStack that owns resources like Docker containers.

**Pinned in-proc servers** are mounted at start and never unmounted:

- `resources` - aggregates resources from all mounted servers
- `compositor_meta` - provides metadata about mounted servers
- `compositor_admin` - mount management (optional)

### Component Structure

```
Compositor
  └─> _mounts: dict[MCPMountPrefix, Mount]
       └─> Each Mount encapsulates:
            ├─> State: MountState enum (PENDING/ACTIVE/FAILED/CLOSED)
            ├─> Proxy: FastMCPProxy (routing layer)
            ├─> Child client: Client (persistent session)
            └─> Stack: AsyncExitStack
                 └─> Owns: stdio processes, HTTP clients, Docker containers
```

### Resource Notifications

```
Child Server (runtime, etc.)
  └─> ResourceUpdatedNotification(uri="resource://...")
       └─> Child Session (per-mount)
            └─> compositor_meta's mount listener
                 └─> Compositor aggregated ResourceUpdated
                      └─> Client sessions (subscribers)
```

**Components:**

1. **Child Server Sessions** - Each mount has a persistent Client session to the child server that listens for ResourceUpdated notifications.

2. **Compositor Mount Listener** - Listens for `MountEvent.MOUNTED` and `MountEvent.UNMOUNTED`, broadcasts `ResourceListChangedNotification`.

3. **Resource Aggregation** - `resources` server aggregates `list_resources()` across all mounted servers; `compositor_meta` provides metadata resources per server.

**Key Invariant:** Resource notifications propagate from child servers → compositor → subscribed clients. The compositor forwards notifications from child servers and broadcasts ResourceListChanged when mounts change.

## Lifecycle

The compositor uses async context manager protocol for resource management:

```python
async with Compositor() as comp:
    await comp.mount_server(prefix, server)
    # ... use compositor ...
# All mounts cleaned up automatically
```

### State Machines

**CompositorState:** `CREATED → ACTIVE → CLOSED`

- CREATED: Constructed but not entered
- ACTIVE: Inside `async with` block
- CLOSED: Cleanup completed (terminal)

**MountState:** `PENDING → ACTIVE/FAILED → CLOSED`

- PENDING: Created but not initialized
- ACTIVE: Ready for use
- FAILED: Initialization failed
- CLOSED: Cleanup completed

### Guarantees

1. **Cannot leak containers** - AsyncExitStack ensures cleanup on all paths
2. **Cannot double-enter** - Atomic state check under lock
3. **Cannot corrupt state** - All mutations under `_mount_lock`

## Implementation

Files:

- `mcp_infra/src/mcp_infra/compositor/server.py` - Compositor class
- `mcp_infra/src/mcp_infra/compositor/mount.py` - Mount class

Reference: <fastmcp-lifecycle-analysis.md>
