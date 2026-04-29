"""
FastMCP server: per-session Docker container exec.

- One container per FastMCP session (created in lifespan; stopped on exit)
- Network mode configurable (default: none); RO/RW bind mounts as provided; working_dir is writable
- Single source of truth for container contents: host-side docker image history (CreatedBy)
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Any

import aiodocker
import mcp.types as mcp_types
from fastmcp.server.context import Context
from pydantic import Field, WithJsonSchema, create_model

from mcp_infra.enhanced.server import EnhancedFastMCP
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
    ModelChooses,
)
from mcp_infra.exec.models import BaseExecResult, EnvVar, ResolvedExecInput, TimeoutMs, async_timer
from mcp_infra.exec.read_image import ReadImageInput, validate_and_encode_image
from mcp_infra.flat_tool import FlatTool
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# URI template for file:// resource (file:///absolute/path format)
# Uses {path*} wildcard syntax (RFC 6570) to match paths with slashes
FILE_RESOURCE_URI_TEMPLATE = "file://{path*}"


def _get_running_container(ctx: Context) -> aiodocker.docker.DockerContainer:
    """Get the aiodocker container handle for the current session, or raise."""
    session = session_state_from_ctx(ctx)
    if session.container_id is None:
        raise RuntimeError("No container available")
    return session.docker_client.containers.container(session.container_id)


def resolve_cwd(policy: CwdPolicy, tool_input: Any) -> str | None:
    """Resolve the effective cwd from the policy and the tool input model.

    The tool input model may or may not have a ``cwd`` field depending on the policy.
    """
    match policy:
        case AlwaysSetTo(value=v):
            return str(v)
        case DefaultValue(value=v):
            model_cwd: str | None = tool_input.cwd
            return model_cwd if model_cwd is not None else str(v)
        case ModelChooses():
            return str(tool_input.cwd)
        case _:
            raise TypeError(f"Unknown CwdPolicy: {policy!r}")


def _make_exec_input_model(*, allow_user: bool, allow_env: bool, cwd_policy: CwdPolicy) -> type:
    """Dynamically create the Pydantic input model for the exec tool.

    When allow_user=False or allow_env=False, the corresponding field is omitted from
    the schema so the LLM cannot set it (the handler substitutes None for disabled fields).
    cwd_policy controls how the cwd field appears in the schema.
    """
    fields: dict[str, Any] = {
        "cmd": (
            list[str],
            Field(
                description=(
                    "Command array passed directly to Docker exec API (no shell). "
                    "DO NOT include shell quotes around arguments - array elements are passed as-is. "
                    "WRONG: ['sed', '-n', \"'1,10p'\", 'file'] (quotes in string). "
                    "RIGHT: ['sed', '-n', '1,10p', 'file'] (no quotes). "
                    "For shell features (pipes, globs), use: ['sh', '-c', 'sed -n 1,10p file | head']"
                )
            ),
        ),
        "timeout_ms": (
            # ``TimeoutMs`` is ``Annotated[int, Field(gt=0, le=MAX_EXEC_TIMEOUT_MS)]``,
            # which lands in the JSON Schema as ``exclusiveMinimum`` / ``maximum``.
            # Anthropic's strict tool-use mode rejects integer constraints
            # (``"For 'integer' type, properties exclusiveMinimum, maximum are not
            # supported"``), so we override the schema to a plain ``{"type": "integer"}``
            # while keeping the Pydantic runtime validators that ``TimeoutMs`` carries.
            Annotated[TimeoutMs, WithJsonSchema({"type": "integer"})],
            Field(description="Timeout in milliseconds; sends TERM (exit status becomes TimedOut)"),
        ),
    }
    if isinstance(cwd_policy, ModelChooses):
        fields["cwd"] = (str, Field(description="Working directory inside container"))
    elif isinstance(cwd_policy, DefaultValue):
        fields["cwd"] = (
            str | None,
            Field(default=None, description=f"Working directory inside container (None = {cwd_policy.value!s})"),
        )
    if allow_env:
        fields["env"] = (
            list[EnvVar] | None,
            Field(description="Environment variables as ['NAME=value', ...] (None = inherit container env)"),
        )
    if allow_user:
        fields["user"] = (str | None, Field(description="Username inside container (None = container default)"))
    return create_model("ExecInput", __base__=OpenAIStrictModeBaseModel, **fields)


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

    # Resource URI constants
    CONTAINER_INFO_URI = "resource://container.info"

    # Tool reference (assigned in __init__ after tool registration)
    exec_tool: FlatTool

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

        # Register exec tool - name derived from function name.
        # Input model is created dynamically based on allow_user_field / allow_env_field / cwd_policy;
        # annotation is set after definition so FastMCP sees the correct schema.
        exec_input_type = _make_exec_input_model(
            allow_user=allow_user_field, allow_env=allow_env_field, cwd_policy=cwd_policy
        )

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
            '- DO: {"cmd": ["sh", "-c", "cat > file.txt"], "stdin_text": "content"}',
        ]

        if isinstance(cwd_policy, AlwaysSetTo):
            _exec_doc_lines.append(f"\nCommands always run in {cwd_policy.value}.")
        _exec_description = "\n".join(_exec_doc_lines)

        async def exec(input, context: Context) -> BaseExecResult:
            async with async_timer() as get_duration_ms:
                session = session_state_from_ctx(context)
                effective = ResolvedExecInput(
                    cmd=input.cmd,
                    cwd=resolve_cwd(cwd_policy, input),
                    timeout_ms=input.timeout_ms,
                    env=getattr(input, "env", None) if allow_env_field else None,
                    user=getattr(input, "user", None) if allow_user_field else None,
                )
                (stdout_buf, stderr_buf, exit_code, timed_out) = await run_session_container(
                    session, effective.cmd, effective
                )
                duration_ms = get_duration_ms()
                return render_container_result(stdout_buf, stderr_buf, exit_code, timed_out, duration_ms)

        exec.__annotations__["input"] = exec_input_type
        self.exec_tool = self.flat_model(description=_exec_description)(exec)

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
        async def read_image(input: ReadImageInput, ctx: Context) -> list[mcp_types.ImageContent]:
            """Read an image file from the container and return it for the model to see. Supported formats: JPEG, PNG, GIF, WebP."""
            container = _get_running_container(ctx)
            # Pull file from container via Docker API
            tar = await container.get_archive(input.path)
            member_name = PurePosixPath(input.path).name
            member = tar.getmember(member_name)
            f = tar.extractfile(member)
            if f is None:
                raise ValueError(f"{input.path} is not a regular file")
            return [validate_and_encode_image(f.read(), input.path)]

        self.read_image_tool = self.flat_model()(read_image)
