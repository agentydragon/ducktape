from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sys

import anyio
import docker
from docker import DockerClient
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from adgn.mcp.compositor.server import Compositor
from adgn.mcp.exec.models import (
    BaseExecResult,
    StreamReadResult,
    TimeoutMs,
    async_timer,
    read_stream_limited_async,
    render_raw_to_result,
)
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP
from adgn.seatbelt.model import EnvPassthroughMode, SBPLPolicy
from adgn.seatbelt.runner import apopen, collect_unified_sandbox_denies

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
    max_bytes: int = Field(..., ge=0, le=100_000, description="0..100_000; applies to stdin and captures")
    cwd: Path | None = None
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


class SeatbeltExecMCP(NotifyingFastMCP):
    def __init__(
        self, name: str = SERVER_NAME, *, agent_id: str | None = None, persistence=None, docker_client: DockerClient
    ) -> None:
        # Refuse to instantiate on non-darwin
        if sys.platform != "darwin":
            raise RuntimeError("seatbelt_exec is macOS-only (requires sandbox-exec)")
        super().__init__(
            name,
            instructions=("Execute commands via macOS seatbelt (sandbox-exec). Provide a full SBPL policy per call."),
        )
        if not agent_id:
            raise ValueError("SeatbeltExecMCP requires agent_id")
        self._agent_id = agent_id
        self._SBPL = TypeAdapter(SBPLPolicy)
        self._docker = docker_client

        # Register sandbox_exec tool
        @self.flat_model()
        async def sandbox_exec(input: SandboxExecArgs) -> SandboxExecResult:
            """Execute a command via macOS seatbelt (sandbox-exec). Provide a full SBPL policy per call."""
            # Platform precheck
            if sys.platform != "darwin":
                raise ToolError("NOT_DARWIN: sandbox available only on macOS")

            # Pydantic has already validated argv min length and max_bytes range
            max_b = input.max_bytes

            cwd_path = input.cwd.resolve() if isinstance(input.cwd, Path) else None

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
                child_env.update({k: str(v) for k, v in input.env.items()})

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

        # ---- Template management tools ----
        # No template management or resources: stateless server


async def attach_seatbelt_exec(
    comp: Compositor, *, agent_id: str, persistence, docker_client: DockerClient, name: str = SERVER_NAME
):
    server = SeatbeltExecMCP(name, agent_id=agent_id, persistence=persistence, docker_client=docker_client)
    await comp.mount_inproc(name, server)
    return server


def main() -> None:
    """Stdio main for seatbelt exec server."""
    agent_id = os.environ.get("ADGN_AGENT_ID") or "seatbelt-dev"
    dcli = docker.from_env()
    server = SeatbeltExecMCP(SERVER_NAME, agent_id=agent_id, persistence=None, docker_client=dcli)
    anyio.run(server.run_stdio_async)
