"""Run init scripts in containerized agents.

The init script (/init) is executed in the container and its stdout becomes
the agent's system prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.client import Client

from agent_pkg.host.builder import IMAGE_INIT_PATH
from mcp_infra.exec.models import BaseExecResult, ExecInput, Exited, TruncatedStream
from mcp_infra.naming import build_mcp_function

if TYPE_CHECKING:
    from mcp_infra.exec.docker.server import ContainerExecServer
    from mcp_infra.mounted import Mounted

__all__ = ["DEFAULT_INIT_TIMEOUT_MS", "InitFailedError", "run_init_script"]


class InitFailedError(Exception):
    """Raised when init script fails (non-zero exit, truncated output, or MCP error)."""

    exec_result: BaseExecResult | None

    def __init__(self, message: str, *, exec_result: BaseExecResult | None = None):
        full_message = message
        if exec_result is not None:
            stdout = exec_result.stdout
            stderr = exec_result.stderr
            stdout_text = stdout.truncated_text if isinstance(stdout, TruncatedStream) else stdout
            stderr_text = stderr.truncated_text if isinstance(stderr, TruncatedStream) else stderr
            full_message = f"{message}\n\nSTDOUT:\n{stdout_text}\n\nSTDERR:\n{stderr_text}"
        super().__init__(full_message)
        self.exec_result = exec_result


# Default timeout for init script (1 minute)
DEFAULT_INIT_TIMEOUT_MS = 60_000


async def run_init_script(
    mcp_client: Client, runtime: Mounted[ContainerExecServer], *, timeout_ms: int = DEFAULT_INIT_TIMEOUT_MS
) -> str:
    """Run /init script and return stdout as system prompt.

    Executes the init script in the container before the agent loop starts.
    The init script should print the complete system prompt to stdout.

    Args:
        mcp_client: MCP client connected to the compositor
        runtime: Mounted container exec server
        timeout_ms: Timeout in milliseconds (default: 60s)

    Returns:
        stdout from init script (becomes system prompt)

    Raises:
        InitFailedError: If init fails (non-zero exit, truncated, timeout, error)
    """
    # Build the tool call name
    tool_name = build_mcp_function(runtime.prefix, runtime.server.exec_tool.name)

    # Call the exec tool directly
    result = await mcp_client.call_tool(
        tool_name,
        ExecInput(cmd=[IMAGE_INIT_PATH], cwd=None, env=None, user=None, timeout_ms=timeout_ms).model_dump(mode="json"),
    )

    # Check for MCP-level errors
    if result.is_error:
        error_text = str(result.structured_content) if result.structured_content else "unknown error"
        raise InitFailedError(f"Init script MCP error: {error_text}")

    # Parse structured result
    if not result.structured_content:
        raise InitFailedError("Init script returned no structured content")

    exec_result = BaseExecResult.model_validate(result.structured_content)

    # Validate exit code
    if not (isinstance(exec_result.exit, Exited) and exec_result.exit.exit_code == 0):
        stderr_text = (
            exec_result.stderr.truncated_text if isinstance(exec_result.stderr, TruncatedStream) else exec_result.stderr
        )
        stderr_preview = stderr_text[:500] if stderr_text else ""
        raise InitFailedError(
            f"Init script failed: {exec_result.exit.model_dump()}\nstderr: {stderr_preview}", exec_result=exec_result
        )

    # Check for truncation
    if isinstance(exec_result.stdout, TruncatedStream):
        raise InitFailedError(
            f"Init script stdout truncated: {exec_result.stdout.model_dump()}", exec_result=exec_result
        )
    if isinstance(exec_result.stderr, TruncatedStream):
        raise InitFailedError(
            f"Init script stderr truncated: {exec_result.stderr.model_dump()}", exec_result=exec_result
        )

    return exec_result.stdout
