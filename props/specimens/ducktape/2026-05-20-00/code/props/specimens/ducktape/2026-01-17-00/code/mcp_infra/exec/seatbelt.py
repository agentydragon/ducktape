from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import anyio
import mcp.types as mcp_types
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool
from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.models import (
    BaseExecResult,
    StreamReadResult,
    TimeoutMs,
    async_timer,
    read_stream_limited_async,
    render_raw_to_result,
)
from mcp_infra.exec.read_image import ReadImageInput, validate_and_encode_image
from mcp_infra.seatbelt.model import EnvPassthroughMode, SBPLPolicy
from mcp_infra.seatbelt.runner import apopen, collect_unified_sandbox_denies

SERVER_NAME = "seatbelt_exec"


logger = logging.getLogger(__name__)


# Default env whitelist used when passing through only a safe subset.
DEFAULT_ENV_WHITELIST: tuple[str, ...] = (
    "HOME",
    "LOGNAME",
    "PATH",
    "SHELL",
    "USER",
    "USERNAME",
    "TMPDIR",
    "TEMP",
    "TMP",
)


class SandboxExecArgs(BaseModel):
    # Stateless: require a full policy on every call
    policy: SBPLPolicy
    argv: list[str] = Field(min_length=1)
    max_bytes: int = Field(..., ge=0, le=100_000, description="Applies to stdin and captures")
    # str not Path: OpenAI strict mode doesn't accept format="path" in JSON schemas
    cwd: str | None = None
    # Explicit env to set/override in the child (applied after policy.env passthrough base)
    env: dict[str, str] | None = None
    timeout_ms: TimeoutMs
    trace: bool = False
    stdin_text: str | None = None

    model_config = ConfigDict(extra="forbid")


class SandboxExecResult(BaseExecResult):
    """Exec result for sandbox_exec with additional sandbox metadata.

    Inherits core exec fields (exit, stdout, stderr, duration_ms) from BaseExecResult.
    """

    trace_text: str | None = None
    unified_sandbox_denies_text: str | None = None


