# Compositor

Aggregates multiple MCP servers behind a single interface with tool namespacing and lifecycle management.

## API Conventions

### Mount Prefixes vs Server Names

- **Mount prefix** (`MCPMountPrefix`): owned by `x.agent_cli.compositor.prefix` and used within
  `agent_cli` to validate mount names. Tools are namespaced as `{prefix}_{tool}`. Use
  `build_mcp_function(prefix, tool)` from `mcp_infra.naming` to construct prefixed names. Shared
  code outside `agent_cli` passes mount names as strings.
- **Server name**: just `FastMCP(name=...)` metadata. Nothing outside the server should care.
- `ContainerExecServer.RUNTIME_MOUNT_PREFIX` is defined on the server class.

## Architecture

Usage: `async with Compositor() as comp`. Mount with `comp.mount_server(prefix, server)`, create client with `async with Client(comp) as client`.

Each mount encapsulates: `MountState` enum, `FastMCPProxy` (routing), persistent `Client` session (notifications), `AsyncExitStack` (owns resources like Docker containers).

### Resource Notifications

Per-mount child handlers capture resource notifications. The compositor records resource-list changes and passes resource-update events to registered listeners.

### Lifecycle

**CompositorState:** `CREATED -> ACTIVE -> CLOSED`
**MountState:** `PENDING -> ACTIVE/FAILED -> CLOSED`

Guarantees: cannot leak containers (AsyncExitStack), cannot double-enter (atomic state check under lock), cannot corrupt state (all mutations under `_mount_lock`).

## Implementation

- `x/agent_cli/compositor/server.py` -- Compositor implementation
- `x/agent_cli/compositor/mount.py` -- Mount class

Reference: <../../../mcp_infra/docs/fastmcp_lifecycle_analysis.md>
