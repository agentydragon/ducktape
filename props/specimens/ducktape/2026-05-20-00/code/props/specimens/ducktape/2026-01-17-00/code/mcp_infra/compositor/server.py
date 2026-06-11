from __future__ import annotations

import asyncio
import base64
import logging
import sys
import warnings
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum, auto

# Import will be circular at module load, so use TYPE_CHECKING
from typing import TYPE_CHECKING, TypeVar

from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import ClientTransport, StdioTransport, StreamableHttpTransport
from fastmcp.mcp_config import (
    MCPConfig,
    MCPServerTypes,
    RemoteMCPServer,
    StdioMCPServer,
    TransformingRemoteMCPServer,
    TransformingStdioMCPServer,
)
from fastmcp.server import FastMCP
from mcp import types as mcp_types
from pydantic import ValidationError

from mcp_infra.compositor.meta_server import CompositorMetaServer
from mcp_infra.compositor.mount import Mount
from mcp_infra.compositor.rendering import render_compositor_instructions
from mcp_infra.compositor.resources_server import ResourcesServer
from mcp_infra.constants import COMPOSITOR_META_MOUNT_PREFIX, RESOURCES_MOUNT_PREFIX
from mcp_infra.mount_types import MountEvent
from mcp_infra.mounted import Mounted
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.snapshots import (
    FailedServerEntry,
    InitializingServerEntry,
    RunningServerEntry,
    SamplingSnapshot,
    ServerEntry,
)
from mcp_infra.tool_schemas import extract_tool_input_schemas, extract_tool_schemas

if TYPE_CHECKING:
    from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=FastMCP)


class ChildNotificationHandler(MessageHandler):
    """Message handler that forwards notifications to compositor with origin attribution."""

    def __init__(self, compositor: Compositor, server_prefix: MCPMountPrefix) -> None:
        self._compositor = compositor
        self._server_prefix = server_prefix

    async def on_resource_list_changed(self, message: mcp_types.ResourceListChangedNotification) -> None:
        self._compositor._pending_resource_list_changes.add(self._server_prefix)
        # No forwarding here; child client handles forwarding via proxy
        await self._compositor._notify_resource_list_change(self._server_prefix)

    async def on_resource_updated(self, message: mcp_types.ResourceUpdatedNotification) -> None:
        # Forward to listeners with origin attribution
        await self._compositor._notify_resource_updated(self._server_prefix, str(message.params.uri))


class CompositorState(Enum):
    """Compositor lifecycle states.

    Transitions:
    - CREATED → ACTIVE (on first __aenter__)
    - ACTIVE → CLOSED (on __aexit__)
    - CREATED/ACTIVE → CLOSED (on explicit close())

    Invalid transitions:
    - ACTIVE → ACTIVE (double-enter, raises RuntimeError)
    - CLOSED → anything (closed is terminal)
    """

    CREATED = auto()  # Constructed but not entered
    ACTIVE = auto()  # Inside async with block
    CLOSED = auto()  # Cleanup completed, terminal state


