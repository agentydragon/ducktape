"""Typed server stub utilities for MCP testing.

Provides a base class for creating typed stubs that wrap MCP servers
with proper type hints for better IDE support and type checking.
"""

from __future__ import annotations

from collections.abc import Awaitable
import inspect
from typing import TYPE_CHECKING, Any, TypeVar, get_args, get_origin, get_type_hints

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.client import Client

from .typed_stubs import TypedClient

T = TypeVar("T")


class ServerStub:
    """Base class for typed MCP server stubs.

    Subclasses should define tool methods with type hints that get auto-wired.

    Example:
        class ChatStub(ServerStub):
            # Define the interface - these get auto-wired
            async def post(self, input: PostInput) -> PostResult:
                raise NotImplementedError  # Auto-wired at runtime

            async def read_pending_messages(self, input: ReadPendingInput) -> ReadPendingResult:
                raise NotImplementedError  # Auto-wired at runtime

        # Usage
        async with Client(server) as session:
            stub = ChatStub.from_server(server, session)
            result = await stub.post(PostInput(content="hello"))
    """

    def __init__(self, client: TypedClient):
        self._client = client
        self._auto_wire_methods()

    def _auto_wire_methods(self) -> None:
        """Auto-wire methods based on type hints."""
        # Get all methods defined in the subclass (not inherited from ServerStub)
        for name, method in inspect.getmembers(self.__class__, predicate=inspect.isfunction):
            # Skip special methods and inherited methods from ServerStub
            if name.startswith("_") or hasattr(ServerStub, name):
                continue

            # Only wire async methods
            if not inspect.iscoroutinefunction(method):
                continue

            # Get type hints
            try:
                hints = get_type_hints(method)
            except Exception:
                # Can't get type hints, skip
                continue

            # Extract return type (output model)
            if not (return_type := hints.get("return")):
                continue

            # Handle Awaitable wrapping if present
            if get_origin(return_type) == Awaitable:
                args = get_args(return_type)
                if args:
                    return_type = args[0]

            # Create and attach the stub
            stub = self._client.stub(name, return_type)

            # Create async wrapper that extracts the first parameter
            async def _call(self, input: Any, _stub=stub) -> Any:
                return await _stub(input)

            # Bind the wrapper to the instance
            setattr(self, name, _call.__get__(self, self.__class__))

    @classmethod
    def from_server(cls: type[T], server: FastMCP, session: Client) -> T:
        """Create a typed stub from a FastMCP server and session."""
        client = TypedClient.from_server(server, session)
        return cls(client)  # type: ignore[call-arg]

    def _stub(self, name: str, output_type: type):
        """Create a typed stub for a tool."""
        return self._client.stub(name, output_type)
