"""Bootstrap handlers for injecting synthetic function calls before agent sampling.

Bootstrap handlers inject pre-determined function calls (e.g., reading resources,
listing files) before the agent's first sampling cycle. This provides context
without requiring the agent to explicitly request it.

# TODO: Evaluate whether for_server() introspection-based type validation is worth
# the complexity. The validation catches type mismatches at bootstrap build time,
# but the same errors would surface at runtime anyway. Consider simplifying to
# just the basic call() method without introspection.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, get_type_hints

import pydantic_core
from pydantic import BaseModel
from pydantic.networks import AnyUrl

from mcp_infra.compositor.resources_server import ResourcesReadArgs, ResourcesServer
from mcp_infra.enhanced.flat_mixin import FlatTool
from mcp_infra.exec.models import ExecInput
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.stubs.typed_stubs import _resolve_output_type
from openai_utils.model import FunctionCallItem

if TYPE_CHECKING:
    from fastmcp.server import FastMCP

    from mcp_infra.exec.docker.server import ContainerExecServer
    from mcp_infra.mounted import Mounted

# Default timeout for bootstrap docker exec calls (1 second).
# Bootstrap commands should complete quickly - failing fast reveals issues.
DEFAULT_BOOTSTRAP_ITEM_TIMEOUT_MS = 1000


def introspect_server_models(server: FastMCP) -> dict[str, tuple[type[BaseModel] | None, type]]:
    """Extract tool Input/Output models from FastMCP server (no session needed).

    Returns:
        Dict mapping tool names to (Input model | None, Output type) tuples
    """
    try:
        tm = server._tool_manager
    except AttributeError as exc:
        raise RuntimeError("Server does not expose _tool_manager") from exc

    try:
        tools_by_name = tm._tools
    except AttributeError as exc:
        raise RuntimeError("Server tool manager does not expose _tools") from exc

    models: dict[str, tuple[type[BaseModel] | None, type]] = {}

    for tool in tools_by_name.values():
        if not isinstance(tool, FlatTool):
            continue

        input_type: type[BaseModel] | None = tool.input_model

        # Get output type from function's return annotation
        try:
            hints = get_type_hints(tool.fn, include_extras=True)
            hinted_output = hints.get("return")
        except (NameError, TypeError, AttributeError):
            hinted_output = None

        output_type = _resolve_output_type(hinted_output, hinted_output)
        models[tool.key] = (input_type, output_type)

    return models


class TypedBootstrapBuilder:
    """Type-safe builder for bootstrap function call items.

    Constructs FunctionCallItem instances with automatic call_id generation
    and optional type validation against introspected server schemas.

    Usage:
        # Without introspection (no type checking):
        builder = TypedBootstrapBuilder()
        call = builder.call("my_server", "my_tool", MyInput(...))

        # With introspection (validates payload types):
        builder = TypedBootstrapBuilder.for_server(server)
        call = builder.call("my_server", "my_tool", MyInput(...))  # type-checked
    """

    def __init__(self, *, call_id_prefix: str = "bootstrap") -> None:
        self._prefix = call_id_prefix
        self._counter = 0
        self._models: dict[str, tuple[type[BaseModel] | None, type]] = {}

    def next_call_id(self) -> str:
        """Generate next auto-incremented call_id."""
        self._counter += 1
        return f"{self._prefix}:{self._counter}"

    def call(
        self, server: MCPMountPrefix, tool: str, payload: BaseModel, *, call_id: str | None = None
    ) -> FunctionCallItem:
        """Create a typed MCP tool call item.

        Args:
            server: MCP mount prefix (already validated)
            tool: Tool name on the server
            payload: Pydantic model instance with call arguments
            call_id: Optional explicit call_id (auto-generated if not provided)

        Returns:
            FunctionCallItem ready for bootstrap injection

        Raises:
            TypeError: If payload type doesn't match introspected Input model
        """
        # Validate payload type if we have introspection data
        tool_key = build_mcp_function(server, tool)
        if tool_key in self._models:
            expected_input, _ = self._models[tool_key]
            if expected_input is not None and not isinstance(payload, expected_input):
                raise TypeError(f"{server}/{tool} expects {expected_input.__name__}, got {type(payload).__name__}")

        return FunctionCallItem(
            call_id=call_id or self.next_call_id(),
            name=build_mcp_function(server, tool),
            arguments=pydantic_core.to_json(payload.model_dump(mode="json"), fallback=str).decode("utf-8"),
        )

    @classmethod
    def for_server(cls, server: FastMCP, *, call_id_prefix: str = "bootstrap") -> TypedBootstrapBuilder:
        """Create builder with introspected tool schemas from FastMCP server.

        No MCP session required - only introspects the server's tool registry.

        Args:
            server: FastMCP server instance to introspect
            call_id_prefix: Prefix for auto-generated call_ids

        Returns:
            Builder with type validation enabled for the server's tools
        """
        builder = cls(call_id_prefix=call_id_prefix)
        builder._models = introspect_server_models(server)
        return builder

    def read_resource(
        self, resources: Mounted[ResourcesServer], server: MCPMountPrefix, uri: str | AnyUrl, *, max_bytes: int = 65536
    ) -> FunctionCallItem:
        """Bootstrap helper for resources.read.

        Args:
            resources: Mounted resources server (comp.resources)
            server: Mount prefix of server to read resource from (already validated)
            uri: Resource URI to read
            max_bytes: Maximum bytes to read (default: 65536)

        Returns:
            FunctionCallItem ready for bootstrap injection

        Example:
            builder = TypedBootstrapBuilder.for_server(runtime.server)
            call = builder.read_resource(
                comp.resources,
                comp.critic_submit.prefix,
                comp.critic_submit.server.snapshot_slug_resource.uri,
                max_bytes=256,
            )
        """
        return self.call(
            resources.prefix,
            resources.server.read_tool.name,
            ResourcesReadArgs(server=server, uri=str(uri), start_offset=0, max_bytes=max_bytes),
        )


# Helper functions for common bootstrap patterns


def docker_exec_call(
    builder: TypedBootstrapBuilder,
    runtime: Mounted[ContainerExecServer],
    cmd: Sequence[str | Path],
    *,
    timeout_ms: int | None = None,
) -> FunctionCallItem:
    """Bootstrap helper for docker exec.

    Uses DEFAULT_BOOTSTRAP_ITEM_TIMEOUT_MS (1 second) by default.
    Bootstrap commands should complete quickly; failing fast reveals issues.

    Args:
        builder: Bootstrap builder for generating typed tool calls
        runtime: Mounted runtime server (e.g., comp.runtime)
        cmd: Command to execute (accepts str or Path elements)
        timeout_ms: Optional timeout override (default: DEFAULT_BOOTSTRAP_ITEM_TIMEOUT_MS)

    Example:
        call = docker_exec_call(builder, comp.runtime, ["ls", "-la", Path("/workspace")])
        # With custom timeout for slower commands
        call = docker_exec_call(builder, comp.runtime, ["psql", "-c", "..."], timeout_ms=5000)
    """
    # Convert Path elements to str for ExecInput
    cmd_str = [str(item) for item in cmd]

    return builder.call(
        runtime.prefix,
        runtime.server.exec_tool.name,
        ExecInput(
            cmd=cmd_str, cwd=None, env=None, user=None, timeout_ms=timeout_ms or DEFAULT_BOOTSTRAP_ITEM_TIMEOUT_MS
        ),
    )
