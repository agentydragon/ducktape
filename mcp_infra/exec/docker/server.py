"""
FastMCP server: per-session Docker container exec.

- One container per FastMCP session (created in lifespan; stopped on exit)
- Network mode configurable (default: none); RO/RW bind mounts as provided; working_dir is writable
- Single source of truth for container contents: host-side docker image history (CreatedBy)
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from typing import Annotated, Any

import aiodocker
import mcp.types as mcp_types
from fastmcp.dependencies import Depends
from fastmcp.server import FastMCP
from fastmcp.server.context import Context
from pydantic import Field, WithJsonSchema

from mcp_infra.exec.docker.container_session import (
    make_container_lifespan,
    render_container_result,
    run_session_container,
    session_state_from_ctx,
)
from mcp_infra.exec.docker.types import (
    AlwaysSetTo,
    ContainerExecServerConfig,
    ContainerImageHistoryEntry,
    ContainerImageInfo,
    ContainerInfo,
    CwdPolicy,
    DefaultValue,
    EnvVar,
    ModelChooses,
    ResolvedExecInput,
)
from mcp_infra.exec.read_image import validate_and_encode_image
from util.exec.models import CMD_DESCRIPTION, BaseExecResult, TimeoutMs, async_timer

# URI template for file:// resource (file:///absolute/path format)
# Uses {path*} wildcard syntax (RFC 6570) to match paths with slashes
FILE_RESOURCE_URI_TEMPLATE = "file://{path*}"

DockerExecCommand = Annotated[list[str], Field(description=CMD_DESCRIPTION)]
# Keep runtime timeout bounds from TimeoutMs while exposing a plain integer schema to
# strict tool-use providers that reject integer-bound keywords.
DockerExecTimeout = Annotated[
    TimeoutMs,
    WithJsonSchema({"type": "integer"}),
    Field(description="Timeout in milliseconds; stops this exec's process group (exit status becomes TimedOut)"),
]
DockerExecCwd = Annotated[str | None, Field(description="Working directory inside container")]
DockerExecRequiredCwd = Annotated[str, Field(description="Working directory inside container")]
DockerExecEnv = Annotated[
    list[EnvVar] | None,
    Field(description="Environment variables as ['NAME=value', ...] (None = inherit container env)"),
]
DockerExecUser = Annotated[str | None, Field(description="Username inside container (None = container default)")]


def _get_running_container(ctx: Context) -> aiodocker.docker.DockerContainer:
    """Get the aiodocker container handle for the current session, or raise."""
    session = session_state_from_ctx(ctx)
    if session.container_id is None:
        raise RuntimeError("No container available")
    return session.docker_client.containers.container(session.container_id)


def _none() -> None:
    return None


def resolve_cwd(policy: CwdPolicy, requested_cwd: str | None) -> str | None:
    """Resolve the effective cwd from the policy and the exposed argument."""
    match policy:
        case AlwaysSetTo(value=v):
            return str(v)
        case DefaultValue(value=v):
            return requested_cwd if requested_cwd is not None else str(v)
        case ModelChooses():
            if requested_cwd is None:
                raise ValueError("cwd is required by this container's working-directory policy")
            return requested_cwd
        case _:
            raise TypeError(f"Unknown CwdPolicy: {policy!r}")


class ContainerExecServer(FastMCP):
    """Docker container exec MCP server with typed tool access.

    Subclasses FastMCP and adds typed tool attributes for accessing
    tool names. This is the single source of truth - no string literals elsewhere.
    """

    # Tool name constant (for test infrastructure only)
    EXEC_TOOL_NAME = "exec"

    # Default mount prefixes for container exec servers in tests
    # Different test contexts use different names for the same server type
    DOCKER_MOUNT_PREFIX = "docker"
    RUNTIME_MOUNT_PREFIX = "runtime"

    # Resource URI constants
    CONTAINER_INFO_URI = "resource://container.info"

    # Tool reference (assigned in __init__ after tool registration)
    exec_tool: Callable[..., Awaitable[BaseExecResult]]

    @staticmethod
    def file_uri(path: str) -> str:
        """Construct file:// URI for a container path (file:///absolute/path)."""
        return f"file://{path}"

    def __init__(self, docker_client: aiodocker.Docker, config: ContainerExecServerConfig):
        """Create a generic per-session container exec FastMCP server.

        Note:
            The caller must create and manage the docker_client lifecycle. The server
            lifespan uses the client but does not close it - caller remains responsible
            for cleanup (typically via atexit or app shutdown hooks).
        """
        cwd_policy = config.cwd_policy
        allow_user_field = config.allow_user_field
        allow_env_field = config.allow_env_field
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
            lifespan=make_container_lifespan(config, docker_client),
        )

        # Register container.info resource
        async def container_info_json(ctx: Context) -> str:
            session = session_state_from_ctx(ctx)
            img_info = await session.docker_client.images.inspect(session.opts.image)
            img_history_raw = await session.docker_client.images.history(session.opts.image)
            img_history = (
                [ContainerImageHistoryEntry.model_validate(entry) for entry in img_history_raw]
                if img_history_raw
                else None
            )

            ci = ContainerInfo(
                image=ContainerImageInfo(name=session.opts.image, id=img_info["Id"], tags=img_info["RepoTags"]),
                container_id=session.container_id,
                binds=session.opts.binds,
                working_dir=str(session.opts.working_dir),
                network_mode=session.opts.network_mode,
                image_history=img_history,
            )
            return ci.model_dump_json()

        # Ensure the context annotation is preserved after future-annotations rewriting so
        # FastMCP treats this as a static resource rather than a template.
        container_info_json.__annotations__["ctx"] = Context
        self.resource(
            container_info_uri,
            mime_type="application/json",
            name="container.info",
            title="Container session metadata",
            description="Docker container details for this session",
        )(container_info_json)

        _exec_doc_lines = [
            "Run a command inside the per-session Docker container.",
            "",
            "The cmd array is passed directly to Docker exec (execve-style, no shell).",
            "No shell interpretation - arguments are passed as-is to the executable.",
            "",
            "Usage patterns:",
            '- Simple command: {"cmd": ["python", "--version"]}',
            '- With arguments: {"cmd": ["nl", "-ba", "/workspace/file.py"]}',
            '- Shell features (pipes, redirection): {"cmd": ["sh", "-c", "grep pattern | head"]}',
        ]
        if not isinstance(cwd_policy, AlwaysSetTo):
            _exec_doc_lines.append('- Working directory: {"cmd": ["ls"], "cwd": "/snapshots"}')
        _exec_doc_lines += [
            "",
            "Common mistakes:",
            """- DON'T: {"cmd": ["python '- << 'PY'"]} (shell syntax without sh -c)""",
            """- DON'T: {"cmd": ["grep", "'pattern'"]} (quotes in string)""",
            '- DO: {"cmd": ["sh", "-c", "printf %s content > file.txt"]}',
        ]

        if isinstance(cwd_policy, AlwaysSetTo):
            _exec_doc_lines.append(f"\nCommands always run in {cwd_policy.value}.")
        _exec_description = "\n".join(_exec_doc_lines)

        async def run_exec(
            cmd: list[str],
            timeout_ms: int,
            cwd: str | None,
            env: list[EnvVar] | None,
            user: str | None,
            context: Context,
        ) -> BaseExecResult:
            async with async_timer() as get_duration_ms:
                session = session_state_from_ctx(context)
                effective = ResolvedExecInput(
                    cmd=cmd,
                    cwd=resolve_cwd(cwd_policy, cwd),
                    timeout_ms=timeout_ms,
                    env=env if allow_env_field else None,
                    user=user if allow_user_field else None,
                )
                (stdout_buf, stderr_buf, exit_code, timed_out) = await run_session_container(
                    session, effective.cmd, effective
                )
                duration_ms = get_duration_ms()
                return render_container_result(stdout_buf, stderr_buf, exit_code, timed_out, duration_ms)

        env_default: Any = inspect.Parameter.empty if allow_env_field else Depends(_none)
        user_default: Any = inspect.Parameter.empty if allow_user_field else Depends(_none)

        if isinstance(cwd_policy, ModelChooses):

            async def exec_model_chooses(
                *,
                cmd: DockerExecCommand,
                timeout_ms: DockerExecTimeout,
                cwd: DockerExecRequiredCwd,
                env: DockerExecEnv = env_default,
                user: DockerExecUser = user_default,
                context: Context,
            ) -> BaseExecResult:
                return await run_exec(cmd, timeout_ms, cwd, env, user, context)

            self.exec_tool = self.tool(name=self.EXEC_TOOL_NAME, description=_exec_description, output_schema=None)(
                exec_model_chooses
            )

        else:
            if isinstance(cwd_policy, AlwaysSetTo):

                def configured_cwd() -> str:
                    return str(cwd_policy.value)

                cwd_default: Any = Depends(configured_cwd)
            else:
                # Keep the nullable cwd field required in the tool schema. Strict
                # OpenAI function tools require callers to send null explicitly to
                # select the configured default.
                cwd_default = inspect.Parameter.empty

            async def exec_with_configured_cwd(
                *,
                cmd: DockerExecCommand,
                timeout_ms: DockerExecTimeout,
                cwd: DockerExecCwd = cwd_default,
                env: DockerExecEnv = env_default,
                user: DockerExecUser = user_default,
                context: Context,
            ) -> BaseExecResult:
                return await run_exec(cmd, timeout_ms, cwd, env, user, context)

            self.exec_tool = self.tool(name=self.EXEC_TOOL_NAME, description=_exec_description, output_schema=None)(
                exec_with_configured_cwd
            )

        # Register file:// resource template for reading files from container
        async def read_container_file(path: str, ctx: Context) -> str:
            """Read file at absolute path from container."""
            container = _get_running_container(ctx)
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
        self.resource(
            FILE_RESOURCE_URI_TEMPLATE,
            name="container.file",
            mime_type="text/plain",
            description="Read a file from the container filesystem",
        )(read_container_file)

        # Register read_image tool for reading images from container
        async def read_image(path: str, ctx: Context) -> list[mcp_types.ImageContent]:
            """Read an image file from the container and return it for the model to see. Supported formats: JPEG, PNG, GIF, WebP."""
            container = _get_running_container(ctx)
            # Pull file from container via Docker API
            tar = await container.get_archive(path)
            member_name = PurePosixPath(path).name
            member = tar.getmember(member_name)
            f = tar.extractfile(member)
            if f is None:
                raise ValueError(f"{path} is not a regular file")
            return [validate_and_encode_image(f.read(), path)]

        self.read_image_tool = self.tool(output_schema=None)(read_image)
