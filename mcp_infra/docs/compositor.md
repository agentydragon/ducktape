# Compositor

Aggregates multiple MCP servers behind a single interface with tool namespacing, resource aggregation, and lifecycle management.

## API Conventions

### Mount Prefixes vs Server Names

- **Mount prefix** (`MCPMountPrefix`): tools namespaced as `{prefix}_{tool}`. ALWAYS use `MCPMountPrefix` type, NEVER raw strings. ALWAYS use `build_mcp_function(prefix, tool)` from `mcp_infra.naming` to construct prefixed names.
- **Server name**: just `FastMCP(name=...)` metadata. Nothing outside the server should care.
- Standard prefixes: `RESOURCES_MOUNT_PREFIX`, `COMPOSITOR_META_MOUNT_PREFIX`, `POLICY_READER_MOUNT_PREFIX`, `POLICY_PROPOSER_MOUNT_PREFIX` (from `mcp_infra.constants`). `ContainerExecServer.RUNTIME_MOUNT_PREFIX` is on the server class.
- Resource URIs: typed attributes on server classes (e.g., `CompositorMetaServer.server_state_resource`).

## Architecture

Usage: `async with Compositor() as comp`. Mount with `comp.mount_server(prefix, server)`, create client with `async with Client(comp) as client`.

**Pinned in-proc servers** (auto-mounted): `resources` (aggregates resources from all mounts), `compositor_meta` (metadata), `compositor_admin` (mount management, optional).

Each mount encapsulates: `MountState` enum, `FastMCPProxy` (routing), persistent `Client` session (notifications), `AsyncExitStack` (owns resources like Docker containers).

### Resource Notifications

Child servers emit `ResourceUpdatedNotification` -> per-mount child session -> `compositor_meta` mount listener -> compositor aggregated `ResourceUpdated` -> subscribed clients. Mount/unmount events trigger `ResourceListChangedNotification`.

### Lifecycle

**CompositorState:** `CREATED -> ACTIVE -> CLOSED`
**MountState:** `PENDING -> ACTIVE/FAILED -> CLOSED`

Guarantees: cannot leak containers (AsyncExitStack), cannot double-enter (atomic state check under lock), cannot corrupt state (all mutations under `_mount_lock`).

## Implementation

- `mcp_infra/src/mcp_infra/compositor/server.py` -- Compositor class
- `mcp_infra/src/mcp_infra/compositor/mount.py` -- Mount class

Reference: <fastmcp_lifecycle_analysis.md>
