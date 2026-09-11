"""Bearer-protected FastMCP server for one-shot SSH execution."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any

import paramiko
import uvicorn
import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from mcp_infra.static_bearer import StaticBearerGuard

logger = logging.getLogger(__name__)
_MAX_OUTPUT_BYTES = 100_000


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1, max_length=253)
    user: str = Field(min_length=1, max_length=128)
    identity_file: Path
    port: int = Field(default=22, ge=1, le=65535)


class SshSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    known_hosts_file: Path
    targets: list[TargetConfig] = Field(min_length=1)
    command_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=300)
    output_limit_bytes: int = Field(default=64 * 1024, ge=1, le=_MAX_OUTPUT_BYTES)
    max_concurrent_executions: int = Field(default=4, ge=1, le=64)

    @field_validator("targets")
    @classmethod
    def unique_targets(cls, targets: list[TargetConfig]) -> list[TargetConfig]:
        tuples = [(target.host, target.user) for target in targets]
        if len(set(tuples)) != len(tuples):
            raise ValueError("SSH target (host, user) tuples must be unique")
        return targets


_SETTINGS = TypeAdapter(SshSettings)


class TargetStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    user: str
    available: bool
    error: dict[str, str] | None = None


class ExecResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    user: str
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


_READ_ONLY = ToolAnnotations(readOnlyHint=True)


def load_settings(path: Path) -> SshSettings:
    with path.open() as stream:
        return _SETTINGS.validate_python(yaml.safe_load(stream))


def _error(kind: str, message: str) -> ToolError:
    return ToolError(json.dumps({"kind": kind, "message": message}, separators=(",", ":")))


def _target_status(settings: SshSettings, target: TargetConfig) -> TargetStatus:
    if not settings.known_hosts_file.is_file():
        return TargetStatus(
            host=target.host,
            user=target.user,
            available=False,
            error={"kind": "known_hosts_unavailable", "message": "configured SSH host-key file is unavailable"},
        )
    if not target.identity_file.is_file() or not os.access(target.identity_file, os.R_OK):
        return TargetStatus(
            host=target.host,
            user=target.user,
            available=False,
            error={"kind": "identity_file_unavailable", "message": "configured SSH identity is unavailable"},
        )
    return TargetStatus(host=target.host, user=target.user, available=True)


def build_mcp(settings: SshSettings) -> FastMCP:
    mcp = FastMCP(
        "agentplane-ssh", instructions="Execute approved non-interactive commands over SSH on configured targets."
    )
    execution_slots = asyncio.Semaphore(settings.max_concurrent_executions)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_targets() -> list[TargetStatus]:
        """List configured SSH machine/user targets and whether their local key is available."""
        return [_target_status(settings, target) for target in settings.targets]

    @mcp.tool
    async def exec(
        host: Annotated[str, Field(min_length=1, description="Configured SSH target host.")],
        user: Annotated[str, Field(min_length=1, description="Configured SSH target Unix user.")],
        command: Annotated[str, Field(min_length=1, description="Remote command string to execute.")],
        timeout_seconds: Annotated[
            int | None, Field(default=None, ge=1, description="Optional timeout bounded by server configuration.")
        ] = None,
    ) -> ExecResult:
        """Execute one non-interactive remote command on a configured host/user pair."""
        target = next((item for item in settings.targets if item.host == host and item.user == user), None)
        if target is None:
            raise _error("target_not_configured", "SSH host/user target is not configured")
        status = _target_status(settings, target)
        if not status.available:
            assert status.error is not None
            raise _error(status.error["kind"], status.error["message"])
        timeout = settings.command_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout > settings.command_timeout_seconds:
            raise _error("timeout_exceeds_maximum", "requested timeout exceeds configured maximum")
        try:
            async with execution_slots:
                stdout, stderr, exit_code = await asyncio.wait_for(
                    asyncio.to_thread(_run_ssh, target, settings, command, timeout), timeout=timeout
                )
        except TimeoutError:
            raise _error("execution_unknown", "SSH execution timed out after it may have started") from None
        except _SshUnknownError:
            raise _error("execution_unknown", "SSH connection ended after execution may have started") from None
        except _SshFailureError as error:
            raise _error(error.kind, error.message) from None
        return ExecResult(
            host=host,
            user=user,
            exit_code=exit_code,
            stdout=stdout[0],
            stderr=stderr[0],
            stdout_truncated=stdout[1],
            stderr_truncated=stderr[1],
        )

    return mcp


class _SshFailureError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind, self.message = kind, message


class _SshUnknownError(Exception):
    pass


def _run_ssh(
    target: TargetConfig, settings: SshSettings, command: str, timeout: int
) -> tuple[tuple[str, bool], tuple[str, bool], int]:
    client = paramiko.SSHClient()
    try:
        client.load_host_keys(str(settings.known_hosts_file))
    except (OSError, paramiko.SSHException) as error:
        raise _SshFailureError("known_hosts_unavailable", "SSH host-key file could not be loaded") from error
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        try:
            client.connect(
                target.host,
                port=target.port,
                username=target.user,
                key_filename=str(target.identity_file),
                timeout=settings.connect_timeout_seconds,
                banner_timeout=settings.connect_timeout_seconds,
                auth_timeout=settings.connect_timeout_seconds,
                allow_agent=False,
                look_for_keys=False,
            )
        except (paramiko.AuthenticationException, paramiko.BadHostKeyException) as error:
            raise _SshFailureError(
                "ssh_authentication_failed", "SSH authentication or host-key verification failed"
            ) from error
        except (paramiko.SSHException, OSError) as error:
            raise _SshFailureError(
                "ssh_connection_failed", "SSH connection failed before command completion"
            ) from error
        try:
            _, stdout_stream, stderr_stream = client.exec_command(command, timeout=timeout, get_pty=False)
            with ThreadPoolExecutor(max_workers=2) as pool:
                stdout_future = pool.submit(_read_bounded_sync, stdout_stream, settings.output_limit_bytes)
                stderr_future = pool.submit(_read_bounded_sync, stderr_stream, settings.output_limit_bytes)
                stdout = stdout_future.result()
                stderr = stderr_future.result()
            exit_code = stdout_stream.channel.recv_exit_status()
        except (TimeoutError, paramiko.SSHException, OSError) as error:
            raise _SshUnknownError from error
        return stdout, stderr, exit_code
    finally:
        client.close()


def _read_bounded_sync(stream: Any, limit: int) -> tuple[str, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        remaining = limit - total
        if remaining > 0:
            kept = chunk[:remaining]
            chunks.append(kept)
            total += len(kept)
        if len(chunk) > max(0, remaining):
            truncated = True
    return b"".join(chunks).decode("utf-8", errors="replace"), truncated


def create_app(settings: SshSettings, bearer: str) -> Starlette:
    mcp_app = build_mcp(settings).http_app(path="/mcp", stateless_http=True)

    async def healthz(request: Request) -> JSONResponse:
        del request
        return JSONResponse({"ok": True})

    protected: ASGIApp = StaticBearerGuard(mcp_app, token=bearer)
    return Starlette(routes=[Route("/healthz", healthz), Mount("/", app=protected)], lifespan=mcp_app.lifespan)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr)
    settings = load_settings(Path(os.environ["SSH_MCP_CONFIG_FILE"]))
    app = create_app(settings, os.environ["SSH_MCP_BEARER_TOKEN"])
    uvicorn.run(app, host=os.environ.get("SSH_MCP_HOST", "0.0.0.0"), port=int(os.environ.get("SSH_MCP_PORT", "8080")))


if __name__ == "__main__":
    main()
