"""Run one bounded, non-interactive script inside a Kubernetes Pod over ``pods/exec``.

The Kubernetes backend beside this package's other exec substrates (``direct``, ``docker``,
``bwrap``): the caller supplies a Pod that already exists and this runs a script in one of its
containers, bounding wall-clock time and retained output. It owns no Pod lifecycle -- whoever
created the Pod decides when it goes away.

`pods/exec` is a WebSocket upgrade carrying multiplexed channels, so a timeout has to close the
socket rather than signal anything: the remote process keeps running until its container does.
That is why `script` is wrapped in `timeout` remotely as well, and why the exit status distinguishes
a remote `TimedOut` from a `Killed`.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol, cast

from aiohttp import WSMessage, WSMsgType, WSServerHandshakeError
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import ApiException, Configuration
from kubernetes_asyncio.stream import WsApiClient
from kubernetes_asyncio.stream.ws_client import ERROR_CHANNEL, STDERR_CHANNEL, STDOUT_CHANNEL

from mcp_infra.exec.models import ExecStream, Exited, Killed, TimedOut, TruncatedStream

logger = logging.getLogger(__name__)


class PodExecError(Exception):
    """The exec could not be carried out: the API server refused it, or the transport failed.

    A command that ran and exited nonzero is not this -- that is an ordinary `CommandResult`. Each
    caller maps this onto whatever its own surface owes a client; nothing here knows about MCP.
    """


def _handshake_error(status: int, message: str) -> str:
    """Translate an opaque exec WebSocket handshake rejection into an actionable message.

    aiohttp surfaces a bare ``WSServerHandshakeError`` ("invalid response status") that hides
    the apiserver's reason, so name the most likely cause per HTTP status.
    """
    detail = f"HTTP {status}" + (f" {message}" if message else "")
    match status:
        case 403:
            cause = (
                "the calling ServiceAccount lacks `get pods/exec` in the pod's namespace — "
                "kubernetes_asyncio opens exec with an HTTP GET, so it needs the `get` verb "
                "(kubectl POSTs and needs `create`)"
            )
        case 401:
            cause = "the ServiceAccount token was rejected"
        case _:
            cause = "check that the pod exists and its target container is ready"
    return f"Kubernetes rejected the exec WebSocket handshake ({detail}); {cause}"


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit: Exited | TimedOut | Killed
    stdout: ExecStream
    stderr: ExecStream
    duration_seconds: float


class ExecRunner(Protocol):
    async def run(
        self,
        *,
        pod_name: str,
        namespace: str,
        container: str,
        script: str,
        cwd: str,
        max_output_bytes: int,
        timeout_seconds: int,
    ) -> CommandResult: ...


class _ExecWebSocket(Protocol):
    async def receive(self) -> WSMessage: ...

    def exception(self) -> BaseException | None: ...


class _ExecWebSocketContext(Protocol):
    async def __aenter__(self) -> _ExecWebSocket: ...

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> bool | None: ...


class _PodExecClient(Protocol):
    async def connect_get_namespaced_pod_exec(
        self,
        name: str,
        namespace: str,
        *,
        command: list[str],
        container: str,
        stderr: bool,
        stdin: bool,
        stdout: bool,
        tty: bool,
        _preload_content: bool,
    ) -> _ExecWebSocketContext: ...


@dataclass(slots=True)
class _Capture:
    limit: int
    stored: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        remaining = self.limit - len(self.stored)
        if remaining > 0:
            self.stored.extend(chunk[:remaining])

    def render(self) -> ExecStream:
        text = bytes(self.stored).decode("utf-8", errors="replace")
        if self.total_bytes > len(self.stored):
            return TruncatedStream(truncated_text=text, total_bytes=self.total_bytes)
        return text


class KubernetesWebSocketExecRunner:
    """Run one bounded, non-interactive Bash script through ``pods/exec``."""

    def __init__(self, configuration: Configuration) -> None:
        self._configuration = configuration

    async def run(
        self,
        *,
        pod_name: str,
        namespace: str,
        container: str,
        script: str,
        cwd: str,
        max_output_bytes: int,
        timeout_seconds: int,
    ) -> CommandResult:
        stdout = _Capture(max_output_bytes)
        stderr = _Capture(max_output_bytes)
        error_data = bytearray()
        shell_script = f"cd -- {shlex.quote(cwd)}\n{script}"
        command = [
            "/usr/bin/timeout",
            "--signal=TERM",
            "--kill-after=5s",
            f"{timeout_seconds}s",
            "bash",
            "-lc",
            shell_script,
        ]
        loop = asyncio.get_running_loop()
        started = loop.time()

        try:
            async with asyncio.timeout(timeout_seconds + 15):
                async with WsApiClient(configuration=self._configuration) as ws_api:
                    core_v1 = cast(_PodExecClient, k8s_client.CoreV1Api(api_client=ws_api))
                    websocket = await core_v1.connect_get_namespaced_pod_exec(
                        pod_name,
                        namespace,
                        command=command,
                        container=container,
                        stderr=True,
                        stdin=False,
                        stdout=True,
                        tty=False,
                        _preload_content=False,
                    )
                    async with websocket as ws:
                        while True:
                            message = await ws.receive()
                            if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
                                break
                            if message.type == WSMsgType.ERROR:
                                raise PodExecError(f"Kubernetes exec WebSocket failed: {ws.exception()}")
                            if message.type not in {WSMsgType.BINARY, WSMsgType.TEXT}:
                                continue
                            payload = message.data.encode() if isinstance(message.data, str) else message.data
                            if not payload:
                                continue
                            channel, chunk = payload[0], payload[1:]
                            if channel == STDOUT_CHANNEL:
                                stdout.append(chunk)
                            elif channel == STDERR_CHANNEL:
                                stderr.append(chunk)
                            elif channel == ERROR_CHANNEL:
                                remaining = 64 * 1024 - len(error_data)
                                if remaining > 0:
                                    error_data.extend(chunk[:remaining])
        except TimeoutError:
            return CommandResult(
                exit=TimedOut(), stdout=stdout.render(), stderr=stderr.render(), duration_seconds=loop.time() - started
            )
        except WSServerHandshakeError as error:
            raise PodExecError(_handshake_error(error.status, error.message)) from error
        except ApiException as error:
            raise PodExecError(
                f"Kubernetes could not execute the command (HTTP {error.status or 'unknown'}"
                f"{f': {error.reason}' if error.reason else ''})"
            ) from error

        if not error_data:
            raise PodExecError("Kubernetes exec ended without a command status frame; retry or inspect the pod")
        try:
            exit_code = WsApiClient.parse_error_data(bytes(error_data))
        except (KeyError, TypeError, ValueError) as error:
            raise PodExecError("Kubernetes exec returned a malformed command status frame") from error

        exit_status: Exited | TimedOut | Killed
        if exit_code == 124:
            exit_status = TimedOut()
        elif exit_code >= 128:
            exit_status = Killed(signal=exit_code - 128)
        else:
            exit_status = Exited(exit_code=exit_code)
        return CommandResult(
            exit=exit_status, stdout=stdout.render(), stderr=stderr.render(), duration_seconds=loop.time() - started
        )
