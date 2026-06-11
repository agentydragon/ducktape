"""
Manual MCP test script for using Claude Code Python SDK
Tests both stdio and SSE modes with comprehensive tool validation

Requirement packages: claude-code rich
"""

import asyncio
import errno
import logging
import os
import socket
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient
from claude_code_sdk._internal.transport import subprocess_cli
from claude_code_sdk.types import McpSSEServerConfig, McpStdioServerConfig
from rich.console import Console
from rich.logging import RichHandler

# UNFORTUNATE MONKEY PATCH: Add strict MCP mode support to Claude Code SDK
#
# BACKGROUND: The Claude Code CLI supports --strict-mcp-config to isolate MCP tool usage
# to only the servers specified in --mcp-config, preventing loading of global MCP servers.
# However, the Python SDK (claude_code_sdk) does not expose this option in ClaudeCodeOptions.
#
# IMPACT: Without --strict-mcp-config, the SDK loads ALL configured MCP servers from the
# user's global configuration (~/.claude/config.json), defeating our security isolation
# that should only allow MCP Starter Template tools.
#
# WORKAROUND: Monkey patch the SubprocessCLITransport._build_command method to inject
# --strict-mcp-config when custom MCP servers are configured.
#
# TODO: This should be fixed upstream in claude_code_sdk by adding a strict_mcp_config
# parameter to ClaudeCodeOptions. This monkey patch can be removed once that's available.

original_build_command = subprocess_cli.SubprocessCLITransport._build_command


def _build_command_with_strict_mcp(self: subprocess_cli.SubprocessCLITransport) -> list[str]:
    """Build CLI command with --strict-mcp-config option."""
    cmd = original_build_command(self)
    # Add strict MCP config if we have MCP servers configured
    if self._options.mcp_servers:
        cmd.append("--strict-mcp-config")
    return cmd


# Apply the monkey patch - this modifies the SDK's internal behavior
subprocess_cli.SubprocessCLITransport._build_command = _build_command_with_strict_mcp  # type: ignore[method-assign]


console = Console()

# Set up logging with Rich handler for pretty output
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True)],
)

logger = logging.getLogger(__name__)


# Configuration
# Starting port to search from
SSE_PORT_START = 8002

# SECURITY: Only allow MCP tools from mcp-starter server
# This explicitly disables ALL built-in tools (Edit, Read, Write, Bash, etc.)
# by only whitelisting specific MCP tools
# Note: Tool names are prefixed with the MCP server name
ALLOWED_TOOLS = [
    "mcp__mcp_starter__greet",
    "mcp__mcp_starter__get_text_chunks",
    "mcp__mcp_starter__generate_sample_image",
]

# Disallow all built-in and other MCP tools to enforce strict isolation
DISALLOWED_TOOLS = [
    "Task",
    "Bash",
    "Glob",
    "Grep",
    "LS",
    "exit_plan_mode",
    "Read",
    "Edit",
    "MultiEdit",
    "Write",
    "NotebookRead",
    "NotebookEdit",
    "WebFetch",
    "TodoWrite",
]

# Test cases for comprehensive validation
TEST_CASES = [
    {
        "name": "Basic Greeting",
        "prompt": "Use greet to greet 'Alice'. What's the result? End your response with either 'PASS' if the tool worked correctly and returned 'hello, Alice', or 'FAIL' if it didn't work or returned the wrong result.",
    },
    {
        "name": "Different Name Greeting",
        "prompt": "Use greet to greet 'Bob'. Show the greeting result. End your response with either 'PASS' if the tool worked correctly and returned 'hello, Bob', or 'FAIL' if it didn't work or returned the wrong result.",
    },
    {
        "name": "Text Chunking",
        "prompt": "Use get_text_chunks to split 'Hello world this is a test' into chunks of size 5. Show the chunk structure. End your response with either 'PASS' if the tool worked correctly and returned proper chunks, or 'FAIL' if it didn't work correctly.",
    },
    {
        "name": "Small Chunks",
        "prompt": "Use get_text_chunks to split 'Testing' into chunks of size 3. What chunks do you get? End your response with either 'PASS' if the tool worked correctly and returned proper chunks, or 'FAIL' if it didn't work correctly.",
    },
    {
        "name": "Image Generation",
        "prompt": "Use generate_sample_image to create a sample image. What format is returned? End your response with either 'PASS' if the tool worked correctly and returned image data, or 'FAIL' if it didn't work correctly.",
    },
]


def find_unused_port(start_port: int = SSE_PORT_START, max_attempts: int = 100) -> int:
    """Find an unused port starting from start_port"""
    for port in range(start_port, start_port + max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("localhost", port))
                logger.info(f"[blue]Found unused port: {port}[/blue]")
                return port
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                continue  # Port in use, try next one
            # Unexpected error (permission denied, etc.)
            logger.error(f"[red]Unexpected error trying port {port}: {e}[/red]")
            raise

    raise RuntimeError(f"Could not find unused port in range {start_port}-{start_port + max_attempts}")


