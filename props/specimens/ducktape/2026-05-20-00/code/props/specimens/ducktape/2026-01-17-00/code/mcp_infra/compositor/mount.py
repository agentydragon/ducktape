"""Mount class for managing MCP server lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal

from fastmcp.client import Client
from fastmcp.client.transports import ClientTransport
from fastmcp.mcp_config import MCPServerTypes
from fastmcp.server import FastMCP
from fastmcp.server.proxy import FastMCPProxy

from mcp_infra.prefix import MCPMountPrefix

logger = logging.getLogger(__name__)


class MountState(Enum):
    """Mount lifecycle states.

    Transitions:
    - PENDING → ACTIVE (successful setup)
    - PENDING → FAILED (setup failure)
    - ACTIVE/FAILED → CLOSED (cleanup)
    """

    PENDING = auto()  # Created but not initialized
    ACTIVE = auto()  # Initialized and ready
    FAILED = auto()  # Initialization failed
    CLOSED = auto()  # Cleanup completed


# Discriminated union for type-safe state management
@dataclass
class _MountPending:
    """Mount is created but not yet initialized."""

    kind: Literal[MountState.PENDING] = MountState.PENDING


@dataclass
class _MountActive:
    """Mount is initialized and ready for use."""

    stack: AsyncExitStack
    proxy: FastMCPProxy
    child_client: Client
    kind: Literal[MountState.ACTIVE] = MountState.ACTIVE


@dataclass
class _MountFailed:
    """Mount initialization failed."""

    exception: Exception
    stack: AsyncExitStack | None = None
    kind: Literal[MountState.FAILED] = MountState.FAILED


@dataclass
class _MountClosed:
    """Mount has been cleaned up."""

    kind: Literal[MountState.CLOSED] = MountState.CLOSED


_MountStateData = _MountPending | _MountActive | _MountFailed | _MountClosed


class Mount:
    """A mounted MCP server with encapsulated lifecycle.

    Usage:
        mount = Mount(prefix="runtime", pinned=False)
        await mount.setup_inproc(server)  # Exception-safe

        if mount.is_active:
            tools = await mount.child_client.list_tools()

        await mount.cleanup()  # Idempotent, exception-safe
    """

    def __init__(self, prefix: MCPMountPrefix, *, pinned: bool = False, spec: MCPServerTypes | None = None):
        """Create mount. Does not initialize - call setup_*() to initialize."""
        self._prefix = prefix
        self._pinned = pinned
        self._spec = spec
        self._state_data: _MountStateData = _MountPending()
        self._server: FastMCP | None = None

    # Read-only properties

    @property
    def prefix(self) -> MCPMountPrefix:
        return self._prefix

    @property
    def pinned(self) -> bool:
        return self._pinned

    @property
    def spec(self) -> MCPServerTypes | None:
        return self._spec

    @property
    def state(self) -> MountState:
        return self._state_data.kind

    @property
    def is_active(self) -> bool:
        return isinstance(self._state_data, _MountActive)

    @property
    def is_failed(self) -> bool:
        return isinstance(self._state_data, _MountFailed)

    @property
    def is_closed(self) -> bool:
        return isinstance(self._state_data, _MountClosed)

    @property
    def exception(self) -> Exception | None:
        """Get exception if mount failed, None otherwise."""
        if isinstance(self._state_data, _MountFailed):
            return self._state_data.exception
        return None

    @property
    def inproc_server(self) -> FastMCP | None:
        """Get the in-process FastMCP server instance if this is an in-process mount."""
        return self._server

    # Safe accessors (raise if not active)

    @property
    def proxy(self) -> FastMCPProxy:
        """Get proxy. Raises if mount not active."""
        if not isinstance(self._state_data, _MountActive):
            raise RuntimeError(f"Mount '{self._prefix}' not active (state: {self._state_data.kind.name})")
        return self._state_data.proxy

    @property
    def child_client(self) -> Client:
        """Get child client. Raises if mount not active."""
        if not isinstance(self._state_data, _MountActive):
            raise RuntimeError(f"Mount '{self._prefix}' not active (state: {self._state_data.kind.name})")
        return self._state_data.child_client

    # Setup methods

    async def setup_inproc(
        self, server: FastMCP, child_handler_factory: Callable[[MCPMountPrefix], object] | None = None
    ) -> None:
        """Setup in-process server mount. Exception-safe.

        On success: mount.is_active == True
        On failure: mount.is_failed == True, mount.exception set

        Args:
            server: FastMCP server instance
            child_handler_factory: Optional factory for creating child message handler

        Raises:
            RuntimeError: If already setup
            Exception: If setup fails (after cleanup)
        """
        if not isinstance(self._state_data, _MountPending):
            raise RuntimeError(f"Mount '{self._prefix}' already setup")

        stack = AsyncExitStack()
        try:
            # Store server reference
            self._server = server

            # Create client with optional message handler
            handler = child_handler_factory(self._prefix) if child_handler_factory else None
            child_client = Client(server, message_handler=handler)
            await stack.enter_async_context(child_client)

            # Create proxy with persistent child client
            # NOTE: We must set client_factory BEFORE proxy construction because
            # FastMCPProxy passes client_factory to its managers at construction time.
            # Setting proxy.client_factory after construction would not update the
            # already-instantiated ProxyToolManager, ProxyResourceManager, etc.
            proxy = FastMCPProxy(client_factory=lambda: child_client)
            # Also set on proxy for any code that reads it directly
            proxy.client_factory = lambda: child_client

            # Verify initialize result is accessible
            try:
                _ = child_client.initialize_result
                self._state_data = _MountActive(stack=stack, proxy=proxy, child_client=child_client)
            except Exception as e:
                logger.warning(f"Failed to get initialize result for '{self._prefix}': {e}")
                self._state_data = _MountFailed(exception=e, stack=stack)
                # Don't raise - mount is registered but failed

        except Exception as e:
            # Setup failed before we could initialize
            await stack.aclose()
            self._state_data = _MountFailed(exception=e)
            raise

    async def setup_external(
        self,
        spec: MCPServerTypes,
        transport_factory: Callable[[MCPServerTypes], ClientTransport],
        child_handler_factory: Callable[[MCPMountPrefix], object] | None = None,
    ) -> None:
        """Setup external server mount. Exception-safe.

        On success: mount.is_active == True
        On failure: mount.is_failed == True, mount.exception set

        Args:
            spec: Server specification
            transport_factory: Factory for creating transport from spec
            child_handler_factory: Optional factory for creating child message handler

        Raises:
            RuntimeError: If already setup
            Exception: If setup fails (after cleanup)
        """
        if not isinstance(self._state_data, _MountPending):
            raise RuntimeError(f"Mount '{self._prefix}' already setup")

        stack = AsyncExitStack()
        try:
            # Create transport and client
            transport = transport_factory(spec)
            handler = child_handler_factory(self._prefix) if child_handler_factory else None
            base_client = Client(transport, message_handler=handler)
            await stack.enter_async_context(base_client)

            # Create proxy
            proxy = FastMCP.as_proxy(base_client)
            # Ensure proxy uses the persistent child client session
            proxy.client_factory = lambda: base_client

            # Verify initialize result is accessible
            try:
                _ = base_client.initialize_result
                self._state_data = _MountActive(stack=stack, proxy=proxy, child_client=base_client)
            except Exception as e:
                logger.warning(f"Failed to get initialize result for '{self._prefix}': {e}")
                self._state_data = _MountFailed(exception=e, stack=stack)
                # Don't raise - mount is registered but failed

        except Exception as e:
            # Setup failed before we could initialize
            await stack.aclose()
            self._state_data = _MountFailed(exception=e)
            raise

    # Cleanup

    async def cleanup(self) -> None:
        """Cleanup mount resources. Exception-safe, idempotent.

        Safe to call multiple times. Logs errors but doesn't raise.
        """
        if isinstance(self._state_data, _MountClosed):
            return

        # Defensive check: warn if already closing
        if isinstance(self._state_data, _MountPending):
            logger.warning(
                f"Cleaning up mount '{self._prefix}' that was never initialized "
                "(state: PENDING). This is safe but unexpected."
            )

        # Get stack to close (from Active or Failed state)
        stack = None
        if isinstance(self._state_data, _MountActive | _MountFailed):
            stack = self._state_data.stack

        if stack is not None:
            try:
                await stack.aclose()
            except Exception as e:
                logger.exception(f"Error during cleanup for '{self._prefix}'", exc_info=e)

        self._state_data = _MountClosed()
