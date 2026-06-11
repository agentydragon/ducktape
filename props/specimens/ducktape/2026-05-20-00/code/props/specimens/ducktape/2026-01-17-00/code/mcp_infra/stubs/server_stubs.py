"""Typed server stub utilities for MCP testing.

Provides a base class for creating typed stubs that wrap MCP servers
with proper type hints for better IDE support and type checking.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeVar, cast, get_args, get_origin, get_type_hints

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.client import Client

from .typed_stubs import TypedClient

TServerStub = TypeVar("TServerStub", bound="ServerStub")


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
        """Auto-wire methods based on type hints.

        Only wires methods that have a trivial body (just raise NotImplementedError).
        Methods with actual implementations are left as-is for helper methods.
        """
        # Get all methods defined in the subclass (not inherited from ServerStub)
        for name, method in inspect.getmembers(self.__class__, predicate=inspect.isfunction):
            # Skip special methods and inherited methods from ServerStub
            if name.startswith("_") or hasattr(ServerStub, name):
                continue

            # Only wire async methods
            if not inspect.iscoroutinefunction(method):
                continue

            # Skip methods with non-trivial implementations (helper methods)
            try:
                source = inspect.getsource(method)
                # Dedent the source to handle class method indentation
                source = textwrap.dedent(source)
                tree = ast.parse(source)
                # Find the function definition
                func_def = None
                for node in ast.walk(tree):
                    if isinstance(node, ast.AsyncFunctionDef):
                        func_def = node
                        break

                if not func_def:
                    continue

                # Check if the body is just "raise NotImplementedError"
                if len(func_def.body) != 1:
                    # Multi-statement body, skip auto-wiring
                    continue

                stmt = func_def.body[0]
                if not isinstance(stmt, ast.Raise):
                    # Not a raise statement, skip auto-wiring
                    continue

                # Check if it's raising NotImplementedError
                if not isinstance(stmt.exc, ast.Name) or stmt.exc.id != "NotImplementedError":
                    # Not raising NotImplementedError, skip auto-wiring
                    continue
            except TypeError:
                # TypeError from inspect.getsource on built-in methods; skip auto-wiring
                continue

            # Get type hints
            try:
                hints = get_type_hints(method)
            except (NameError, AttributeError, TypeError):
                # Can't get type hints (forward refs, missing imports), skip
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
    def from_server(cls: type[TServerStub], server: FastMCP, session: Client) -> TServerStub:
        """Create a typed stub from a FastMCP server and session."""
        # TypeVar bound to ServerStub ensures cls() accepts TypedClient
        return cast(TServerStub, cls(TypedClient.from_server(server, session)))

    def _stub(self, name: str, output_type: type):
        """Create a typed stub for a tool."""
        return self._client.stub(name, output_type)