class SeatbeltExecServer(EnhancedFastMCP):
    """Seatbelt exec MCP server with typed tool access (macOS only)."""

    # Tool references (assigned in __init__)
    sandbox_exec_tool: FunctionTool

    def __init__(self):
        """Create a seatbelt exec MCP server (macOS only).

        Raises:
            RuntimeError: If not on macOS
        """
        # Refuse to instantiate on non-darwin
        if sys.platform != "darwin":
            raise RuntimeError("seatbelt_exec is macOS-only (requires sandbox-exec)")

        super().__init__(
            "Seatbelt Exec MCP Server",
            instructions=("Execute commands via macOS seatbelt (sandbox-exec). Provide a full SBPL policy per call."),
        )

        async def sandbox_exec(input: SandboxExecArgs) -> SandboxExecResult:
            """Execute a command via macOS seatbelt (sandbox-exec). Provide a full SBPL policy per call."""
            # Platform precheck
            if sys.platform != "darwin":
                raise ToolError("NOT_DARWIN: sandbox available only on macOS")

            # Pydantic has already validated argv min length and max_bytes range
            max_b = input.max_bytes

            cwd_path = Path(input.cwd).resolve() if input.cwd else None

            # Stateless: require inline policy (validated by Pydantic)
            policy = input.policy

            # Prepare stdin bytes (clamped to max_bytes); no metadata returned for stdin
            stdin_b = input.stdin_text.encode("utf-8", errors="replace") if input.stdin_text else b""

            # Compute child environment based on policy.env (default: whitelist with safe defaults),
            # then overlay any explicit env values provided in the request.
            env_parent = os.environ

            mode = policy.env.mode
            wl = policy.env.whitelist or list(DEFAULT_ENV_WHITELIST)
            if mode == EnvPassthroughMode.ALL:
                child_env: dict[str, str] = dict(env_parent)
            else:
                child_env = {k: v for k, v in env_parent.items() if k in wl}
            if input.env:
                child_env.update(input.env)

            # Run with apopen so we can enforce timeout and kill if needed
            try:
                async with async_timer() as get_duration_ms:
                    async with await apopen(
                        input.argv,
                        policy,
                        cwd=cwd_path,
                        env=child_env,
                        trace=input.trace,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    ) as proc:
                        loop = asyncio.get_running_loop()
                        start = loop.time()
                        total_secs = max(0.001, float(input.timeout_ms) / 1000.0)
                        deadline = start + total_secs

                        def _remaining() -> float:
                            return max(0.0, deadline - loop.time())

                        # Kick off reads first; then write stdin; this avoids fill/lock
                        stdout_task = asyncio.create_task(read_stream_limited_async(proc.stdout, store_limit=max_b))
                        stderr_task = asyncio.create_task(read_stream_limited_async(proc.stderr, store_limit=max_b))

                        # Write stdin (if any), then close to signal EOF
                        try:
                            if proc.stdin is not None:
                                if stdin_b:
                                    proc.stdin.write(stdin_b)
                                    await proc.stdin.drain()
                                proc.stdin.close()
                        except Exception as e:
                            # Record the failure but do not crash; exit code/streams will reflect errors
                            logger.debug("stdin write/close failed: %s", e)

                        timed_out = False
                        try:
                            # Wait for stream drains and process exit with timeout
                            # Enforce a single overall timeout budget across both awaits
                            t1 = _remaining()
                            await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=t1)
                            t2 = _remaining()
                            await asyncio.wait_for(proc.wait(), timeout=t2)
                        except TimeoutError:
                            # Best-effort termination; __aexit__ will also ensure cleanup
                            timed_out = True
                            try:
                                proc.kill()
                            except Exception as e:
                                logger.debug("proc.kill() failed: %s", e)
                            try:
                                await proc.wait()
                            except Exception as e:
                                logger.debug("proc.wait() after kill failed: %s", e)

                    duration_ms = get_duration_ms()

                # Collect stream results (completed or cancelled → default empty)
                def _done_result(t: asyncio.Task[StreamReadResult]) -> StreamReadResult:
                    if t.done() and not t.cancelled() and t.exception() is None:
                        return t.result()
                    return StreamReadResult(stored_bytes=b"", truncated=False, total_bytes=0)

                out_res = _done_result(stdout_task)
                err_res = _done_result(stderr_task)

                # Use the shared rendering logic from BaseExecResult
                base_result = render_raw_to_result(
                    stdout=out_res.stored_bytes,
                    stderr=err_res.stored_bytes,
                    exit_code=proc.returncode,
                    timed_out=timed_out,
                    max_bytes=max_b,
                    duration_ms=duration_ms,
                )

                trace_text: str | None = None
                if input.trace and proc.trace_file and proc.trace_file.exists():
                    try:
                        trace_text = proc.trace_file.read_text(errors="replace")
                    except Exception as e:
                        logger.debug("failed to read trace file: %s", e)
                        trace_text = None

                # Disabled for now: unified sandbox denies are noisy/unscoped.
                unified_text: str | None = None
                if False:
                    try:
                        _p, unified_text = collect_unified_sandbox_denies(proc.artifacts_dir)
                    except Exception as e:
                        logger.debug("collect unified denies failed: %s", e)
                        unified_text = None

                return SandboxExecResult.from_rendered_streams(
                    exit_status=base_result.exit,
                    stdout=base_result.stdout,
                    stderr=base_result.stderr,
                    duration_ms=duration_ms,
                    trace_text=trace_text,
                    unified_sandbox_denies_text=unified_text,
                )
            except FileNotFoundError as e:
                # sandbox-exec missing
                raise ToolError(f"SANDBOX_EXEC_MISSING: {e}") from e
            except Exception as e:
                raise ToolError(str(e)) from e

        self.sandbox_exec_tool = self.flat_model()(sandbox_exec)

        def read_image(input: ReadImageInput) -> list[mcp_types.ImageContent]:
            """Read an image file and return it for the model to see."""
            # TODO: should respect seatbelt sandbox boundaries
            p = Path(input.path)
            if not p.is_file():
                raise ValueError(f"Not a file: {input.path}")
            return [validate_and_encode_image(p.read_bytes(), input.path)]

        self.read_image_tool = self.flat_model()(read_image)


def main() -> None:
    """Stdio main for seatbelt exec server."""
    server = SeatbeltExecServer()
    anyio.run(server.run_stdio_async)
