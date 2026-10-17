"""Launch and own the built Go egress sidecar in Python end-to-end tests."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from types import MappingProxyType

import aiohttp

from util.bazel.runfiles import get_required_path, own_repo_rlocation

_BINARY = get_required_path(own_repo_rlocation("agentplane/egress/sidecar/egress-sidecar_/egress-sidecar"))


def _free_ports(host: str, count: int) -> list[int]:
    listeners = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(count)]
    try:
        for listener in listeners:
            listener.bind((host, 0))
        return [listener.getsockname()[1] for listener in listeners]
    finally:
        for listener in listeners:
            listener.close()


class SidecarProcess:
    def __init__(
        self,
        *,
        proxy_host: str,
        proxy_port: int,
        token_file: Path,
        audience_token_files: Mapping[str, Path] = MappingProxyType({}),
        platform_routes: Mapping[str, Mapping[str, object]] = MappingProxyType({}),
        listen_host: str = "127.0.0.1",
        listen_port: int | None = None,
        readiness_host: str = "127.0.0.1",
        readiness_port: int | None = None,
    ) -> None:
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._token_file = token_file
        self._audience_token_files = audience_token_files
        self._platform_routes = platform_routes
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._readiness_host = readiness_host
        self._readiness_port = readiness_port
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: list[str] = []
        self._route_ports: dict[str, int] = {}

    async def __aenter__(self) -> SidecarProcess:
        routes = {}
        for name, config in self._platform_routes.items():
            routes[name] = dict(config)
        allocations: list[tuple[str, str | None]] = []
        if self._listen_port is None:
            allocations.append(("listen", self._listen_host))
        if self._readiness_port is None:
            allocations.append(("readiness", self._readiness_host))
        for name, config in routes.items():
            if config.get("listen_port") == 0:
                allocations.append((f"route:{name}", self._listen_host))
        ports_by_host: dict[str, list[str]] = {}
        for key, host in allocations:
            assert host is not None
            ports_by_host.setdefault(host, []).append(key)
        allocated = {
            key: port
            for host, keys in ports_by_host.items()
            for key, port in zip(keys, _free_ports(host, len(keys)), strict=True)
        }
        if self._listen_port is None:
            self._listen_port = allocated["listen"]
        if self._readiness_port is None:
            self._readiness_port = allocated["readiness"]
        for name, config in routes.items():
            port = config.get("listen_port")
            if port == 0:
                port = allocated[f"route:{name}"]
                config["listen_port"] = port
            if not isinstance(port, int):
                raise TypeError(f"platform route {name!r} listen_port must be an integer")
            self._route_ports[name] = port
        env = {
            "AGENTPLANE_EGRESS_SIDECAR_PROXY_HOST": self._proxy_host,
            "AGENTPLANE_EGRESS_SIDECAR_PROXY_PORT": str(self._proxy_port),
            "AGENTPLANE_EGRESS_SIDECAR_TOKEN_FILE": str(self._token_file),
            "AGENTPLANE_EGRESS_SIDECAR_AUDIENCE_TOKEN_FILES": json.dumps(
                {audience: str(path) for audience, path in self._audience_token_files.items()}
            ),
            "AGENTPLANE_EGRESS_SIDECAR_PLATFORM_ROUTES": json.dumps(routes),
            "AGENTPLANE_EGRESS_SIDECAR_LISTEN_HOST": self._listen_host,
            "AGENTPLANE_EGRESS_SIDECAR_LISTEN_PORT": str(self._listen_port),
            "AGENTPLANE_EGRESS_SIDECAR_READINESS_HOST": self._readiness_host,
            "AGENTPLANE_EGRESS_SIDECAR_READINESS_PORT": str(self._readiness_port),
        }
        self.process = await asyncio.create_subprocess_exec(
            str(_BINARY), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env
        )
        assert self.process.stderr is not None
        self._stderr_task = asyncio.create_task(self._capture_stderr(self.process.stderr))
        try:
            await self._wait_ready()
        except BaseException:
            await self._stop()
            raise
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self._stop()

    @property
    def listen_port(self) -> int:
        assert self._listen_port is not None
        return self._listen_port

    @property
    def readiness_port(self) -> int:
        assert self._readiness_port is not None
        return self._readiness_port

    @property
    def platform_route_ports(self) -> Mapping[str, int]:
        return self._route_ports

    async def _capture_stderr(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            self._stderr.append(line.decode(errors="replace").rstrip())

    async def _wait_ready(self) -> None:
        assert self.process is not None
        deadline = asyncio.get_running_loop().time() + 10
        async with aiohttp.ClientSession() as session:
            while asyncio.get_running_loop().time() < deadline:
                if self.process.returncode is not None:
                    raise RuntimeError(f"Go sidecar exited with {self.process.returncode}: {self._logs()}")
                try:
                    async with session.get(
                        f"http://{self._readiness_host}:{self.readiness_port}/readyz",
                        timeout=aiohttp.ClientTimeout(total=0.5),
                    ) as response:
                        if response.status == 200:
                            return
                except aiohttp.ClientError, TimeoutError:
                    pass
                await asyncio.sleep(0.02)
        raise TimeoutError(f"Go sidecar did not become ready: {self._logs()}")

    async def _stop(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        returncode = self.process.returncode
        self.process = None
        if returncode != 0:
            raise RuntimeError(f"Go sidecar exited with {returncode}: {self._logs()}")

    def _logs(self) -> str:
        return "\n".join(self._stderr)
