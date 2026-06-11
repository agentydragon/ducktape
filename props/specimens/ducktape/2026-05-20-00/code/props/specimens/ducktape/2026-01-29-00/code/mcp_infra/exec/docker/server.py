"""
FastMCP server: per-session Docker container exec.

- One container per FastMCP session (created in lifespan; stopped on exit)
- Network mode configurable (default: none); RO/RW bind mounts as provided; working_dir is writable
- Single source of truth for container contents: host-side docker image history (CreatedBy)
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, cast

import aiodocker
import mcp.types as mcp_types
from fastmcp.resources import FunctionResource, ResourceTemplate
from fastmcp.server.context import Context

from mcp_infra.enhanced.flat_mixin import FlatTool
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.docker.container_session import (
    ContainerOptions,
    make_container_lifespan,
    render_container_result,
    run_session_container,
    session_state_from_ctx,
)
from mcp_infra.exec.models import BaseExecResult, ExecInput, async_timer
from mcp_infra.exec.read_image import ReadImageInput, validate_and_encode_image
from mcp_infra.mcp_types import ContainerImageHistoryEntry, ContainerImageInfo, ContainerInfo
from mcp_infra.prefix import MCPMountPrefix

# URI template for file:// resource (file:///absolute/path format)
# Uses {path*} wildcard syntax (RFC 6570) to match paths with slashes
FILE_RESOURCE_URI_TEMPLATE = "file://{path*}"


class ContainerExecServer(EnhancedFastMCP):
    """Docker container exec MCP server with typed tool access.

    Subclasses EnhancedFastMCP and adds typed tool attributes for accessing
    tool names. This is the single source of truth - no string literals elsewhere.
    """

    # Tool name constant (for test infrastructure only)
    EXEC_TOOL_NAME = "exec"

    # Default mount prefixes for container exec servers in tests
    # Different test contexts use different names for the same server type
    DOCKER_MOUNT_PREFIX: MCPMountPrefix = MCPMountPrefix("docker")
    RUNTIME_MOUNT_PREFIX: MCPMountPrefix = MCPMountPrefix("runtime")

    # Resource attributes (stashed results of @resource decorator - single source of truth for URI access)
    container_info_resource: FunctionResource
    file_resource: ResourceTemplate

    # Tool reference (assigned in __init__ after tool registration)
    exec_tool: FlatTool

    @staticmethod
    def file_uri(path: str) -> str:
        """Construct file:// URI for a container path (file:///absolute/path)."""
        return f"file://{path}"

    def __init__(self, docker_client: aiodocker.Docker, opts: ContainerOptions):
        """Create a generic per-session container exec FastMCP server.

        Args:
            docker_client: Async Docker client (owned and managed by caller).
            opts: Container configuration options

        Note:
            The caller must create and manage the docker_client lifecycle. The server
            lifespan uses the client but does not close it - caller remains responsible
            for cleanup (typically via atexit or app shutdown hooks).
        """
        # Define container.info resource URI (before super().__init__ so it can be used in instructions)
        container_info_uri = "resource://container.info"

        super().__init__(
            "Docker Exec MCP Server",
            instructions=(
                f"Provides access to a Docker container.\n\n"
                f"Image history is available by reading the resource {container_info_uri}.\n\n"
                f"/tmp is writable and can be used as a scratchpad for notes, intermediate results, "
                f"or organizing your thoughts."
            ),
            lifespan=make_container_lifespan(opts, docker_client),
        )

        # Register container.info resource and stash the result
        async def container_info_json(ctx: Context) -> dict[str, Any]:
            s = session_state_from_ctx(ctx)
            img_info = await s.docker_client.images.inspect(s.image)
            img_history_raw = await s.docker_client.images.history(s.image)
            img_history = (
                [ContainerImageHistoryEntry.model_validate(entry) for entry in img_history_raw]
                if img_history_raw
                else None
            )

            ci = ContainerInfo(
                image=ContainerImageInfo(
                    name=s.image, id=img_info.get("Id", "unknown"), tags=img_info.get("RepoTags", [s.image])
                ),
                container_id=s.container_id,
                binds=s.binds,
                working_dir=str(s.working_dir),
                network_mode=s.network_mode,
                image_history=img_history,
            )
            return ci.model_dump(mode="json")

        # Ensure the context annotation is preserved after future-annotations rewriting so
        # FastMCP treats this as a static resource rather than a template.
        container_info_json.__annotations__["ctx"] = Context
        self.container_info_resource = cast(
            FunctionResource,
            self.resource(
                container_info_uri,
                mime_type="application/json",
                name="container.info",
                title="Container session metadata",
                description="Docker container details for this session",
            )(container_info_json),
        )

        # Register exec tool - name derived from function name
        async def exec(input: ExecInput, context: Context) -> BaseExecResult:
            """Run a command inside the per-session Docker container.

            The cmd array is passed directly to Docker exec (execve-style, no shell).
            No shell interpretation - arguments are passed as-is to the executable.

            Usage patterns:
            - Simple command: {"cmd": ["python", "--version"]}
            - With arguments: {"cmd": ["nl", "-ba", "/workspace/file.py"]}
            - Shell features (pipes, redirection): {"cmd": ["sh", "-c", "grep pattern | head"]}
            - Python from stdin: {"cmd": ["python"], "stdin_text": "print('hello')\\n"}
            - Working directory: {"cmd": ["ls"], "cwd": "/snapshots"}

            Common mistakes:
            - DON'T: {"cmd": ["python '- << 'PY'"]} (shell syntax without sh -c)
            - DON'T: {"cmd": ["grep", "'pattern'"]} (quotes in string)
            - DO: {"cmd": ["sh", "-c", "cat > file.txt"], "stdin_text": "content"}
            """
            async with async_timer() as get_duration_ms:
                s = session_state_from_ctx(context)

                # Pass cmd directly to Docker
                cmd = input.cmd

                (stdout_buf, stderr_buf, exit_code, timed_out) = await run_session_container(s, cmd, input, opts)

                duration_ms = get_duration_ms()
                return render_container_result(stdout_buf, stderr_buf, exit_code, timed_out, duration_ms)

        self.exec_tool = self.flat_model()(exec)

        # Register file:// resource template for reading files from container
        async def read_container_file(path: str, ctx: Context) -> str:
            """Read file at absolute path from container."""
            s = session_state_from_ctx(ctx)
            if s.container_id is None:
                raise RuntimeError("No container available")
            container = s.docker_client.containers.container(s.container_id)
            # get_archive returns a TarFile directly (not an async iterable)
            tar = await container.get_archive(path)
            # The archive contains one member with basename of the path
            member_name = PurePosixPath(path).name
            member = tar.getmember(member_name)
            f = tar.extractfile(member)
            if f is None:
                raise RuntimeError(f"{path} is not a regular file")
            return f.read().decode("utf-8")

        read_container_file.__annotations__["ctx"] = Context
        self.file_resource = cast(
            ResourceTemplate,
            self.resource(
                FILE_RESOURCE_URI_TEMPLATE,
                name="container.file",
                mime_type="text/plain",
                description="Read a file from the container filesystem",
            )(read_container_file),
        )

        # Register read_image tool for reading images from container
        async def read_image(input: ReadImageInput, ctx: Context) -> list[mcp_types.ImageContent]:
            """Read an image file from the container and return it for the model to see."""
            s = session_state_from_ctx(ctx)
            if s.container_id is None:
                raise RuntimeError("No container available")
            container = s.docker_client.containers.container(s.container_id)
            # Pull file from container via Docker API
            tar = await container.get_archive(input.path)
            member_name = PurePosixPath(input.path).name
            member = tar.getmember(member_name)
            f = tar.extractfile(member)
            if f is None:
                raise ValueError(f"{input.path} is not a regular file")
            return [validate_and_encode_image(f.read(), input.path)]

        self.read_image_tool = self.flat_model()(read_image)