@asynccontextmanager
async def sse_server_manager() -> AsyncIterator[McpSSEServerConfig]:
    """Context manager for SSE server lifecycle - yields valid MCP config when ready"""

    # Find an unused port
    sse_port = find_unused_port()

    # Set up logging
    sse_log_file = Path(tempfile.gettempdir()) / "mcp_starter_sse_server.log"

    logger.info(f"[blue]Starting SSE server on port {sse_port}. Logs: {sse_log_file}[/blue]")

    # Set up clean isolated environment
    server_env = {}
    for var in ["PATH", "PYTHONPATH", "HOME", "USER"]:
        if var in os.environ:
            server_env[var] = os.environ[var]

    server_env.update(
        {
            "FASTMCP_LOG_LEVEL": "DEBUG",
            "PYTHONUNBUFFERED": "1",
            "CLAUDE_CONFIG_PATH": "/nonexistent",
            "CLAUDE_CODE_CONFIG_PATH": "/nonexistent",
        }
    )

    server_process = None
    try:
        # Start server process asynchronously
        with sse_log_file.open("w") as log_file:
            server_process = await asyncio.create_subprocess_exec(
                "adgn-mcp-starter",
                "--transport",
                "sse",
                "--port",
                str(sse_port),
                "--host",
                "localhost",
                "--debug",
                stdout=log_file,
                stderr=log_file,
                env=server_env,
            )

        # Wait for server to start
        await asyncio.sleep(3)

        # Check if server failed immediately
        if server_process.returncode is not None:
            raise RuntimeError("SSE server failed to start")

        # Simple startup delay - real test is whether Claude SDK can connect
        logger.info("[blue]Waiting for SSE server to fully initialize...[/blue]")
        await asyncio.sleep(2)

        # Final check that server is still running
        if server_process.returncode is not None:
            raise RuntimeError("SSE server died during startup")

        logger.info("[green]✓ SSE server started[/green]")

        yield McpSSEServerConfig(type="sse", url=f"http://localhost:{sse_port}/sse")

    except Exception:
        # Read the log file for debugging & re-raise
        try:
            log_content = sse_log_file.read_text()
            logger.error(f"[red]SSE server log content:\n{log_content}[/red]")
        except Exception as log_e:
            logger.warning(f"[yellow]Could not read SSE server log: {log_e}[/yellow]")
        raise

    finally:
        if server_process:
            logger.info("[blue]Stopping SSE server...[/blue]")
            server_process.terminate()
            try:
                await asyncio.wait_for(server_process.wait(), timeout=5)
            except TimeoutError:
                server_process.kill()
                await server_process.wait()

        logger.info(f"[blue]SSE server log file for inspection: {sse_log_file}[/blue]")


async def run_test_suite(mcp_server_config: McpStdioServerConfig | McpSSEServerConfig, mode_name: str) -> None:
    """Run the complete test suite using the provided MCP server config"""
    # Create options with strict MCP isolation
    temp_dir = tempfile.mkdtemp()
    mcp_servers: dict[str, Any] = {"mcp_starter": mcp_server_config}
    options = ClaudeCodeOptions(
        mcp_servers=mcp_servers,
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=DISALLOWED_TOOLS,  # Block built-in tools
        permission_mode="acceptEdits",
        # Run Claude process in clean temporary directory
        cwd=temp_dir,
    )

    # Use async context manager for proper session management
    async with ClaudeSDKClient(options=options) as client:
        session_id = str(uuid.uuid4())

        passed_tests = 0
        failed_tests = 0

        # Run all test cases
        for i, test_case in enumerate(TEST_CASES, 1):
            logger.info(f"[blue]Test {i}: {test_case['name']}[/blue]")
            console.print(f"[dim]Prompt: {test_case['prompt']}[/dim]")
            console.print("Streaming response:")

            # Send query
            await client.query(prompt=test_case["prompt"], session_id=session_id)

            # Collect full response
            # TODO(mpokorny): Accumulate only textual fields (avoid arbitrary str(message) concatenation); keep as-is for now.
            full_response = ""
            async for message in client.receive_response():
                console.print(message, end="")
                full_response += str(message)

            console.print()  # Add spacing between tests

            # Validate response
            if "PASS" in full_response:
                logger.info(f"[green]✓ Test {i} ({test_case['name']}): PASSED[/green]")
                passed_tests += 1
            elif "FAIL" in full_response:
                logger.error(f"[red]✗ Test {i} ({test_case['name']}): FAILED[/red]")
                failed_tests += 1
            else:
                logger.error(f"[red]✗ Test {i} ({test_case['name']}): INVALID (no PASS/FAIL found)[/red]")
                failed_tests += 1

            console.print()  # Add spacing between tests

        # Report final results
        console.print(f"\n[bold]{mode_name} Test Results:[/bold]")
        console.print(f"[green]Passed: {passed_tests}[/green]")
        console.print(f"[red]Failed: {failed_tests}[/red]")
        console.print(f"[blue]Total: {passed_tests + failed_tests}[/blue]")

        if failed_tests == 0:
            logger.info(f"[green]✓ All {mode_name} mode tests completed successfully[/green]")
        else:
            logger.error(f"[red]✗ {failed_tests} out of {passed_tests + failed_tests} {mode_name} tests failed[/red]")


async def main() -> None:
    """Main test function"""
    logger.info("[blue]Starting MCP Starter Template tests with Claude Code SDK...[/blue]")

    logger.info("[blue]Testing STDIO mode with Claude Code SDK...[/blue]")
    await run_test_suite(McpStdioServerConfig(command="adgn-mcp-starter", args=["--debug"]), "STDIO SDK")

    logger.info("[blue]Testing SSE mode with Claude Code SDK...[/blue]")
    async with sse_server_manager() as mcp_server_config:
        await run_test_suite(mcp_server_config, "SSE SDK")

    logger.info("[green]✓ All test suites completed[/green]")


if __name__ == "__main__":
    asyncio.run(main())