class Compositor(FastMCP):
    """Aggregates upstream MCP servers under a single FastMCP surface.

    MUST be used as async context manager:
        async with Compositor() as comp:
            await comp.mount_inproc(RUNTIME_MOUNT_PREFIX, runtime_server)
            async with Client(comp) as client:
                from mcp_infra.naming import build_mcp_function
                result = await client.call_tool(
                    build_mcp_function("runtime", "exec"), {"command": ["ls"]}
                )
        # All non-pinned servers cleaned up here

    Features:
    - Namespaces tools as {server}_{tool} (use build_mcp_function(server, tool) helper)
    - Reuses persistent upstream sessions per mount
    - Relays resource updates as notifications
    - Exception-safe mount/unmount (no leaks on failure)
    - Pinned servers (e.g., compositor_meta) persist through close()
    - Auto-mounts infrastructure servers (resources, compositor_meta) on __aenter__

    Common Patterns:

    1. Short-lived script:
        async with Compositor() as comp:
            await comp.mount_inproc(RUNTIME_MOUNT_PREFIX, RuntimeServer(...))
            async with Client(comp) as client:
                agent = await Agent.create(mcp_client=client)
                await agent.run("review this code")

    2. Long-lived server:
        stack = AsyncExitStack()
        comp = Compositor()
        await stack.enter_async_context(comp)  # Adds to parent stack
        await comp.mount_servers_from_config(config)
        # Later: await stack.aclose() cleans up compositor

    3. With pinned servers:
        async with Compositor() as comp:
            await comp.mount_inproc(RESOURCES_MOUNT_PREFIX, resources_server, pinned=True)
            await comp.mount_inproc(RUNTIME_MOUNT_PREFIX, runtime_server)  # Not pinned
            # On exit: runtime unmounted, resources stays

    Safeguards (raises RuntimeError/ValueError):
    - Cannot double-enter same compositor
    - Cannot reuse closed compositor
    - Cannot mount/unmount after close
    - Cannot mount duplicate server names
    - Cannot unmount pinned servers
    - Invalid server names (must match ^[a-z][a-z0-9_]*$)
    - __del__ warning if leaked (container leak detection)

    See also:
    - Mount class for per-server lifecycle
    - docs/compositor.md for architecture and exception safety proofs
    """

    # Infrastructure servers (mounted automatically in __aenter__, always pinned)
    resources: Mounted[ResourcesServer]
    compositor_meta: Mounted[CompositorMetaServer]

    def __init__(
        self, name: str = "compositor", *, instructions: str | None = None, version: str | None = None
    ) -> None:
        # Pass explicit version to avoid importlib.metadata.version() lookup which can hang under pytest-xdist
        super().__init__(name=name, instructions=instructions, version=version)

        # State machine (replaces _context_manager_entered/_context_manager_exited)
        self._state = CompositorState.CREATED
        self._state_lock = asyncio.Lock()

        # Mounts and listeners
        self._mounts: dict[MCPMountPrefix, Mount] = {}
        self._mount_lock = asyncio.Lock()
        self._mount_listeners: list[Callable[[MCPMountPrefix, MountEvent], Awaitable[None] | None]] = []

        # Resource change tracking
        self._pending_resource_list_changes: set[MCPMountPrefix] = set()
        self._resource_list_change_listeners: list[Callable[[MCPMountPrefix], Awaitable[None] | None]] = []
        self._resource_updated_listeners: list[Callable[[MCPMountPrefix, str], Awaitable[None] | None]] = []

        # Compositor metadata resources are exposed via the separate 'compositor_meta' server.

    # ---- Public child client accessor -------------------------------------

    def add_mount_listener(self, cb: Callable[[MCPMountPrefix, MountEvent], Awaitable[None] | None]) -> None:
        """Register a callback invoked on mount lifecycle changes.

        Callback signature: (name: str, action: MountEvent) where action is one of
        MountEvent.MOUNTED | MountEvent.UNMOUNTED | MountEvent.STATE.
        """
        self._mount_listeners.append(cb)

    async def _notify_mount_listeners(self, prefix: MCPMountPrefix, action: MountEvent) -> None:
        for cb in list(self._mount_listeners):
            res = cb(prefix, action)
            if asyncio.iscoroutine(res):
                await res

    def add_resource_list_change_listener(self, cb: Callable[[MCPMountPrefix], Awaitable[None] | None]) -> None:
        """Register a callback invoked when a child reports resources/list_changed.

        Callback signature: (name: MCPMountPrefix) where name is the origin server.
        """
        self._resource_list_change_listeners.append(cb)

    async def _notify_resource_list_change(self, prefix: MCPMountPrefix) -> None:
        for cb in list(self._resource_list_change_listeners):
            res = cb(prefix)
            if asyncio.iscoroutine(res):
                await res

    def add_resource_updated_listener(self, cb: Callable[[MCPMountPrefix, str], Awaitable[None] | None]) -> None:
        """Register a callback invoked when a child reports resources/updated.

        Callback signature: (name: str, uri: str) where name is the origin
        server and uri is the raw (unprefixed) resource URI from the child.
        """
        self._resource_updated_listeners.append(cb)

    async def _notify_resource_updated(self, prefix: MCPMountPrefix, uri: str) -> None:
        for cb in list(self._resource_updated_listeners):
            res = cb(prefix, uri)
            if asyncio.iscoroutine(res):
                await res

    # ---- Child notifications capture (origin attribution) -----------------

    def pop_recent_resource_list_changes(self) -> list[MCPMountPrefix]:
        """Return and clear servers that recently reported resource list changes."""
        names = sorted(self._pending_resource_list_changes)
        self._pending_resource_list_changes.clear()
        return names

    async def server_entries(self) -> dict[MCPMountPrefix, ServerEntry]:
        """Return per-child status entries keyed by child name.

        Entries are discriminated-union ServerEntry values keyed by mount name.
        """
        # Phase 1: capture init results and schedule tool enumeration concurrently
        async with self._mount_lock:
            items = list(self._mounts.items())
        per_name: dict[MCPMountPrefix, ServerEntry] = {}
        tool_tasks: dict[MCPMountPrefix, asyncio.Task[list[mcp_types.Tool]]] = {}

        for name, mount in items:
            # Check mount state
            if mount.is_failed:
                exc = mount.exception
                error_msg = str(exc) if exc else "Mount failed"
                per_name[name] = FailedServerEntry(error=error_msg)
                continue

            if not mount.is_active:
                per_name[name] = InitializingServerEntry()
                continue

            try:
                # Get initialize result from child client
                client = mount.child_client
                init = client.initialize_result

                # If we don't have init result, that's a failure
                if init is None:
                    per_name[name] = FailedServerEntry(error="No initialize result available")
                    continue

                # Schedule list_tools via proxy client for parallel enumeration
                async def _list_tools_via_client(cf):
                    cli = cf()
                    async with cli:
                        return await cli.list_tools()

                tool_tasks[name] = asyncio.create_task(_list_tools_via_client(mount.proxy.client_factory))
                per_name[name] = RunningServerEntry(initialize=init, tools=[])
            except Exception as e:
                per_name[name] = FailedServerEntry(error=f"{type(e).__name__}: {e}")

        # Phase 2: resolve tool enumeration in parallel with structured concurrency
        async def _handle_tools(prefix: MCPMountPrefix, task: asyncio.Task, entry: RunningServerEntry):
            try:
                tools = await task
                per_name[prefix] = RunningServerEntry(initialize=entry.initialize, tools=tools)
            except Exception as e:
                per_name[prefix] = FailedServerEntry(error=f"{type(e).__name__}: {e}")

        async with asyncio.TaskGroup() as tg:
            for name, task in tool_tasks.items():
                entry = per_name[name]
                assert isinstance(entry, RunningServerEntry), (
                    f"Expected RunningServerEntry for {name}, got {type(entry)}"
                )
                tg.create_task(_handle_tools(name, task, entry))

        return per_name

    async def sampling_snapshot(self) -> SamplingSnapshot:
        """Return a SamplingSnapshot mirroring the manager's shape, aggregated over children."""
        entries_map = await self.server_entries()
        return SamplingSnapshot(ts=datetime.now(UTC).isoformat(), servers=entries_map)

    async def mount_specs(self) -> dict[str, MCPServerTypes]:
        """Return a snapshot of current mount specs keyed by name.

        Only includes spec-based mounts; in-process mounts (spec=None) are excluded.
        """
        async with self._mount_lock:
            return {k: v.spec for k, v in self._mounts.items() if v.spec is not None}

    async def get_inproc_servers(self) -> dict[MCPMountPrefix, FastMCP]:
        """Get all mounted in-process servers.

        Returns:
            Dict mapping mount prefix to FastMCP server instance.
            Only includes in-process servers (external mounts excluded).
        """
        async with self._mount_lock:
            return {
                prefix: mount.inproc_server for prefix, mount in self._mounts.items() if mount.inproc_server is not None
            }

    async def extract_tool_input_schemas(self) -> dict[tuple[MCPMountPrefix, str], type[BaseModel]]:
        """Extract tool input schemas from all mounted in-process servers.

        Returns:
            Dict mapping (server_prefix, tool_name) to Pydantic input model type.
            Only includes tools with Pydantic BaseModel input annotations.
        """
        servers = await self.get_inproc_servers()
        return extract_tool_input_schemas(servers)

    async def extract_tool_schemas(self) -> dict[tuple[MCPMountPrefix, str], type[BaseModel]]:
        """Extract tool output schemas from all mounted in-process servers.

        Returns:
            Dict mapping (server_prefix, tool_name) to Pydantic output model type.
            Only includes tools with Pydantic BaseModel return annotations.
        """
        servers = await self.get_inproc_servers()
        return extract_tool_schemas(servers)

    async def render_agent_dynamic_instructions(self) -> str:
        """Render the MCP instructions banner for agent dynamic_instructions.

        Returns grouped MCP server instructions/capabilities using the same
        template as the UI server. Use this as the dynamic_instructions callback
        for agents that connect to this compositor.

        Example:
            async with Compositor() as comp:
                await comp.mount_inproc(RUNTIME_MOUNT_PREFIX, runtime_server)
                async with Client(comp) as mcp_client:
                    agent = await Agent.create(
                        mcp_client=mcp_client,
                        client=client,
                        dynamic_instructions=comp.render_agent_dynamic_instructions,
                    )
        """
        states = await self.server_entries()
        return render_compositor_instructions(states)

    # ---- Management API (Python-only) --------------------------------------

    async def _mount_common(self, prefix: MCPMountPrefix, mount: Mount) -> None:
        """Common mounting logic after Mount object is created and setup.

        Args:
            prefix: Mount prefix (already validated)
            mount: Mount object (already setup)
        """
        # Register the mount (under lock)
        async with self._mount_lock:
            self._mounts[prefix] = mount

        # Mount proxy on FastMCP surface
        if mount.is_active:
            self.mount(mount.proxy, prefix=prefix)
            await self._notify_mount_listeners(prefix, MountEvent.STATE)
            await self._notify_mount_listeners(prefix, MountEvent.MOUNTED)
        else:
            # Mount failed but is registered (for status reporting)
            await self._notify_mount_listeners(prefix, MountEvent.STATE)

    async def mount_server(self, name: str, spec: MCPServerTypes, *, pinned: bool = False) -> None:
        """Mount server from MCP config (stdio, HTTP, etc).

        Exception-safe: if mount fails, no server is registered and no resources leak.

        Args:
            name: Server name (used in tool prefixes: {name}_{tool})
            spec: Server configuration (StdioMCPServer, RemoteMCPServer, etc)
            pinned: If True, server won't be unmounted on close()

        Raises:
            RuntimeError: If state is CLOSED
            ValueError: If name is invalid or already mounted
        """
        # Check state
        async with self._state_lock:
            if self._state == CompositorState.CLOSED:
                raise RuntimeError(f"Cannot mount server - compositor '{self.name}' is closed")

        # Validate mount prefix (MCPMountPrefix constructor validates automatically)
        try:
            validated_prefix = MCPMountPrefix(name)
        except ValidationError as e:
            error_msg = e.errors()[0]["msg"] if e.errors() else str(e)
            raise ValueError(f"Invalid mount prefix {name!r}: {error_msg}") from e

        # Check for duplicate under lock
        async with self._mount_lock:
            if validated_prefix in self._mounts:
                raise ValueError(f"Server '{validated_prefix}' is already mounted")

        # Create mount and setup (exception-safe internally)
        mount = Mount(prefix=validated_prefix, pinned=pinned, spec=spec)
        await mount.setup_external(spec, self._fm_transport_from_spec, lambda n: ChildNotificationHandler(self, n))

        # Register and notify
        await self._mount_common(validated_prefix, mount)

    async def mount_inproc(self, prefix: MCPMountPrefix, server: T, *, pinned: bool = False) -> Mounted[T]:
        """Mount in-process FastMCP server and return Mounted wrapper.

        Exception-safe: if mount fails, no server is registered and no resources leak.

        Args:
            prefix: Mount prefix for tool namespacing ({prefix}_{tool})
            server: FastMCP server instance
            pinned: If True, server won't be unmounted on close()

        Returns:
            Mounted[T] wrapper bundling prefix + server

        Raises:
            RuntimeError: If state is CLOSED
            ValueError: If prefix is already mounted

        Example:
            self.runtime = await self.mount_inproc(RUNTIME_MOUNT_PREFIX, ContainerExecServer(...), pinned=True)
        """
        # Check state
        async with self._state_lock:
            if self._state == CompositorState.CLOSED:
                raise RuntimeError(f"Cannot mount server - compositor '{self.name}' is closed")

        # Check for duplicate under lock
        async with self._mount_lock:
            if prefix in self._mounts:
                raise ValueError(f"Server '{prefix}' is already mounted")

        # Create mount and setup (exception-safe internally)
        mount = Mount(prefix=prefix, pinned=pinned, spec=None)
        await mount.setup_inproc(server, lambda n: ChildNotificationHandler(self, n))

        # Register and notify
        await self._mount_common(prefix, mount)

        # Return Mounted wrapper
        return Mounted(prefix=prefix, server=server)

    async def unmount_server(self, prefix: MCPMountPrefix, *, _allow_pinned: bool = False) -> None:
        """Unmount a specific server.

        Exception-safe: cleanup always attempted, mount always removed from dict.

        Args:
            prefix: Server mount prefix
            _allow_pinned: Internal flag - allow unmounting pinned servers (for __aexit__ only)

        Raises:
            RuntimeError: If server is pinned (unless _allow_pinned=True) or compositor is closed
            ValueError: If server not found

        Note:
            TODO (Pinning Architecture): Current situation and future improvement

            CURRENT STATE:
            - Pinning is available to ALL servers via mount_inproc(pinned=True)
            - This _allow_pinned flag allows __aexit__ to cleanup pinned servers
            - Two categories of pinned servers exist:
              1) Compositor internals: resources, compositor_meta (pure Python, minimal cleanup)
              2) Application servers: runtime/docker (Docker containers!), stateful servers (critic_submit, etc.)

            PROBLEM:
            - Application servers (category 2) have CRITICAL external resources that need cleanup
            - Docker containers: Must be stopped/removed via scoped_container().__aexit__
            - But they're marked "pinned" which conceptually means "lives as long as compositor"
            - This contradicts the cleanup need, requiring _allow_pinned workaround

            FUTURE ARCHITECTURAL OPTION:
            - Restrict pinning to compositor-internal servers ONLY
            - Remove public pinned= parameter from mount_inproc()
            - Add internal _mount_internal_pinned() for resources/compositor_meta only
            - Force applications to manage lifecycle explicitly (no pinning for app servers)
            - Benefits: clearer ownership, no _allow_pinned flag needed, no Docker container leak risk

            RATIONALE FOR CURRENT FIX:
            - Immediate: fixes ~75 test teardown errors (77% → 93% pass rate)
            - Safe: prevents Docker container leaks by ensuring cleanup runs
            - Minimal: preserves current API, no application code changes needed
            - Future work can migrate to restricted pinning incrementally
        """
        # Defensive check: prevent unmount when closed
        async with self._state_lock:
            if self._state == CompositorState.CLOSED:
                raise RuntimeError(f"Cannot unmount server - compositor '{self.name}' is closed")

        # Get mount under lock
        async with self._mount_lock:
            mount = self._mounts.get(prefix)

            if mount is None:
                raise ValueError(f"Server '{prefix}' is not mounted")

            if mount.pinned and not _allow_pinned:
                raise RuntimeError(
                    f"Cannot unmount pinned server '{prefix}'. Pinned servers remain for the compositor's lifetime."
                )

        # Defensive check: warn if mount is in unexpected state
        if not mount.is_active and not mount.is_failed:
            logger.warning(
                f"Unmounting server '{prefix}' in unexpected state: {mount.state.name}. Cleanup will proceed anyway."
            )

        # Cleanup (exception-safe, idempotent)
        # Always remove from dict even if cleanup fails
        try:
            await mount.cleanup()
        except Exception as e:
            logger.exception(f"Error cleaning up mount '{prefix}' (server will still be unmounted)", exc_info=e)

        # Remove from dict (always, even if cleanup failed)
        async with self._mount_lock:
            self._mounts.pop(prefix, None)

        # Notify listeners
        await self._notify_mount_listeners(prefix, MountEvent.UNMOUNTED)

    async def mount_servers_from_config(
        self, config: MCPConfig, *, on_error: str = "raise"
    ) -> dict[str, Exception | None]:
        """Mount multiple servers from config in parallel.

        Args:
            config: MCPConfig object with mcpServers dict
            on_error: How to handle mount errors:
                - "raise": Raise on first error (default)
                - "collect": Continue mounting others, return errors dict

        Returns:
            Dict mapping server name to error (None if successful)
        """
        servers = config.mcpServers
        if not servers:
            return {}

        # Mount all servers in parallel
        async def _mount_one(name: str, spec: MCPServerTypes) -> tuple[str, Exception | None]:
            try:
                await self.mount_server(name, spec)
                return (name, None)
            except Exception as e:
                if on_error == "raise":
                    raise
                return (name, e)

        results = await asyncio.gather(*[_mount_one(name, spec) for name, spec in servers.items()])
        return dict(results)

    # ---- Lifecycle management (async context manager) ---------------------

    async def __aenter__(self):
        """Enter context. Returns self (NOT a separate Handle type).

        Auto-mounts infrastructure servers (resources, compositor_meta) as pinned.

        Raises:
            RuntimeError: If already entered or closed
        """
        async with self._state_lock:
            if self._state == CompositorState.ACTIVE:
                raise RuntimeError(
                    f"Compositor '{self.name}' is already in an active context manager! "
                    "Cannot enter the same compositor twice."
                )
            if self._state == CompositorState.CLOSED:
                raise RuntimeError(f"Compositor '{self.name}' is already closed. Cannot reuse a closed compositor.")

            self._state = CompositorState.ACTIVE

        # Mount infrastructure servers (always pinned)
        self.resources = await self.mount_inproc(RESOURCES_MOUNT_PREFIX, ResourcesServer(compositor=self), pinned=True)
        self.compositor_meta = await self.mount_inproc(
            COMPOSITOR_META_MOUNT_PREFIX, CompositorMetaServer(compositor=self), pinned=True
        )

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit context, cleanup all servers (including pinned).

        Pinned servers are unmounted only on compositor exit (not on close()).
        Always updates state to CLOSED, even if cleanup fails.

        Raises:
            ExceptionGroup: If any servers failed to unmount (Python 3.11+)
        """
        exceptions: list[Exception] = []

        try:
            # First unmount non-pinned servers
            try:
                await self.close()
            except Exception as e:
                exceptions.append(e)
                logger.exception("Failed to close non-pinned servers during exit", exc_info=e)

            # Then unmount pinned servers (they should live as long as compositor lives)
            async with self._mount_lock:
                pinned_names = [name for name, mount in self._mounts.items() if mount.pinned]

            # Unmount each pinned server (collect exceptions)
            for name in pinned_names:
                try:
                    await self.unmount_server(name, _allow_pinned=True)
                except Exception as e:
                    exceptions.append(e)
                    logger.exception(f"Failed to unmount pinned server '{name}' during exit", exc_info=e)
        finally:
            async with self._state_lock:
                self._state = CompositorState.CLOSED

        # Raise collected exceptions as a group
        if exceptions:
            raise ExceptionGroup("Failed to unmount one or more servers during compositor exit", exceptions)

        return False  # Don't suppress exceptions

    async def close(self):
        """Cleanup all non-pinned servers.

        Continues cleanup even if individual servers fail.
        Safe to call multiple times (idempotent).

        Raises:
            ExceptionGroup: If any servers failed to unmount (Python 3.11+)
        """
        if self._state == CompositorState.CLOSED:
            raise RuntimeError(f"Compositor '{self.name}' is already closed")

        # Snapshot non-pinned servers under lock
        async with self._mount_lock:
            names = [name for name, mount in self._mounts.items() if not mount.pinned]

        # Unmount each server (collect exceptions)
        exceptions: list[Exception] = []
        for name in names:
            try:
                await self.unmount_server(name)
            except Exception as e:
                exceptions.append(e)
                logger.exception(f"Failed to unmount server '{name}' during cleanup", exc_info=e)

        # Raise collected exceptions as a group
        if exceptions:
            raise ExceptionGroup("Failed to unmount one or more non-pinned servers", exceptions)

    def __del__(self):
        """Warn if compositor is garbage collected without proper cleanup.

        This detects container leaks at development time by catching compositors
        that were created without using 'async with Compositor() as comp:'.
        """
        # Check if we have unclosed non-pinned mounts
        if not self._mounts:
            return

        non_pinned = [name for name, mount in self._mounts.items() if not mount.pinned]
        if not non_pinned:
            return

        # Determine the specific problem
        if self._state == CompositorState.CREATED:
            problem = "was never used as context manager"
            hint = "ALWAYS use: async with Compositor() as comp:"
        elif self._state == CompositorState.ACTIVE:
            problem = "entered but never exited"
            hint = "Did the async context manager fail to exit?"
        elif self._state == CompositorState.CLOSED:
            problem = "has unclosed servers after exit"
            hint = "This may indicate a cleanup failure in close()"
        else:
            problem = "has invalid state"
            hint = "Internal error - state tracking broken"

        msg = (
            f"\nCOMPOSITOR LEAK: '{self.name}' {problem}!\n"
            f"  Still has {len(non_pinned)} server(s): {non_pinned}\n"
            f"  This will leak Docker containers!\n\n"
            f"  {hint}\n"
        )

        warnings.warn(msg, ResourceWarning, stacklevel=2)
        print(msg, file=sys.stderr)

    # ---- Internals ----------------------------------------------------------
    async def _mount_names(self) -> list[str]:
        async with self._mount_lock:
            return list(self._mounts.keys())

    # ---- Slot factory (transport-agnostic) ---------------------------------

    def _fm_transport_from_spec(self, spec: MCPServerTypes) -> ClientTransport:
        # Use FastMCP's typed server config classes
        if isinstance(spec, RemoteMCPServer | TransformingRemoteMCPServer):
            headers = dict(spec.headers or {})
            if spec.auth:
                headers.setdefault("Authorization", f"Bearer {spec.auth}")
            return StreamableHttpTransport(spec.url, headers=headers)
        if isinstance(spec, StdioMCPServer | TransformingStdioMCPServer):
            return StdioTransport(spec.command, args=list(spec.args or []), env=spec.env, cwd=spec.cwd)
        raise ValueError("unsupported transport for fastmcp client")

    def get_child_client(self, server: MCPMountPrefix) -> Client:
        """Return the persistent child client for a mounted server.

        The returned client maintains a long-lived session. Callers MAY use
        `async with client:` to temporarily borrow the session; exiting the
        context will not close the underlying persistent session.

        Raises:
            ValueError: If server not found
            RuntimeError: If server not active
        """
        mount = self._mounts.get(server)
        if mount is None:
            raise ValueError(f"Server '{server}' is not mounted")

        # Use mount's property which validates state
        return mount.child_client

    def get_inproc_server(self, prefix: MCPMountPrefix) -> FastMCP | None:
        """Get the in-process FastMCP server instance for a given mount prefix.

        Args:
            prefix: The mount prefix (already validated)

        Returns:
            The FastMCP server instance if the mount exists and is in-process, None otherwise.
        """
        mount = self._mounts.get(prefix)
        if mount is None:
            return None
        return mount.inproc_server

    async def read_resource_contents(
        self, uri: mcp_types.AnyUrl
    ) -> list[mcp_types.TextResourceContents | mcp_types.BlobResourceContents]:
        """Read resource contents, converting from FastMCP's internal types to MCP protocol types.

        Used by resources server to avoid client dependency. FastMCP's internal _read_resource_mcp
        returns mcp.server.lowlevel.helper_types.ReadResourceContents which must be converted to
        proper MCP protocol types (TextResourceContents | BlobResourceContents).
        """
        raw_contents = await self._read_resource_mcp(uri)
        # Convert FastMCP's internal ReadResourceContents to MCP protocol types
        return [
            mcp_types.BlobResourceContents(
                uri=uri, mimeType=c.mime_type, blob=base64.b64encode(c.content).decode("ascii")
            )
            if isinstance(c.content, bytes)
            else mcp_types.TextResourceContents(uri=uri, mimeType=c.mime_type, text=c.content)
            for c in raw_contents
        ]
